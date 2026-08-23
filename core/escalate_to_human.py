"""
escalate_to_human — core operation handler.
Hand off to a human operator with full context bundle.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from core.models import EscalateToHumanRequest, OutcomeReceipt, OperationStatus, CostRecord


async def handle_escalate_to_human(
    request: EscalateToHumanRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())
    ticket_id = f"esc_{uuid.uuid4().hex[:8]}"

    queue = "urgent_support" if request.priority == "urgent" else "smb_support"
    estimated = datetime.now(timezone.utc) + timedelta(
        seconds=60 if request.priority == "urgent" else 300
    )

    # In production: submit to ticketing system (Zendesk, Linear, etc.)
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
        status=OperationStatus.PENDING_ASYNC,
        reason_code="escalation_queued",
        human_message=f"Escalation ticket {ticket_id} created in {queue}. Estimated human response: {estimated.isoformat()}.",
        result=result,
        cost=CostRecord(amount=0.20, currency="USD", basis="per_escalation"),  # FIX 4: manifest price
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used="internal:ticketing",
        estimated_completion_time=estimated,
        next_actions=[f"poll get_status with operation_id {operation_id}"],
        retriable=False,
        trace_id=trace_id,
    )
