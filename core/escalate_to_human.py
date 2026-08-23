"""
escalate_to_human -- core operation handler.
Hand off to a human operator with full context bundle.

FIX 1 (2026-08-24): performs a REAL durable write to the Supabase `escalations`
table. The ticket_id is the actual inserted row id. $0.20 is charged ONLY when
the insert succeeds. On any Supabase failure: honest failure, cost=0.00, no
fake ticket. Never promises a "60-300 second" response time.
"""
from __future__ import annotations

import time
import uuid

from core.models import EscalateToHumanRequest, OutcomeReceipt, OperationStatus, CostRecord


async def handle_escalate_to_human(
    request: EscalateToHumanRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())

    queue = "urgent_support" if request.priority == "urgent" else "smb_support"

    # Build the context payload stored alongside the escalation record.
    context_payload = {
        "original_operation": request.context.original_operation,
        "original_operation_id": request.context.operation_id,
        "recommended_next_step": request.context.recommended_next_step,
        "context_bundle_size": len(request.context.transcript or []),
        "assigned_queue": queue,
        "agent_id": agent_id,
        "trace_id": trace_id,
    }

    # Durable write to Supabase `escalations` table.
    # ticket_id is set ONLY from the real inserted row id, never fabricated.
    from storage.supabase_client import insert_row
    row = {
        "operation_id": operation_id,
        "reason": request.reason.value,
        "context": context_payload,
        "status": "open",
        "source": agent_id or "anonymous",
    }
    inserted = await insert_row("escalations", row)

    if inserted is None:
        # Supabase unreachable or table error -- honest failure, no charge.
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="escalation_not_recorded",
            human_message=(
                "Escalation could not be recorded (operator queue unavailable). "
                "Nothing was written and nothing was charged. Retry or contact "
                "support directly."
            ),
            result=None,
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            channel_used=None,
            retriable=True,
            trace_id=trace_id,
        )

    # Insert succeeded -- use the real DB-assigned id as the ticket_id.
    ticket_id = str(inserted.get("id", operation_id))
    result = {
        "escalation_ticket_id": ticket_id,
        "assigned_queue": queue,
        "reason": request.reason.value,
        "original_operation": request.context.original_operation,
        "original_operation_id": request.context.operation_id,
        "recommended_next_step": request.context.recommended_next_step,
        "context_bundle_size": len(request.context.transcript or []),
    }

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code="escalation_recorded",
        human_message=(
            f"Escalation recorded to the operator queue (id {ticket_id}); "
            "a human will review it."
        ),
        result=result,
        cost=CostRecord(amount=0.20, currency="USD", basis="per_escalation"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used="internal:supabase_escalations",
        retriable=False,
        trace_id=trace_id,
    )
