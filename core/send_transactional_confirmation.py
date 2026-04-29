"""
send_transactional_confirmation — core operation handler.
Idempotent transactional messages: OTPs, booking confirmations, receipts.
"""
from __future__ import annotations

import time
import uuid

from core.models import (
    SendTransactionalConfirmationRequest, OutcomeReceipt, OperationStatus,
    CostRecord, ChannelPreference, ComplianceViolationError
)
from channels.sms_email.twilio_sms import TwilioSMSAdapter
from channels.sms_email.sendgrid_email import SendGridEmailAdapter
from channels.adapter_interface import ChannelRequest

_TEMPLATES = {
    "booking_confirmation": "Hi {name}, your appointment at {smb_name} is confirmed for {appointment_time}. Address: {address}. Reply STOP to unsubscribe.",
    "cancellation_notice": "Hi {name}, your appointment at {smb_name} on {appointment_time} has been cancelled. {refund_note}",
    "payment_receipt": "Hi {name}, payment of {amount} received. Ref: {reference_id}. Thank you!",
    "otp": "Your verification code is {otp_code}. Valid for 10 minutes. Do not share.",
    "reminder": "Hi {name}, reminder: {reminder_text}. Reply STOP to unsubscribe.",
}

_SMS = TwilioSMSAdapter()
_EMAIL = SendGridEmailAdapter()


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
    primary_channel = "email" if is_email else "sms"
    adapter = _EMAIL if is_email else _SMS
    channel_name = "email:sendgrid" if is_email else "sms:twilio"

    channel_req = ChannelRequest(
        recipient_id=recipient,
        channel=primary_channel,
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
                cost=CostRecord(amount=0.03, currency="USD", basis="per_message"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                channel_used=channel_name,
                retriable=False,
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

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="upstream_failure",
        human_message="Confirmation delivery failed.",
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
        retriable=True,
        trace_id=trace_id,
    )
