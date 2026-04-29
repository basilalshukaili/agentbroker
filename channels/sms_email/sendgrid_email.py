"""
SendGrid email channel adapter.
Requires: SENDGRID_API_KEY, SENDGRID_FROM_EMAIL env vars.
All sends pass through compliance.pre_check before dispatch.
CAN-SPAM: unsubscribe link is appended to all commercial emails automatically.
"""
from __future__ import annotations

import os

from channels.adapter_interface import ChannelAdapter, ChannelRequest, ChannelResponse
from compliance.pre_check import pre_check

_UNSUBSCRIBE_FOOTER = "\n\n---\nTo unsubscribe from these emails, reply UNSUBSCRIBE or click here: {unsubscribe_url}"
_PHYSICAL_ADDRESS = "SMB Broker Inc., 123 Main St, Suite 100, Anytown, US 00000"


class SendGridEmailAdapter(ChannelAdapter):
    channel_name = "email:sendgrid"

    def __init__(self) -> None:
        self._api_key = os.getenv("SENDGRID_API_KEY", "")
        self._from_email = os.getenv("SENDGRID_FROM_EMAIL", "noreply@smb-broker.example")

    async def send(self, request: ChannelRequest) -> ChannelResponse:
        pre_check(
            recipient_id=request.recipient_id,
            channel="email",
            message_type=request.message_type,
            content=request.content,
            country_code=request.country_code,
            state_code=request.state_code,
            agent_id=request.agent_id,
            trace_id=request.trace_id,
        )

        body = request.content
        # CAN-SPAM: append unsubscribe footer + physical address for commercial emails
        if request.message_type in ("marketing", "follow_up", "notification"):
            body += _UNSUBSCRIBE_FOOTER.format(unsubscribe_url="https://smb-broker.example/unsubscribe")
            body += f"\n{_PHYSICAL_ADDRESS}"

        if not self._api_key:
            return ChannelResponse(
                success=True,
                provider_message_id=f"EMAIL_STUB_{request.recipient_id[:8]}",
                raw_response={"stub": True},
            )

        try:
            import sendgrid  # type: ignore
            from sendgrid.helpers.mail import Mail  # type: ignore
            sg = sendgrid.SendGridAPIClient(api_key=self._api_key)
            message = Mail(
                from_email=self._from_email,
                to_emails=request.recipient_id,
                subject=request.subject or "Message from your service provider",
                plain_text_content=body,
            )
            response = sg.send(message)
            success = response.status_code in (200, 202)
            return ChannelResponse(
                success=success,
                provider_message_id=response.headers.get("X-Message-Id"),
                raw_response={"status_code": response.status_code},
            )
        except Exception as exc:
            return ChannelResponse(
                success=False,
                error_code="upstream_failure",
                error_message=str(exc),
            )

    async def health_check(self) -> bool:
        return bool(self._api_key)
