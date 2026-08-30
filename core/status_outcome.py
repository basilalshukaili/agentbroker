"""
get_status / get_outcome — core operation handlers.

FIX 1 (2026-08-23): Both handlers now call get_async() which reads from
Supabase when the operation is not in the same-process in-memory cache,
making async polling work across requests.
"""
from __future__ import annotations

import time

from core.models import OutcomeReceipt, OperationStatus, CostRecord
from storage.outcome_store import get_outcome_store
from billing.pricing import receipt_usd as _receipt_usd


async def handle_get_status(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    store = get_outcome_store()
    record = await store.get_async(operation_id)
    if not record:
        out = {
            "operation_id": operation_id,
            "status": "not_found",
            "error": "No operation found with this ID.",
        }
        if trace_id:
            out["trace_id"] = trace_id
        return out
    out = {
        "operation_id": operation_id,
        "status": record.get("status", "pending"),
        "estimated_completion_time": record.get("estimated_completion_time"),
        "last_updated_at": record.get("updated_at"),
        "partial_result": record.get("partial_result"),
    }
    # TRACE_ID IS ADVERTISED FOR CORRELATION AND WAS BEING DROPPED. A caller
    # passing it got nothing back to correlate against, which is the whole
    # point of sending one. Echoing it is the minimum that makes the parameter
    # real; it costs nothing and it is what a caller asked for.
    if trace_id:
        out["trace_id"] = trace_id
    return out


async def handle_get_outcome(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    store = get_outcome_store()
    record = await store.get_async(operation_id)

    if not record:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="not_found",
            human_message=f"No operation found with ID {operation_id}.",
            cost=CostRecord(amount=_receipt_usd("get_status"), currency="USD", basis="free"),
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
            cost=CostRecord(amount=_receipt_usd("get_status"), currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    outcome = record.get("outcome")
    if outcome:
        try:
            return OutcomeReceipt(**outcome)
        except Exception:
            # outcome may be a plain dict from Supabase; build best-effort receipt
            return OutcomeReceipt(
                operation_id=outcome.get("operation_id", operation_id),
                status=OperationStatus(outcome.get("status", "success")),
                reason_code=outcome.get("reason_code", "completed"),
                human_message=outcome.get("human_message", "Operation completed."),
                result=outcome.get("result"),
                cost=CostRecord(**(outcome.get("cost") or {"amount": 0.001, "currency": "USD", "basis": "per_call"})),
                latency_ms=int((time.monotonic() - t0) * 1000),
                retriable=False,
                trace_id=trace_id,
            )

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="outcome_not_stored",
        human_message="Operation completed but outcome record is missing. Contact support.",
        cost=CostRecord(amount=_receipt_usd("get_outcome"), currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )
