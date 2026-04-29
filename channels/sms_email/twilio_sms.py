"""
Twilio SMS channel adapter.
Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID env vars.
All sends pass through compliance.pre_check before dispatch.
"""
from __future__ import annotations

import os
from typing import Optional

from channels.adapter_interface import ChannelAdapter, ChannelRequest, ChannelResponse
from compliance.pre_check import pre_check
from core.models import ComplianceViolationError


class TwilioSMSAdapter(ChannelAdapter):
    channel_name = "sms:twilio"

    def __init__(self) -> None:
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self._messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")

    async def send(self, request: ChannelRequest) -> ChannelResponse:
        # Compliance pre-check — MUST run before any dispatch
        pre_check(
            recipient_id=request.recipient_id,
            channel="sms",
            message_type=request.message_type,
            content=request.content,
            country_code=request.country_code,
            state_code=request.state_code,
            agent_id=request.agent_id,
            trace_id=request.trace_id,
        )

        if not self._account_sid or not self._auth_token:
            # Stub mode for testing
            return ChannelResponse(
                success=True,
                provider_message_id=f"SM_STUB_{request.recipient_id[:8]}",
                raw_response={"stub": True},
            )

        try:
            # Production path: Twilio REST API
            from twilio.rest import Client  # type: ignore
            client = Client(self._account_sid, self._auth_token)
            message = client.messages.create(
                body=request.content,
                messaging_service_sid=self._messaging_service_sid,
                to=request.recipient_id,
            )
            return ChannelResponse(
                success=message.status not in ("failed", "undelivered"),
                provider_message_id=message.sid,
                raw_response={"status": message.status, "sid": message.sid},
            )
        except Exception as exc:
            return ChannelResponse(
                success=False,
                error_code="upstream_failure",
                error_message=str(exc),
            )

    async def health_check(self) -> bool:
        return bool(self._account_sid and self._auth_token)
