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
from billing.pricing import receipt_usd as _receipt_usd

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
        # Fall back to listing the data fields in a readable way rather than
        # exposing a raw Python dict repr as the email body.
        lines = [f"  {k}: {v}" for k, v in data.items() if v is not None]
        return "\n".join(lines) if lines else "(no details provided)"


_CONFIRMATION_TYPE_LABELS: dict[str, str] = {
    "booking_confirmation": "Booking Confirmation",
    "cancellation_notice": "Appointment Cancellation Notice",
    "payment_receipt": "Payment Receipt",
    "otp": "Your Verification Code",
    "reminder": "Appointment Reminder",
}


def _build_subject(confirmation_type: str, data: dict) -> str:
    """Build a specific, descriptive email subject line."""
    label = _CONFIRMATION_TYPE_LABELS.get(confirmation_type, "Confirmation")
    # Try to enrich subject with business name and/or time
    smb = data.get("smb_name") or data.get("business_name") or ""
    when = data.get("appointment_time") or data.get("date") or ""
    if smb and when:
        return f"{label}: {smb} on {when}"
    if smb:
        return f"{label}: {smb}"
    if when:
        return f"{label} on {when}"
    return label


def _build_email_body(confirmation_type: str, data: dict, core_text: str) -> str:
    """
    Format a proper plain-text confirmation email.

    Structure:
      Hello <name>,

      <core confirmation text>

      If you have questions, please reply to this message.

      Best regards,
      SMB Agent Broker
      ---
      This is an automated transactional message sent on behalf of <smb_name>.
    """
    name = data.get("name") or data.get("customer_name") or ""
    greeting = f"Hello {name}," if name else "Hello,"

    smb = data.get("smb_name") or data.get("business_name") or ""
    footer_from = f"on behalf of {smb}" if smb else "via SMB Agent Broker"

    lines = [
        greeting,
        "",
        core_text,
        "",
        "If you have any questions, please reply to this message.",
        "",
        "Best regards,",
        "SMB Agent Broker",
        "",
        "---",
        f"This is an automated transactional message sent {footer_from}.",
        "To stop receiving messages, reply STOP.",
    ]
    return "\n".join(lines)


async def handle_send_transactional_confirmation(
    request: SendTransactionalConfirmationRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())
    core_text = _render(request.confirmation_type.value, request.data)
    recipient = request.recipient.phone_or_email

    is_email = "@" in recipient
    if is_email:
        adapter, channel_name = _get_email_adapter()
        # For email: format a proper plain-text body and a specific subject.
        body = _build_email_body(request.confirmation_type.value, request.data, core_text)
        subject = _build_subject(request.confirmation_type.value, request.data)
    else:
        adapter, channel_name = _get_sms_adapter()
        # For SMS: use the concise rendered template directly (no email wrapper).
        body = core_text
        subject = None

    channel_req = ChannelRequest(
        recipient_id=recipient,
        channel="email" if is_email else "sms",
        message_type="transactional",
        content=body,
        subject=subject,
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
                cost=CostRecord(amount=_receipt_usd("send_transactional_confirmation"), currency="USD", basis="per_message"),  # FIX 4: manifest price
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
