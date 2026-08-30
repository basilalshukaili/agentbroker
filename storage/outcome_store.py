"""
Outcome store — persists terminal OutcomeReceipts and async job states.

FIX 1 (2026-08-23): Added durable Supabase backend so get_status and
get_outcome return real records across requests.  The in-memory dict is kept
as a write-through cache and test backend.

Write path  : every set_complete() call writes in-memory AND fires a
              best-effort Supabase upsert (fail-open — a store failure never
              breaks the tool call).

Read path   : get_async() checks in-memory first (O(1), handles same-request
              and test cases), then falls back to a Supabase SELECT by PK.
              get() stays synchronous for backward-compat / unit tests.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("smb_broker.outcome_store")


class OutcomeStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def set_pending(self, operation_id: str, operation_type: str) -> None:
        self._records[operation_id] = {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._fire_persist(operation_id, self._records[operation_id])

    def set_executing(self, operation_id: str) -> None:
        if operation_id in self._records:
            self._records[operation_id]["status"] = "executing"
            self._records[operation_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._fire_persist(operation_id, self._records[operation_id])

    def set_complete(
        self,
        operation_id: str,
        outcome: dict[str, Any],
        *,
        tool: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        if operation_id not in self._records:
            self._records[operation_id] = {"operation_id": operation_id}
        self._records[operation_id].update({
            "status": outcome.get("status", "success"),
            "outcome": outcome,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self._fire_persist(operation_id, self._records[operation_id], outcome=outcome,
                           tool=tool, agent_id=agent_id)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        """Synchronous get — in-memory only.  Used by tests and legacy callers."""
        return self._records.get(operation_id)

    async def get_async(self, operation_id: str) -> Optional[dict[str, Any]]:
        """
        Async get — checks in-memory first, then Supabase.
        Returns None if the operation is unknown on both layers.
        """
        local = self._records.get(operation_id)
        if local:
            return local
        # Fall back to Supabase (durable, cross-request)
        try:
            row = await _supabase_fetch(operation_id)
            if row:
                # Reconstruct the envelope the handlers expect
                return {
                    "operation_id": row.get("operation_id", operation_id),
                    "status": row.get("status", "unknown"),
                    "outcome": row.get("result_json") or {},
                    "updated_at": row.get("ts"),
                    "partial_result": None,
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("outcome_store_supabase_fetch_failed id=%s err=%s", operation_id, exc)
        return None

    def exists(self, operation_id: str) -> bool:
        return operation_id in self._records

    # ------------------------------------------------------------------
    # Durable write (fire-and-forget)
    # ------------------------------------------------------------------

    def _fire_persist(
        self,
        operation_id: str,
        record: dict,
        outcome: Optional[dict] = None,
        tool: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """Schedule a best-effort Supabase upsert.  Never raises."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    _supabase_upsert(operation_id, record, outcome, tool, agent_id)
                )
        except Exception:  # noqa: BLE001
            pass


async def _supabase_upsert(
    operation_id: str,
    record: dict,
    outcome: Optional[dict],
    tool: Optional[str],
    agent_id: Optional[str],
) -> None:
    """Write one operation row to Supabase.  Never raises — fail-open."""
    try:
        import json as _json
        from storage.supabase_client import upsert_row
        # FIX (quota strip, belt-and-suspenders): strip the transient per-call
        # `quota` block from the serialised result even if the caller somehow
        # passed a mutated dict.  Quota belongs to the response envelope only.
        _EPHEMERAL_KEYS = frozenset({"quota"})
        persisted_outcome = (
            {k: v for k, v in outcome.items() if k not in _EPHEMERAL_KEYS}
            if outcome else None
        )
        row: dict[str, Any] = {
            "operation_id": operation_id,
            "ts": record.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            "tool": tool or record.get("operation_type") or "unknown",
            "status": record.get("status", "unknown"),
            "reason_code": (outcome or {}).get("reason_code"),
            "result_json": _json.dumps(persisted_outcome, default=str) if persisted_outcome else None,
            "agent_id": agent_id,
        }
        # CHECK THE RESULT. upsert_row returns None on failure and cannot
        # raise, so the handler below was dead code - and a lost write means a
        # later get_status/get_outcome answers "unknown operation", which a
        # caller reads as "that never happened" rather than "we lost the
        # record of it". For an operation that may have sent a message or
        # taken a booking, those are very different things.
        written = await upsert_row("operations", row, on_conflict="operation_id")
        if written is None:
            logger.warning(
                "outcome_store_persist_failed id=%s -- the durable record was "
                "NOT written, so get_status/get_outcome will report this "
                "operation as unknown even though it ran", operation_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcome_store_persist_failed id=%s err=%s", operation_id, exc)


async def _supabase_fetch(operation_id: str) -> Optional[dict]:
    """Fetch one row from the operations table.  Returns None on any error."""
    try:
        from storage.supabase_client import select_rows
        rows = await select_rows("operations", filters={"operation_id": operation_id}, limit=1)
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome_store_fetch_failed id=%s err=%s", operation_id, exc)
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store = OutcomeStore()


def get_outcome_store() -> OutcomeStore:
    return _store
