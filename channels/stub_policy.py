"""Stub policy — NEVER report success for work we did not do.

Before 2026-08-04 every channel adapter returned success=True with a synthetic
`*_STUB_*` id when its API key was missing. In production that meant real
visiting agents (Smithery traffic) received fabricated delivery receipts with
fabricated costs for messages that were never sent. That is a lie to a customer
and it poisons every metric built on top of it.

Policy now:
  - Missing credentials => honest failure (`channel_not_configured`).
  - Synthetic success is available ONLY when ALLOW_STUB_CHANNELS is explicitly
    truthy (local tests / simulation harness), never by default.
"""
import os

_TRUTHY = {"1", "true", "yes", "on"}


def stubs_allowed() -> bool:
    """True only when a human explicitly enabled synthetic responses."""
    return os.getenv("ALLOW_STUB_CHANNELS", "").strip().lower() in _TRUTHY


def not_configured(channel: str, provider: str, missing: str = "API key"):
    """The honest response for an unconfigured channel."""
    from channels.adapter_interface import ChannelResponse
    return ChannelResponse(
        success=False,
        provider_message_id=None,
        error_code="channel_not_configured",
        error_message=(
            f"{channel} via {provider} is not configured on this deployment "
            f"({missing} missing) — nothing was sent and nothing was charged."
        ),
        raw_response={"configured": False, "channel": channel, "provider": provider},
    )
