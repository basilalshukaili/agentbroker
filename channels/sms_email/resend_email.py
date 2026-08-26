"""
Resend email channel adapter — replaces SendGrid.

Why Resend:
  - 3,000 emails/month free (vs 100/day on SendGrid free)
  - Works globally including Oman/Middle East
  - Modern HTTP API (no SMTP), no phone-verification gate
  - 30-second signup, instant API key

Sign up: https://resend.com
The first email-from-domain you verify becomes RESEND_FROM_EMAIL.
Until then, `onboarding@resend.dev` works for testing (rate-limited).

All sends pass through compliance.pre_check before dispatch.
CAN-SPAM (US-bound) appends an unsubscribe footer + physical address.
GDPR (EU-bound) requires explicit consent for marketing — checked by pre_check.
"""
from __future__ import annotations

import os
from typing import Optional

from channels.adapter_interface import ChannelAdapter, ChannelRequest, ChannelResponse
from compliance.pre_check import pre_check


_UNSUBSCRIBE_FOOTER = "\n\n---\nTo unsubscribe, reply UNSUBSCRIBE or click: {unsubscribe_url}"
_PHYSICAL_ADDRESS_DEFAULT = (
    "HatchLoop - postal address on request: hello@hatchloop.dev"
)


class ResendEmailAdapter(ChannelAdapter):
    channel_name = "email:resend"

    def __init__(self) -> None:
        self._api_key = os.getenv("RESEND_API_KEY", "")
        self._from_email = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
        self._physical_address = os.getenv(
            "BUSINESS_PHYSICAL_ADDRESS", _PHYSICAL_ADDRESS_DEFAULT,
        )
        # No static URL: the link is signed PER RECIPIENT so one click can
        # identify who to remove without a login. The old default pointed at
        # your-domain.example - a domain that does not exist, so every
        # commercial email carried a dead opt-out (fixed 2026-08-26).
        self._unsubscribe_override = os.getenv("BUSINESS_UNSUBSCRIBE_URL", "")

    def _unsubscribe_headers(self, request: ChannelRequest) -> dict:
        """List-Unsubscribe headers for commercial mail (RFC 8058).

        Returns {} for transactional mail: a booking confirmation is not
        something to unsubscribe from, and offering it there trains people to
        opt out of messages they asked for.
        """
        if request.message_type not in ("marketing", "follow_up", "notification"):
            return {}
        try:
            from agent_interface.unsubscribe import unsubscribe_url as _unsub
            link = self._unsubscribe_override or _unsub(request.recipient_id, "email")
        except Exception:  # noqa: BLE001 - headers must never break a send
            return {}
        return {"headers": {
            "List-Unsubscribe": f"<{link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }}

    async def send(self, request: ChannelRequest) -> ChannelResponse:
        # Compliance pre-check — required before any send
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
        # CAN-SPAM (US) + similar laws — append unsubscribe + physical address for marketing
        if request.message_type in ("marketing", "follow_up", "notification"):
            from agent_interface.unsubscribe import unsubscribe_url as _unsub
            link = self._unsubscribe_override or _unsub(request.recipient_id, "email")
            body += _UNSUBSCRIBE_FOOTER.format(unsubscribe_url=link)
            body += f"\n{self._physical_address}"

        if not self._api_key:
            from channels.stub_policy import stubs_allowed, not_configured
            if not stubs_allowed():
                return not_configured("email", "resend", "RESEND_API_KEY")
            # Stub mode — synthetic success for tests (ALLOW_STUB_CHANNELS only)
            return ChannelResponse(
                success=True,
                provider_message_id=f"RESEND_STUB_{request.recipient_id[:8]}",
                raw_response={"stub": True},
            )

        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self._from_email,
                        "to": [request.recipient_id],
                        "subject": request.subject or "Message from your service provider",
                        "text": body,
                        # RFC 8058. Gmail and Yahoo require these of bulk
                        # senders and filter mail without them, so a footer
                        # link alone is not enough - the client needs a
                        # machine-readable opt-out it can action in one click.
                        **self._unsubscribe_headers(request),
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                return ChannelResponse(
                    success=True,
                    provider_message_id=data.get("id"),
                    raw_response=data,
                )
        except Exception as exc:
            return ChannelResponse(
                success=False,
                error_code="upstream_failure",
                error_message=str(exc),
            )

    async def health_check(self) -> bool:
        return bool(self._api_key)
