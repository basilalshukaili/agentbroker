"""
Outcome store — persists terminal OutcomeReceipts and async job states.
In production: PostgreSQL. For tests: in-memory dict.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


class OutcomeStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def set_pending(self, operation_id: str, operation_type: str) -> None:
        self._records[operation_id] = {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def set_executing(self, operation_id: str) -> None:
        if operation_id in self._records:
            self._records[operation_id]["status"] = "executing"
            self._records[operation_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def set_complete(self, operation_id: str, outcome: dict[str, Any]) -> None:
        if operation_id not in self._records:
            self._records[operation_id] = {"operation_id": operation_id}
        self._records[operation_id].update({
            "status": outcome.get("status", "success"),
            "outcome": outcome,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def get(self, operation_id: str) -> Optional[dict[str, Any]]:
        return self._records.get(operation_id)

    def exists(self, operation_id: str) -> bool:
        return operation_id in self._records


_store = OutcomeStore()


def get_outcome_store() -> OutcomeStore:
    return _store
