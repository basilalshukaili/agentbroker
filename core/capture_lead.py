"""
capture_lead — core operation handler.
Structured intake of a prospect into an SMB's funnel with deduplication.

FIX (2026-09-01): performs a REAL durable write to the Supabase `leads` table.
The lead_id is the actual inserted row id. $0.05 is charged ONLY when a new row
is written. On any Supabase failure: honest failure, cost=0.00, no fake lead_id.

WHAT THIS REPLACED. The handler computed `lead_<uuid5(dedup_key)>`, picked a
channel name off the SMB's advertised channel list, persisted NOTHING, and
returned status=partial / reason_code="lead_logged_no_crm". The id looked like a
record locator and referred to nothing; the channel name named a CRM that was
never contacted. It was on the ACT tool list regardless. Either it saves the
lead or it does not ship — the founder's call.

The receipt now reports channel_used="internal:supabase_leads", which is what
actually happens: the lead lands in AgentBroker's own lead store, where the SMB
reads it. It is NOT a write into the SMB's own CRM, and nothing here claims it.
"""
from __future__ import annotations

import time
import uuid

from core.models import CaptureLeadRequest, OutcomeReceipt, OperationStatus, CostRecord
from billing.pricing import receipt_usd as _receipt_usd
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
    #
    # It stays AHEAD of the durable write: a demo capture must not put a row in
    # the leads table either, or the SMB's funnel fills with sandbox prospects.
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

    # Dedupe key: (smb_id, phone or email or name). UNCHANGED from the stub —
    # `leads.dedup_key` carries a UNIQUE constraint built for exactly this
    # string, so altering the formula here would orphan every row already in the
    # table and let the same prospect in twice.
    dedup_key = f"{request.smb_id}|{request.prospect.phone or request.prospect.email or request.prospect.name}"

    # The table has no service_interest / consent_record_id columns, and dropping
    # caller-supplied fields on the floor is its own kind of lie — the agent
    # passed a consent record id precisely so a human could later prove the
    # prospect asked to be contacted. Fold them into `notes` rather than lose
    # them. (A follow-up migration could give them real columns.)
    note_parts: list[str] = []
    if request.prospect.notes:
        note_parts.append(request.prospect.notes)
    if request.prospect.service_interest:
        note_parts.append(f"service_interest: {request.prospect.service_interest}")
    if request.prospect.consent_record_id:
        note_parts.append(f"consent_record_id: {request.prospect.consent_record_id}")
    notes = " | ".join(note_parts) or None

    channel_used = "internal:supabase_leads"

    # Durable write to Supabase `leads` via the leads_insert_or_get SECURITY
    # DEFINER RPC (sql/agentbroker/005_leads_escalations_security_definer_rpc.sql).
    # `leads` carries RLS with no anon table grant at all (this data is
    # customer PII, unlike sanctions_names) -- a direct insert_row/
    # select_rows_strict call from this anon-only container is rejected by
    # RLS and always returns empty, which is what made capture_lead
    # completely non-operational before this RPC existed. The RPC does the
    # insert-or-read-back atomically server-side (INSERT ... ON CONFLICT
    # (dedup_key) DO NOTHING, then a read-back on conflict), replacing the
    # two separate REST calls this handler used to make.
    #
    # lead_id is set ONLY from the real row the RPC returns, never fabricated.
    from storage.supabase_client import rpc
    try:
        resp = await rpc("leads_insert_or_get", {
            "p_dedup_key": dedup_key,
            "p_smb_id": request.smb_id,
            "p_prospect_name": request.prospect.name,
            "p_prospect_phone": request.prospect.phone,
            "p_prospect_email": request.prospect.email,
            "p_source": request.source,
            "p_notes": notes,
            "p_agent_id": agent_id,
            "p_channel_used": channel_used,
        })
    except Exception:                        # noqa: BLE001 -- rpc() raises on any failure
        resp = None

    if resp:
        row = resp.get("row") or {}
        lead_id = str(row.get("id") or "")
        if lead_id:
            return _captured(
                operation_id, request, lead_id, dedup_key, channel_used,
                row.get("created_at"), deduplicated=bool(resp.get("deduplicated")),
                t0=t0, trace_id=trace_id,
            )

    # Nothing was written and we could not find a prior row — honest failure,
    # no charge, no lead_id. retriable because the common cause is transient.
    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="upstream_failure",
        human_message=(
            "The lead could not be stored (lead store unavailable). Nothing was "
            "written and nothing was charged. Retry — capture is idempotent, so "
            "a retry cannot create a duplicate."
        ),
        result=None,
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        retriable=True,
        trace_id=trace_id,
    )


def _captured(
    operation_id: str,
    request: CaptureLeadRequest,
    lead_id: str,
    dedup_key: str,
    channel_used: str,
    created_at,
    *,
    deduplicated: bool,
    t0: float,
    trace_id: str | None,
) -> OutcomeReceipt:
    """The success receipt, shared by the fresh-insert and the replay paths.

    The two differ ONLY in cost. A replay stored nothing new — the price is
    per lead, and preview_cost publishes that basis — so charging again for the
    same lead would bill twice for one unit of work. The credits rail settles
    from cost.amount, so cost=0.00 here is a real refund of the hold.
    """
    # Built before the receipt rather than inline: test_receipt_cost_parity
    # scans core/ for a literal after `CostRecord(amount=`, and it is right to
    # — every price here comes from billing/pricing.py so the receipt and the
    # table cannot drift apart again.
    cost = (CostRecord(amount=0.0, currency="USD", basis="no_charge_duplicate")
            if deduplicated else
            CostRecord(amount=_receipt_usd("capture_lead"), currency="USD",
                       basis="per_lead"))
    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code="lead_already_captured" if deduplicated else "lead_captured",
        human_message=(
            (f"This prospect was already in the funnel; returning the existing "
             f"lead (id {lead_id}). Nothing was duplicated and nothing was charged."
             if deduplicated else
             f"Lead stored in the SMB's AgentBroker funnel (id {lead_id}). "
             f"This is AgentBroker's lead store, not a write into the business's "
             f"own CRM.")
        ),
        result={
            "lead_id": lead_id,
            "smb_id": request.smb_id,
            "prospect_name": request.prospect.name,
            "source": request.source,
            "channel_used": channel_used,
            "dedup_key": dedup_key,
            "deduplicated": deduplicated,
            "created_at": str(created_at) if created_at else None,
        },
        cost=cost,
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=channel_used,
        retriable=False,
        trace_id=trace_id,
    )
