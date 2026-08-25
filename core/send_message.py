"""
send_message — core operation handler.
Routes outbound messages through the best available channel with automatic fallback.
All paths pass through compliance.pre_check before dispatch.
"""
from __future__ import annotations

import time
import uuid

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

    for channel_name, adapter in chain:
        channel_request.channel = channel_name.split(":")[0]
        attempted.append(channel_name)
        try:
            resp = await adapter.send(channel_request)
            if resp.success:
                cost_amount = _CHANNEL_COSTS.get(channel_name, 0.05)
                fallback_chain = [f"{c} (skipped)" for c in attempted[:-1]]
                increment_messages_sent()
                return OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="message_sent",
                    human_message=f"Message delivered via {channel_name}.",
                    result={"provider_message_id": resp.provider_message_id},
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
