"""
call_business — conversational voice-AI call to a business.

The differentiated capability: reach the ~60M long-tail SMBs that have NO API
and NO booking page, only a phone number. An AI agent cannot pick up a phone
and hold a conversation. We can — via Vapi voice AI. The agent gives an
objective; we place the call, navigate the conversation, and return a
structured answer.

This is BUSINESS-directed calling (B2B), materially less restricted under TCPA
than calls to consumers — but the compliance gate still runs (recording
consent per jurisdiction). The call always identifies itself as an AI agent
acting on a consumer's behalf.

Routes exclusively through Vapi (conversational voice AI). Twilio is SMS-only
in this codebase and raw Twilio voice is robotic IVR, not conversation — so it
is NOT a fallback here. If Vapi is not provisioned (no VAPI_PHONE_NUMBER_ID),
the handler returns a clean "voice_not_provisioned" receipt rather than crashing.
"""
from __future__ import annotations

import os
import time
import uuid

from core.models import (
    CallBusinessRequest, OutcomeReceipt, OperationStatus, CostRecord,
    ComplianceViolationError,
)
from channels.voice_ai.vapi import VapiVoiceAdapter
from channels.adapter_interface import ChannelRequest
from storage.outcome_store import get_outcome_store
from billing.pricing import receipt_usd as _receipt_usd

_VOICE_ADAPTER = VapiVoiceAdapter()

# Price per call attempt. SINGLE SOURCE OF TRUTH is billing/pricing.py
# ("call_business": 20 credits = $0.20 — a deliberate subsidy below the ~$0.30
# Vapi cost, per the founder's 2026-08-26 decision). Deriving it here keeps the
# receipt, preview_cost, and the credits deduction in lockstep; a hardcoded
# value drifted to $0.50 and overstated the charge on every successful call.
_CALL_PRICE_USD = _receipt_usd("call_business")


def _resolve_phone(request: CallBusinessRequest) -> str | None:
    """Resolve the target phone number from business_phone or smb_id."""
    if request.business_phone:
        return request.business_phone
    if request.smb_id:
        from supply.smb_directory import get_directory
        entry = get_directory().get(request.smb_id)
        if entry and entry.phone:
            return entry.phone
    return None


async def handle_call_business(
    request: CallBusinessRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())

    phone = _resolve_phone(request)
    if not phone:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=(
                "No callable phone number. Provide business_phone (E.164, e.g. "
                "+14045550123) or an smb_id that has a phone on record."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_bad_input"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    # Voice not provisioned → honest receipt, no crash, no charge.
    if not os.getenv("VAPI_API_KEY") or not os.getenv("VAPI_PHONE_NUMBER_ID"):
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="voice_not_provisioned",
            human_message=(
                "Voice calling is not yet provisioned on this deployment "
                "(VAPI_PHONE_NUMBER_ID unset). The operator must add a Vapi "
                "phone number to enable call_business."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_not_provisioned"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=True,
            next_actions=["Operator: set VAPI_PHONE_NUMBER_ID and redeploy."],
            trace_id=trace_id,
        )

    # Build the opening line + objective into the call script.
    on_behalf = f" on behalf of {request.on_behalf_of}" if request.on_behalf_of else ""
    script = (
        f"Hello, this is an AI assistant calling{on_behalf}. {request.objective}"
    )

    channel_request = ChannelRequest(
        recipient_id=phone,
        channel="voice",
        message_type="transactional",  # B2B operational call, not marketing
        content=script,
        country_code=request.country_code,
        agent_id=agent_id,
        trace_id=trace_id,
    )

    try:
        resp = await _VOICE_ADAPTER.send(channel_request)
    except ComplianceViolationError as cve:
        receipt = OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="compliance_violation",
            human_message=cve.message,
            result={"rule": cve.rule, "jurisdiction": cve.jurisdiction},
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_compliance_block"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )
        get_outcome_store().set_complete(operation_id, receipt.model_dump(mode="json"))
        return receipt
    except Exception as exc:
        receipt = OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="upstream_failure",
            human_message=f"Voice call could not be placed: {exc}",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_delivery_failure"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=True,
            trace_id=trace_id,
        )
        get_outcome_store().set_complete(operation_id, receipt.model_dump(mode="json"))
        return receipt

    if not resp.success:
        receipt = OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code=resp.error_code or "upstream_failure",
            human_message=resp.error_message or "Voice call failed.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_delivery_failure"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=True,
            trace_id=trace_id,
        )
        get_outcome_store().set_complete(operation_id, receipt.model_dump(mode="json"))
        return receipt

    # Vapi calls are async by nature — the conversation happens over the wire.
    # We return the call handle immediately; the agent polls get_outcome for
    # the transcript + extracted fields once the call completes.
    receipt = OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.PENDING_ASYNC,
        reason_code="call_placed",
        human_message=(
            f"Voice call placed to {phone}. Poll get_outcome(operation_id) for "
            "the transcript and extracted answer once the call completes."
        ),
        result={
            "provider_call_id": resp.provider_message_id,
            "objective": request.objective,
            "extract_fields": request.extract_fields,
            "target_phone": phone,
        },
        cost=CostRecord(amount=_CALL_PRICE_USD, currency="USD", basis="per_call"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used="voice_ai:vapi",
        estimated_completion_time=None,
        retriable=False,
        next_actions=["Poll get_outcome(operation_id) for transcript + answer."],
        trace_id=trace_id,
    )
    get_outcome_store().set_pending(operation_id, "call_business")
    return receipt
