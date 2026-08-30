"""
send_message — core operation handler.
Routes outbound messages through the best available channel with automatic fallback.
All paths pass through compliance.pre_check before dispatch.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from core.models import (
    SendMessageRequest, OutcomeReceipt, OperationStatus, CostRecord,
    ChannelPreference, ComplianceViolationError, ErrorCode
)
import os

from channels.sms_email.twilio_sms import TwilioSMSAdapter
from channels.sms_email.sendgrid_email import SendGridEmailAdapter
from channels.sms_email.resend_email import ResendEmailAdapter
from channels.voice_ai.vapi import VapiVoiceAdapter
from channels.whatsapp.cloud_api import WhatsAppCloudAdapter
from channels.adapter_interface import ChannelRequest
from telemetry.metrics import increment_messages_sent


_SMS_ADAPTER = TwilioSMSAdapter()
_VOICE_ADAPTER = VapiVoiceAdapter()
_WHATSAPP_ADAPTER = WhatsAppCloudAdapter()

# Email provider: Resend is the one we actually own a verified domain on
# (hatchloop.dev). SendGrid stays as the fallback name for deployments that
# configured it. Whichever has a key wins; with neither, send_message now
# fails honestly instead of returning a stub receipt (2026-08-04).
_RESEND_ADAPTER = ResendEmailAdapter()
_SENDGRID_ADAPTER = SendGridEmailAdapter()
if os.getenv("RESEND_API_KEY"):
    _EMAIL_CHANNEL, _EMAIL_ADAPTER = "email:resend", _RESEND_ADAPTER
elif os.getenv("SENDGRID_API_KEY"):
    _EMAIL_CHANNEL, _EMAIL_ADAPTER = "email:sendgrid", _SENDGRID_ADAPTER
else:
    _EMAIL_CHANNEL, _EMAIL_ADAPTER = "email:resend", _RESEND_ADAPTER

# Cost per channel
_CHANNEL_COSTS = {
    "sms:twilio": 0.05,
    "email:sendgrid": 0.02,
    "email:resend": 0.02,
    "voice_ai:vapi": 0.30,
    # FREE during launch (founder 2026-08-26: "anything that is not costing us,
    # put on free trial"). WhatsApp costs us $0 today (test number; service-window
    # replies are free even at production). run_metered_tool commits actual from
    # the receipt cost, so 0.00 here = 0 credits charged, honestly. Revisit when
    # Meta template charges apply at scale.
    "whatsapp:cloud_api": 0.00,
}


def _build_channel_chain(preference: ChannelPreference, recipient_id: str) -> list[tuple[str, object]]:
    """Return ordered list of (channel_name, adapter) to try.

    Fallbacks are filtered by what the recipient identifier can actually
    receive: an email address cannot be reached by SMS or a phone call, and a
    phone number cannot be emailed. Before 2026-08-04 the chain fell back
    across incompatible channels, so an unconfigured email send surfaced an
    unrelated SMS/10DLC compliance error to the caller.
    """
    is_email = "@" in recipient_id
    if preference == ChannelPreference.SMS:
        chain = [("sms:twilio", _SMS_ADAPTER), (_EMAIL_CHANNEL, _EMAIL_ADAPTER)]
    elif preference == ChannelPreference.EMAIL:
        chain = [(_EMAIL_CHANNEL, _EMAIL_ADAPTER), ("sms:twilio", _SMS_ADAPTER)]
    elif preference == ChannelPreference.VOICE:
        chain = [("voice_ai:vapi", _VOICE_ADAPTER), ("sms:twilio", _SMS_ADAPTER)]
    elif preference == ChannelPreference.WHATSAPP:
        chain = [("whatsapp:cloud_api", _WHATSAPP_ADAPTER), ("sms:twilio", _SMS_ADAPTER)]
    elif is_email:
        chain = [(_EMAIL_CHANNEL, _EMAIL_ADAPTER), ("sms:twilio", _SMS_ADAPTER)]
    else:
        chain = [("sms:twilio", _SMS_ADAPTER), (_EMAIL_CHANNEL, _EMAIL_ADAPTER)]

    def reachable(channel_name: str) -> bool:
        kind = channel_name.split(":")[0]
        return (kind == "email") if is_email else (kind in ("sms", "voice_ai", "whatsapp"))

    filtered = [c for c in chain if reachable(c[0])]
    return filtered or chain[:1]


async def handle_send_message(
    request: SendMessageRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())

    # WE DO NOT SCHEDULE, SO WE MUST NOT ACCEPT A SCHEDULE.
    #
    # send_at_iso was advertised as "Schedule for future delivery; omit for
    # immediate" and is read nowhere. Until today it never even reached the
    # handler, so it was inert twice over; forwarding it made the promise
    # look supported without making it true. Measured: a requested delivery
    # of 2027-01-01T09:00Z sent IMMEDIATELY, with a receipt reading
    # "Message delivered" and no mention of scheduling.
    #
    # Disclosure is not enough here, unlike a filter that merely fails to
    # narrow. A caller scheduling a 9am reminder would have it delivered at
    # 3am to a real phone, and no wording in the response undoes that. So it
    # is refused, loudly, until it is built.
    if request.send_at_iso is not None:
        _now = datetime.now(timezone.utc)
        _when = request.send_at_iso
        if _when.tzinfo is None:
            _when = _when.replace(tzinfo=timezone.utc)
        if _when > _now + timedelta(minutes=2):
            return OutcomeReceipt(
                operation_id=operation_id,
                status=OperationStatus.FAILURE,
                reason_code="scheduling_not_supported",
                human_message=(
                    f"NOT SENT. send_at_iso asked for delivery at "
                    f"{_when.isoformat()}, and this service does not schedule "
                    f"messages - it would have sent immediately. Nothing was "
                    f"sent and nothing was charged. Call send_message at the "
                    f"time you want it delivered, or omit send_at_iso to send "
                    f"now."),
                cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                retriable=False,
                trace_id=trace_id,
            )

    chain = _build_channel_chain(request.preferred_channel, request.recipient.id_value)
    attempted: list[str] = []
    last_error: str = ""

    channel_request = ChannelRequest(
        recipient_id=request.recipient.id_value,
        channel="sms",  # updated per attempt
        message_type=request.message_type.value,
        content=request.content.body,
        subject=request.content.subject,
        country_code=request.recipient.country_code,
        agent_id=agent_id,
        trace_id=trace_id,
    )

    # DEMAND SHAPING (supply-side protection). Only the platform sees how many
    # OTHER agents just messaged this same business, so only we can stop a
    # flood. Over-budget requests are QUEUED with an honest retry_after, never
    # silently dropped, and the check FAILS OPEN so throttling can never break
    # delivery. Skipped entirely when the caller supplies no business_id.
    if request.business_id:
        try:
            from core.demand_shaping import check_budget
            # Size the budget to the business. Until now every business
            # got the same 'small' allowance - a one-chair barber and a
            # 50-seat restaurant treated identically.
            from core.business_tier import resolve_tier
            _tier, _tier_why = await resolve_tier(request.business_id)
            decision = await check_budget(request.business_id, tier=_tier)
            if not decision.allowed:
                # PERSIST the deferred request. We tell the agent it is "queued";
                # until this existed nothing stored it, so "queued" meant dropped
                # (audit 2026-08-26). The queue is what the digest renders from.
                queued_row = None
                try:
                    from core.demand_queue import enqueue
                    queued_row = await enqueue(
                        business_id=request.business_id,
                        business_number=request.recipient.id_value,
                        agent_id=agent_id,
                        end_user_ref=request.on_behalf_of,
                        intent=request.content.body[:200],
                        idempotency_key=getattr(request, "idempotency_key", None),
                    )
                except Exception:  # noqa: BLE001 - queueing must never block
                    pass
                shaping_block = decision.as_receipt_block()
                shaping_block["business_tier"] = _tier
                shaping_block["tier_basis"] = _tier_why
                if queued_row:
                    shaping_block["queued"] = True
                    shaping_block["queued_request_id"] = queued_row.get("request_id")
                    shaping_block["reference"] = queued_row.get("ref_token")
                else:
                    # Say so rather than implying a queue entry that isn't there.
                    shaping_block["queued"] = False
                return OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.FAILURE,
                    reason_code=decision.reason_code,
                    human_message=decision.human_message,
                    result={"demand_shaping": shaping_block},
                    cost=CostRecord(amount=0.0, currency="USD",
                                    basis="no_charge_rate_limited"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    retriable=True,
                    trace_id=trace_id,
                )
        except Exception:  # noqa: BLE001 - shaping must never block a send
            pass

    conversation: dict | None = None
    base_body = request.content.body

    for channel_name, adapter in chain:
        channel_request.channel = channel_name.split(":")[0]
        attempted.append(channel_name)
        # Two-way channel + a named end-user -> open a tracked conversation and
        # carry its reference in-message. That reference is what lets the
        # business's free-typed reply be matched back to THIS end-user rather
        # than guessed at (core/conversations.py).
        channel_request.content = base_body
        if channel_name == "whatsapp:cloud_api" and request.on_behalf_of:
            try:
                from core import conversations as _conv
                from core.number_pool import allocate as _allocate
                # Prefer a sender number with NO live thread to this business:
                # a unique pair makes a bare "yes" unambiguous, which is
                # structurally better than making the business quote a
                # reference. Falls back to today's behaviour with one number.
                alloc = await _allocate(request.recipient.id_value)
                our_number = (alloc.sender.number if alloc.sender
                              else os.getenv("WHATSAPP_PHONE_NUMBER", ""))
                if alloc.sender:
                    channel_request.metadata = {
                        **(channel_request.metadata or {}),
                        **alloc.sender.as_metadata(),
                    }
                conversation = await _conv.open_conversation(
                    agent_id=agent_id,
                    end_user_ref=request.on_behalf_of,
                    business_id=request.business_id,
                    business_number=request.recipient.id_value,
                    our_number=our_number,
                    intent=(request.content.subject or base_body)[:120],
                )
                # ACT on pair_conflict: with another live thread on this number
                # pair, a bare "yes" is unattributable, so ask for the reference
                # firmly. (Computing the flag but ignoring it was the real gap
                # both independent reviews caught.) The pool may have already
                # avoided the collision — but if it could not, or could not
                # prove it, we still demand the reference.
                contested = bool(conversation.get("pair_conflict")) or alloc.contested
                channel_request.content = base_body + _conv.reference_line(
                    conversation["ref_token"], request.on_behalf_of,
                    contested=contested)
            except Exception:  # noqa: BLE001 - threading must never block a send
                conversation = None
        try:
            resp = await adapter.send(channel_request)
            if resp.success:
                # The conversation belongs to the channel that OPENED it. If
                # WhatsApp opened a thread but then failed and SMS delivered
                # instead, binding the SMS id to that thread — and telling the
                # agent a reference the business never received — would be a
                # lie. Drop it. (found via a DeepSeek review pass, 2026-08-26)
                if conversation and channel_name != "whatsapp:cloud_api":
                    conversation = None
                if conversation and resp.provider_message_id:
                    try:
                        from core import conversations as _conv2
                        await _conv2.record_outbound(
                            conversation["conversation_id"],
                            resp.provider_message_id, channel_request.content)
                    except Exception:  # noqa: BLE001
                        pass
                cost_amount = _CHANNEL_COSTS.get(channel_name, 0.05)
                fallback_chain = [f"{c} (skipped)" for c in attempted[:-1]]
                increment_messages_sent()
                return OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="message_sent",
                    human_message=f"Message delivered via {channel_name}.",
                    result={
                        "provider_message_id": resp.provider_message_id,
                        **({"conversation": {
                            "conversation_id": conversation["conversation_id"],
                            "reference": conversation["ref_token"],
                            "note": ("The business's reply will be matched back to "
                                     "this conversation. Poll get_outcome or handle "
                                     "the inbound webhook to read it."),
                            **({"pair_conflict": conversation["pair_conflict"]}
                               if conversation.get("pair_conflict") else {}),
                        }} if conversation else {}),
                    },
                    cost=CostRecord(amount=cost_amount, currency="USD", basis="per_message"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel_used=channel_name,
                    channel_fallback_chain=fallback_chain,
                    retriable=False,
                    trace_id=trace_id,
                )
            last_error = resp.error_message or "upstream_failure"
        except ComplianceViolationError as cve:
            # Compliance violation — do NOT fall back, surface immediately
            return OutcomeReceipt(
                operation_id=operation_id,
                status=OperationStatus.FAILURE,
                reason_code="compliance_violation",
                human_message=cve.message,
                result={"rule": cve.rule, "jurisdiction": cve.jurisdiction},
                cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_compliance_block"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                channel_used=None,
                retriable=False,
                trace_id=trace_id,
            )
        except Exception as exc:
            last_error = str(exc)

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="upstream_failure",
        human_message=f"All channels failed. Last error: {last_error}",
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_delivery_failure"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        channel_fallback_chain=attempted,
        retriable=True,
        next_actions=["retry after 30s", "call escalate_to_human if urgent"],
        trace_id=trace_id,
    )
