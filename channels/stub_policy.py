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
  - HARD PROD GUARD (FIX 4): when the RENDER env var is set, or
    ENVIRONMENT/ENV=production, stubs are ALWAYS disabled regardless of
    ALLOW_STUB_CHANNELS. This prevents stub receipts from ever appearing in
    production even if someone accidentally sets ALLOW_STUB_CHANNELS on Render.
"""
import os

_TRUTHY = {"1", "true", "yes", "on"}


def _is_production() -> bool:
    """True when running in a production environment (Render, explicit flag, etc.)."""
    # Render sets this env var automatically on all services
    if os.getenv("RENDER"):
        return True
    env = os.getenv("ENVIRONMENT", os.getenv("ENV", "")).strip().lower()
    return env in ("production", "prod")


def stubs_allowed() -> bool:
    """True only when a human explicitly enabled synthetic responses AND not in production."""
    if _is_production():
        # Hard guard: stubs can never be enabled in production
        return False
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
