"""
WhatsApp channel adapter — Meta WhatsApp Cloud API (graph.facebook.com).

Honesty rules (house invariants):
  - compliance.pre_check runs BEFORE any dispatch (channel="whatsapp").
  - Not configured -> is_available False + honest not_configured failure.
  - Cloud API free-form text only delivers inside the 24h service window after
    the user last messaged us. Outside it Meta requires an APPROVED TEMPLATE;
    sending a generic template instead of the caller's content would be a
    content lie, so we FAIL HONESTLY with needs_template instead.

Config: WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_ID (WHATSAPP_WABA_ID for
template management). Test-mode numbers deliver only to Meta-registered
test recipients.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from channels.adapter_interface import ChannelAdapter, ChannelRequest, ChannelResponse
from compliance.pre_check import pre_check

_GRAPH = "https://graph.facebook.com/v21.0"

# Meta error codes that mean "outside the 24h service window / needs template"
_NEEDS_TEMPLATE_CODES = {131047, 131026}


def _digits(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


class WhatsAppCloudAdapter(ChannelAdapter):
    channel_name = "whatsapp:cloud_api"

    def __init__(self) -> None:
        self._token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        self._phone_id = os.getenv("WHATSAPP_PHONE_ID", "")
        self.is_available = bool(self._token and self._phone_id)

    async def send(self, request: ChannelRequest) -> ChannelResponse:
        # Compliance gate first — non-bypassable, same as every channel.
        pre_check(
            recipient_id=request.recipient_id,
            channel="whatsapp",
            message_type=request.message_type,
            content=request.content,
            country_code=request.country_code,
            state_code=request.state_code,
            agent_id=request.agent_id,
            trace_id=request.trace_id,
        )

        if not self.is_available:
            return ChannelResponse(
                success=False,
                error_code="not_configured",
                error_message=(
                    "WhatsApp channel not configured "
                    "(WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_ID missing)."
                ),
            )

        to = _digits(request.recipient_id)
        if not to:
            return ChannelResponse(
                success=False, error_code="invalid_recipient",
                error_message="Recipient must be a phone number for WhatsApp.",
            )

        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": request.content[:4096]},
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_GRAPH}/{self._phone_id}/messages",
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json"},
                    json=body,
                )
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("messages"):
                return ChannelResponse(
                    success=True,
                    provider_message_id=data["messages"][0].get("id"),
                    raw_response=data,
                )
            err = (data.get("error") or {})
            code: Optional[int] = err.get("code")
            if code == 190:
                # Meta's Getting-Started token lasts ~24h. Name it precisely so
                # it is never mistaken for a delivery problem (2026-08-26).
                return ChannelResponse(
                    success=False, error_code="whatsapp_token_expired",
                    error_message=(
                        "WhatsApp access token expired or invalid. A permanent "
                        "System User token is required (temporary tokens last 24h)."
                    ),
                    raw_response=data,
                )
            if code in _NEEDS_TEMPLATE_CODES:
                return ChannelResponse(
                    success=False, error_code="needs_template",
                    error_message=(
                        "Recipient is outside WhatsApp's 24h service window - "
                        "business-initiated messages need an approved template. "
                        "Ask the recipient to message us first, or use a template."
                    ),
                    raw_response=data,
                )
            return ChannelResponse(
                success=False,
                error_code=f"whatsapp_{code or resp.status_code}",
                error_message=(err.get("message") or f"HTTP {resp.status_code}")[:300],
                raw_response=data,
            )
        except Exception as exc:  # noqa: BLE001
            return ChannelResponse(
                success=False, error_code="whatsapp_unreachable",
                error_message=str(exc)[:200],
            )
