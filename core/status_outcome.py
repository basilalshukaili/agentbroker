"""
get_status / get_outcome — core operation handlers.
"""
from __future__ import annotations

import time
import uuid

from core.models import OutcomeReceipt, OperationStatus, CostRecord
from storage.outcome_store import get_outcome_store


async def handle_get_status(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    store = get_outcome_store()
    record = store.get(operation_id)
    if not record:
        return {
            "operation_id": operation_id,
            "status": "not_found",
            "error": "No operation found with this ID.",
        }
    return {
        "operation_id": operation_id,
        "status": record.get("status", "pending"),
        "estimated_completion_time": record.get("estimated_completion_time"),
        "last_updated_at": record.get("updated_at"),
        "partial_result": record.get("partial_result"),
    }


async def handle_get_outcome(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    store = get_outcome_store()
    record = store.get(operation_id)

    if not record:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="not_found",
            human_message=f"No operation found with ID {operation_id}.",
            cost=CostRecord(amount=0.001, currency="USD", basis="per_call"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    if record.get("status") in ("pending", "executing"):
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.PENDING_ASYNC,
            reason_code="still_in_progress",
            human_message="Operation is still in progress. Poll get_status or await webhook.",
            cost=CostRecord(amount=0.001, currency="USD", basis="per_call"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    outcome = record.get("outcome")
    if outcome:
        return OutcomeReceipt(**outcome)

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="outcome_not_stored",
        human_message="Operation completed but outcome record is missing. Contact support.",
        cost=CostRecord(amount=0.001, currency="USD", basis="per_call"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )
