"""
Channel fallback orchestrator.
Executes the fallback chain for an operation, recording circuit breaker outcomes.
Voice → SMS → Email → Human is the canonical escalation path.
"""
from __future__ import annotations

from typing import Any, Callable, Awaitable

from reliability.circuit_breaker import get_breaker


_FALLBACK_CHAINS: dict[str, list[str]] = {
    "schedule_appointment": ["direct_api:calcom", "voice_ai:vapi", "web_form", "escalate_to_human"],
    "send_message":         ["sms:twilio", "email:resend", "voice_ai:vapi"],
    "verify_business":      ["direct_api", "web_scrape", "voice_ai:vapi"],
    "capture_lead":         ["direct_api:crm", "email:resend", "sms:twilio", "web_form"],
    "send_transactional_confirmation": ["sms:twilio", "email:resend"],
    "handle_inbound":       ["direct_api:webhook", "email_polling", "escalate_to_human"],
    "escalate_to_human":    ["direct_api:ticketing", "email:resend", "sms:twilio"],
}


def get_fallback_chain(operation: str, smb_channels: list[str]) -> list[str]:
    """
    Return the ordered fallback chain for an operation, filtered to channels the SMB supports.
    """
    canonical = _FALLBACK_CHAINS.get(operation, ["sms:twilio", "email:resend"])
    available = [ch for ch in canonical if any(
        smb_ch.startswith(ch.split(":")[0]) for smb_ch in smb_channels
    )]
    # Always append human escalation as last resort if not already present
    if "escalate_to_human" not in available:
        available.append("escalate_to_human")
    return available


async def execute_with_fallback(
    channel_chain: list[str],
    fn_for_channel: Callable[[str], Awaitable[Any]],
) -> tuple[Any, str, list[str]]:
    """
    Try each channel in order. Returns (result, channel_used, fallback_chain_log).
    Raises RuntimeError if all channels fail.
    """
    attempted: list[str] = []
    last_error: str = ""

    for channel in channel_chain:
        breaker = get_breaker(channel)
        if not breaker.is_available():
            attempted.append(f"{channel} (circuit_open)")
            continue
        try:
            result = await fn_for_channel(channel)
            breaker.record_success()
            fallback_log = [f"{c}" for c in attempted]
            return result, channel, fallback_log
        except Exception as exc:
            breaker.record_failure()
            attempted.append(f"{channel} ({type(exc).__name__})")
            last_error = str(exc)

    raise RuntimeError(
        f"All channels in fallback chain failed. Attempted: {attempted}. Last error: {last_error}"
    )
