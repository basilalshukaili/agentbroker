"""
Twilio SMS channel adapter.

Auth modes (checked in order of preference):
  API-Key mode (preferred):
    TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET + TWILIO_ACCOUNT_SID
    -> Client(api_key_sid, api_key_secret, account_sid)
  Legacy mode:
    TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN
    -> Client(account_sid, auth_token)
  Neither set -> honest channel_not_configured failure (nothing sent, nothing charged).

Sender resolution (checked in order of preference):
  TWILIO_MESSAGING_SERVICE_SID (preferred) -> messages.create(messaging_service_sid=...)
  TWILIO_FROM_NUMBER             -> messages.create(from_=...)
  Neither set -> honest channel_not_configured/no_sender failure (nothing sent, nothing charged).

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
        # API-Key auth (SK... key + secret + account SID)
        self._api_key_sid = os.getenv("TWILIO_API_KEY_SID", "")
        self._api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "")
        # Both auth modes need ACCOUNT_SID; legacy mode also needs AUTH_TOKEN
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        # Sender: Messaging Service SID is preferred over a raw From number
        self._messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
        self._from_number = os.getenv("TWILIO_FROM_NUMBER", "")

    def _auth_mode(self) -> str:
        """Return 'api_key', 'legacy', or 'none' based on which env vars are set."""
        if self._api_key_sid and self._api_key_secret and self._account_sid:
            return "api_key"
        if self._account_sid and self._auth_token:
            return "legacy"
        return "none"

    def _build_client(self):
        """Construct and return a Twilio REST Client for the detected auth mode."""
        from twilio.rest import Client  # type: ignore
        mode = self._auth_mode()
        if mode == "api_key":
            # API-Key auth: positional args are (username=api_key_sid, password=api_key_secret,
            # account_sid=account_sid) — Twilio SDK convention for key-based auth.
            return Client(self._api_key_sid, self._api_key_secret, self._account_sid)
        if mode == "legacy":
            return Client(self._account_sid, self._auth_token)
        return None

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

        if self._auth_mode() == "none":
            from channels.stub_policy import stubs_allowed, not_configured
            if not stubs_allowed():
                return not_configured(
                    "sms",
                    "twilio",
                    "TWILIO_ACCOUNT_SID + TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET "
                    "(API-Key mode) or TWILIO_AUTH_TOKEN (legacy mode)",
                )
            # Stub mode for testing (ALLOW_STUB_CHANNELS only — never in production)
            return ChannelResponse(
                success=True,
                provider_message_id=f"SM_STUB_{request.recipient_id[:8]}",
                raw_response={"stub": True},
            )

        # Auth is configured — now check that a sender is set
        if not self._messaging_service_sid and not self._from_number:
            from channels.stub_policy import not_configured
            return not_configured(
                "sms",
                "twilio",
                "TWILIO_MESSAGING_SERVICE_SID or TWILIO_FROM_NUMBER (no sender configured)",
            )

        try:
            # Production path: Twilio REST API.
            #
            # The Twilio SDK is SYNCHRONOUS (requests-based) and its default
            # http client carries timeout=None. Called inline from this async
            # method it blocked the single uvicorn worker's entire event loop
            # for the full round trip - indefinitely on a network stall. So:
            # a bounded timeout on the SDK client, and the blocking call moved
            # off the loop with asyncio.to_thread.
            import asyncio as _asyncio
            client = self._build_client()
            try:
                from twilio.http.http_client import TwilioHttpClient as _THC
                client.http_client = _THC(timeout=15)
            except Exception:  # noqa: BLE001 - SDK layout change: keep the
                pass           # send working; to_thread still frees the loop
            create_kwargs: dict = dict(body=request.content, to=request.recipient_id)
            if self._messaging_service_sid:
                create_kwargs["messaging_service_sid"] = self._messaging_service_sid
            else:
                create_kwargs["from_"] = self._from_number
            message = await _asyncio.to_thread(
                client.messages.create, **create_kwargs)
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
        return self._auth_mode() != "none"
