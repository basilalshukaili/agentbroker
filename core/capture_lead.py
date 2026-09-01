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

    # CRITICAL-1 fix: demo SMBs must never trigger a real charge.
    # The directory contract (smb_directory.py line 39) promises that bookings
    # against demo SMBs short-circuit with reason_code='demo_smb_no_live_booking'
    # instead of contacting real businesses. This guard honours that promise and
    # returns status=failure so _receipt_is_error() returns True in x402_gate,
    # which causes the SDK to SKIP settlement — no USDC charged.
    if getattr(smb, "is_demo", False):
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="demo_smb_no_live_booking",
            human_message=(
                f"{smb.name} is a sandbox/demo entry. No real action was taken. "
                "Use import_booking_url to add a real business."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_demo"),
            retriable=False,
            trace_id=trace_id,
        )

    # MEDIUM-2 fix: capture_lead currently performs no real CRM write — it only
    # computes a deterministic UUID and selects a channel name. Returning
    # status=success and charging $0.05 for this stub work is dishonest billing.
    # Return status=partial / cost=0 until a real CRM write (DB row, webhook,
    # or API call) is implemented. The lead_id is still returned so callers can
    # reference the dedup key in a follow-up once CRM is wired.
    #
    # Dedupe key: (smb_id, phone or email)
    dedup_key = f"{request.smb_id}|{request.prospect.phone or request.prospect.email or request.prospect.name}"
    lead_id = f"lead_{uuid.uuid5(uuid.NAMESPACE_URL, dedup_key).hex[:12]}"

    # Attempt direct CRM integration if available (stub: no real write yet)
    channel_used = None
    if "direct_api:calcom" in smb.channels_available:
        channel_used = "direct_api:calcom"
    elif "email" in " ".join(smb.channels_available):
        channel_used = "email:sendgrid"

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.PARTIAL,
        reason_code="lead_logged_no_crm",
        human_message=(
            f"Lead noted for {smb.name} (lead_id={lead_id}). "
            "No external CRM write performed — charged $0 (not charged) until real CRM persistence is wired."
        ),
        result={
            "lead_id": lead_id,
            "smb_id": request.smb_id,
            "prospect_name": request.prospect.name,
            "channel_used": channel_used,
            "source": request.source,
        },
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_stub"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=channel_used,
        retriable=False,
        trace_id=trace_id,
    )
