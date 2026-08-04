"""
Vapi voice AI channel adapter.
Requires: VAPI_API_KEY env var.
Outbound voice calls pass through compliance.pre_check before dispatch.
Recording consent is handled per jurisdiction before recording begins.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from channels.adapter_interface import ChannelAdapter, ChannelRequest, ChannelResponse
from compliance.pre_check import pre_check
from compliance.recording_consent import (
    check_recording_consent_required,
    CONSENT_PROMPT_TEXT,
)


class VapiVoiceAdapter(ChannelAdapter):
    channel_name = "voice_ai:vapi"

    def __init__(self) -> None:
        self._api_key = os.getenv("VAPI_API_KEY", "")
        self._base_url = "https://api.vapi.ai"

    async def send(self, request: ChannelRequest) -> ChannelResponse:
        pre_check(
            recipient_id=request.recipient_id,
            channel="voice",
            message_type=request.message_type,
            content=request.content,
            country_code=request.country_code,
            state_code=request.state_code,
            agent_id=request.agent_id,
            trace_id=request.trace_id,
        )

        call_id = str(uuid.uuid4())

        # Determine recording consent requirements
        recording_status = check_recording_consent_required(
            call_id=call_id,
            country_code=request.country_code or "US",
            state_code=request.state_code,
            agent_id=request.agent_id,
            trace_id=request.trace_id,
        )

        # Build the call script — prepend consent prompt for two-party states
        script = request.content
        if recording_status.required:
            script = CONSENT_PROMPT_TEXT + " " + script

        if not self._api_key:
            from channels.stub_policy import stubs_allowed, not_configured
            if not stubs_allowed():
                return not_configured("voice", "vapi", "VAPI_API_KEY")
            return ChannelResponse(
                success=True,
                provider_message_id=f"VAPI_STUB_{call_id[:8]}",
                raw_response={"stub": True, "call_id": call_id},
            )

        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/call/phone",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "phoneNumberId": os.getenv("VAPI_PHONE_NUMBER_ID", ""),
                        "customer": {"number": request.recipient_id},
                        "assistant": {
                            "firstMessage": script,
                            "recordingEnabled": not recording_status.required,
                        },
                        "metadata": {"call_id": call_id, "trace_id": request.trace_id},
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                return ChannelResponse(
                    success=True,
                    provider_message_id=data.get("id", call_id),
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
