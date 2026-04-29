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


_store = IdempotencyStore()


def get_idempotency_store() -> IdempotencyStore:
    return _store
