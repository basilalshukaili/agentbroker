"""
capture_lead — core operation handler.
Structured intake of a prospect into an SMB's funnel with deduplication.
"""
from __future__ import annotations

import time
import uuid

from core.models import CaptureLeadRequest, OutcomeReceipt, OperationStatus, CostRecord
from supply.smb_directory import get_directory


async def handle_capture_lead(
    request: CaptureLeadRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())
    directory = get_directory()
    smb = directory.get(request.smb_id)

    if not smb:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="supply_unreachable",
            human_message=f"SMB {request.smb_id} not found.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            retriable=False,
            trace_id=trace_id,
        )

    # Dedupe key: (smb_id, phone or email)
    dedup_key = f"{request.smb_id}|{request.prospect.phone or request.prospect.email or request.prospect.name}"
    lead_id = f"lead_{uuid.uuid5(uuid.NAMESPACE_URL, dedup_key).hex[:12]}"

    # Attempt direct CRM integration if available (stub: always succeed)
    channel_used = None
    if "direct_api:calcom" in smb.channels_available:
        channel_used = "direct_api:calcom"
    elif "email" in " ".join(smb.channels_available):
        channel_used = "email:sendgrid"

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code="lead_captured",
        human_message=f"Lead captured for {smb.name}.",
        result={
            "lead_id": lead_id,
            "smb_id": request.smb_id,
            "prospect_name": request.prospect.name,
            "channel_used": channel_used,
            "source": request.source,
        },
        cost=CostRecord(amount=0.10, currency="USD", basis="per_lead"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=channel_used,
        retriable=False,
        trace_id=trace_id,
    )
