"""
send_transactional_confirmation — core operation handler.
Idempotent transactional messages: OTPs, booking confirmations, receipts.

FIX 2 (2026-08-23):
  - Switched email adapter from SendGridEmailAdapter (no API key set) to
    ResendEmailAdapter — same adapter used by send_message, which is confirmed
    working.  Root cause of upstream_failure was a missing SENDGRID_API_KEY
    causing the SendGrid adapter to return channel_not_configured.
  - Error path now surfaces the adapter name and its error code/message so
    callers get actionable diagnostics instead of the opaque
    "Confirmation delivery failed." message.
  - Cost on success aligned to manifest: $0.02 (was $0.03).
"""
from __future__ import annotations

import os
import time
import uuid

from core.models import (
    SendTransactionalConfirmationRequest, OutcomeReceipt, OperationStatus,
    CostRecord, ComplianceViolationError
)
from channels.adapter_interface import ChannelRequest

_TEMPLATES = {
    "booking_confirmation": "Hi {name}, your appointment at {smb_name} is confirmed for {appointment_time}. Address: {address}. Reply STOP to unsubscribe.",
    "cancellation_notice": "Hi {name}, your appointment at {smb_name} on {appointment_time} has been cancelled. {refund_note}",
    "payment_receipt": "Hi {name}, payment of {amount} received. Ref: {reference_id}. Thank you!",
    "otp": "Your verification code is {otp_code}. Valid for 10 minutes. Do not share.",
    "reminder": "Hi {name}, reminder: {reminder_text}. Reply STOP to unsubscribe.",
}

# FIX 2: Use Resend (the configured email provider) instead of SendGrid.
# Mirror the same adapter-selection logic as send_message.py so both tools
# use the same live channel.
def _get_email_adapter():
    from channels.sms_email.resend_email import ResendEmailAdapter
    from channels.sms_email.sendgrid_email import SendGridEmailAdapter
    if os.getenv("RESEND_API_KEY"):
        return ResendEmailAdapter(), "email:resend"
    if os.getenv("SENDGRID_API_KEY"):
        return SendGridEmailAdapter(), "email:sendgrid"
    # Default to Resend (will fail honestly with channel_not_configured if no key)
    return ResendEmailAdapter(), "email:resend"


def _get_sms_adapter():
    from channels.sms_email.twilio_sms import TwilioSMSAdapter
    return TwilioSMSAdapter(), "sms:twilio"


def _render(confirmation_type: str, data: dict) -> str:
    template = _TEMPLATES.get(confirmation_type, "{body}")
    try:
        return template.format(**data)
    except KeyError:
        return str(data)


async def handle_send_transactional_confirmation(
    request: SendTransactionalConfirmationRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())
    body = _render(request.confirmation_type.value, request.data)
    recipient = request.recipient.phone_or_email

    is_email = "@" in recipient
    if is_email:
        adapter, channel_name = _get_email_adapter()
    else:
        adapter, channel_name = _get_sms_adapter()

    channel_req = ChannelRequest(
        recipient_id=recipient,
        channel="email" if is_email else "sms",
        message_type="transactional",
        content=body,
        agent_id=agent_id,
        trace_id=trace_id,
    )

    try:
        resp = await adapter.send(channel_req)
        if resp.success:
            return OutcomeReceipt(
                operation_id=operation_id,
                status=OperationStatus.SUCCESS,
                reason_code="confirmation_sent",
                human_message=f"{request.confirmation_type.value} sent via {channel_name}.",
                result={"provider_message_id": resp.provider_message_id},
                cost=CostRecord(amount=0.02, currency="USD", basis="per_message"),  # FIX 4: manifest price
                latency_ms=int((time.monotonic() - t0) * 1000),
                channel_used=channel_name,
                retriable=False,
                trace_id=trace_id,
            )
        # Adapter returned success=False — surface the provider's own diagnostics
        err_code = getattr(resp, "error_code", "unknown")
        err_msg = getattr(resp, "error_message", "no detail")
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="upstream_failure",
            human_message=(
                f"Confirmation delivery failed via {channel_name}: "
                f"[{err_code}] {err_msg}"
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            channel_used=channel_name,
            retriable=True,
            trace_id=trace_id,
        )
    except ComplianceViolationError as cve:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="compliance_violation",
            human_message=cve.message,
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            retriable=False,
            trace_id=trace_id,
        )
    except Exception as exc:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="upstream_failure",
            human_message=(
                f"Confirmation delivery failed via {channel_name}: "
                f"[exception:{type(exc).__name__}] {exc}"
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            channel_used=channel_name,
            retriable=True,
            trace_id=trace_id,
        )
