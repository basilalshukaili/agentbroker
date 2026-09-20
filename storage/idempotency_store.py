"""
Idempotency store — prevents duplicate state-changing operations.
Key: (agent_id, operation, idempotency_key). Value: cached OutcomeReceipt JSON.
In production: Redis (primary) + PostgreSQL (durable backup). For tests: in-memory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_TTL_HOURS = 24


class IdempotencyStore:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def _key(self, agent_id: str, operation: str, idempotency_key: str) -> str:
        return f"{agent_id}|{operation}|{idempotency_key}"

    def get(self, agent_id: str, operation: str, idempotency_key: str) -> Optional[dict]:
        key = self._key(agent_id, operation, idempotency_key)
        record = self._store.get(key)
        if not record:
            return None
        # Check TTL
        created = datetime.fromisoformat(record["created_at"])
        if datetime.now(timezone.utc) > created + timedelta(hours=_TTL_HOURS):
            del self._store[key]
            return None
        return record["outcome"]

    def set(self, agent_id: str, operation: str, idempotency_key: str, outcome: dict) -> None:
        key = self._key(agent_id, operation, idempotency_key)
        self._store[key] = {
            "outcome": outcome,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def exists(self, agent_id: str, operation: str, idempotency_key: str) -> bool:
        return self.get(agent_id, operation, idempotency_key) is not None

    # ------------------------------------------------------------------
    # Claim-based API — closes the concurrency hole get()/set() cannot.
    #
    # get() then set() (the original pair above, still used by check_gates.py
    # and kept unchanged for that reason) has a window: two callers can both
    # get() None before either set()s, so both proceed to execute the real
    # tool. reserve() collapses that window to nothing by checking AND
    # marking "pending" in one call with no `await` in between -- since
    # asyncio only hands control to another coroutine at a genuine suspension
    # point, a synchronous method like this one always runs to completion
    # before anything else in the same event loop can observe the store, so
    # two concurrent claims for the same key can never both win. See
    # agent_interface/idempotency_gate.py's claim(), the only caller.
    # ------------------------------------------------------------------

    def _is_expired(self, record: dict[str, Any]) -> bool:
        created = datetime.fromisoformat(record["created_at"])
        return datetime.now(timezone.utc) > created + timedelta(hours=_TTL_HOURS)

    def reserve(
        self, agent_id: str, operation: str, idempotency_key: str
    ) -> tuple[str, Optional[dict]]:
        """Atomically claim (agent_id, operation, idempotency_key).

        Returns ("claimed", None) — this call now OWNS the key and must
        call complete() or release() when it is done; no concurrent
        duplicate may execute the real tool while a claim is held.

        Returns ("in_progress", None) — another call already holds this key
        and has not finished. The caller MUST NOT execute the tool again.

        Returns ("complete", outcome) — a prior call already finished;
        replay `outcome` verbatim.
        """
        key = self._key(agent_id, operation, idempotency_key)
        record = self._store.get(key)
        if record is not None and self._is_expired(record):
            del self._store[key]
            record = None
        if record is None:
            self._store[key] = {
                "status": "pending",
                "outcome": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            return "claimed", None
        if record.get("status") == "pending":
            return "in_progress", None
        return "complete", record.get("outcome")

    def complete(
        self, agent_id: str, operation: str, idempotency_key: str, outcome: dict
    ) -> None:
        """Mark a held claim COMPLETE with the outcome future replays return."""
        key = self._key(agent_id, operation, idempotency_key)
        created = self._store.get(key, {}).get(
            "created_at", datetime.now(timezone.utc).isoformat())
        self._store[key] = {
            "status": "complete", "outcome": outcome, "created_at": created,
        }

    def release(self, agent_id: str, operation: str, idempotency_key: str) -> None:
        """Drop a claim that did NOT complete successfully (a transient
        failure, or an exception), so a legitimate retry with the same key
        can proceed — mirrors the existing "only successful responses are
        stored" rule, extended to the pending marker itself. A no-op if the
        key is not currently held pending (e.g. already completed, or never
        claimed), so it is always safe to call from a `finally`-style path.
        """
        key = self._key(agent_id, operation, idempotency_key)
        record = self._store.get(key)
        if record is not None and record.get("status") == "pending":
            del self._store[key]


_store = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    return _store
