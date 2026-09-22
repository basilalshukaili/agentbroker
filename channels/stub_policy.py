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
  - HARD PROD GUARD (FIX 4): when the RENDER env var is set, or ENVIRONMENT is
    anything other than exactly "development", stubs are ALWAYS disabled
    regardless of ALLOW_STUB_CHANNELS.

WHY THE PROD GUARD FAILS CLOSED ON AN UNSET ENVIRONMENT (2026-09-22, board row
250 follow-up). It used to fail OPEN: production meant RENDER set, or
ENVIRONMENT in ("production", "prod"), so unset/empty/"staging"/"Production "
all read as NOT production. That is the same direction of ambiguity that had
just been fixed in three security guards (billing/receipt_signer.py,
agent_interface/identity.py, agent_interface/unsubscribe.py — see
core/env_guard.py).

core/env_guard.py argues this helper should KEEP failing open, on the grounds
that ALLOW_STUB_CHANNELS is itself a sufficient second gate. That argument was
examined and rejected, for three reasons:

  1. IT IS CIRCULAR. This guard's whole stated purpose — see FIX 4 above and
     its original comment, "even if someone accidentally sets
     ALLOW_STUB_CHANNELS on Render" — is to catch the case where the
     ALLOW_STUB_CHANNELS opt-in has leaked onto a production host. If that
     opt-in were a sufficient gate, FIX 4 would never have needed writing.
     A guard that exists because flag A leaks cannot cite flag A as the reason
     it may be lax about flag B.

  2. THE TWO FLAGS ARE NOT INDEPENDENT. Defence in depth only buys anything
     when the layers fail for unrelated reasons. Both of these fail for one
     reason: a host nobody configured carefully. The operator who leaves
     ALLOW_STUB_CHANNELS=1 in a shell or container that then faces real
     traffic is the same operator whose ENVIRONMENT was never set. The
     scenario is one mistake, not two.

  3. THE COSTS ARE WILDLY ASYMMETRIC. Failing closed when we should not:
     a developer gets an honest `channel_not_configured` error, sees it
     immediately, and sets ENVIRONMENT=development (which .env.example has
     shipped since day one). Failing open when we should not: a real customer
     message is silently never sent, and they are handed a fabricated delivery
     receipt with a fabricated cost — the exact lie this module exists to
     prevent. Loud-and-wrong is recoverable; silent-and-wrong is what this
     file was written about.

WHAT THE BELT-AND-BRACES ACTUALLY LOOKS LIKE TODAY (verified, not assumed).
Every documented production deployment does set ENVIRONMENT=production —
deploy/render.yaml, deploy/fly.toml, deploy/koyeb.yaml, and both Dockerfiles
bake `ENV ENVIRONMENT=production` (since 2026-06-02, ab3f7a9). The live VPS
container `techmate-agentbroker` that serves api.hatchloop.dev inherits it
from the image; a read-only `docker inspect` on 2026-09-21 confirmed
ENVIRONMENT present in its Config.Env. So this change is not fixing a live
hole — the guard already fires on every real host. It removes the reliance on
that staying true. Note also that RENDER is a host-specific signal and
production has already moved off Render onto the VPS, where RENDER is unset;
there, ENVIRONMENT is the only signal left, and a single signal must not be
the lax one.

WHY THIS DOES NOT IMPORT core/env_guard.py. The two now give the same answer,
but they answer different questions — "may we fabricate a successful send?"
versus "is this secret forgeable?" — and they must stay independently
changeable. If someone later has cause to loosen the security guards (say,
admitting "staging" as non-production), that must not silently re-enable
fabricated delivery receipts as a side effect. Sharing the spelling of the
answer is deliberate; sharing the decision is not.
"""
import os

_TRUTHY = {"1", "true", "yes", "on"}

# The ONLY spelling that opts a run out of "assume production". Deliberately
# the same spelling core/env_guard.py accepts: two different answers to "is
# this production?" inside one codebase is itself a drift bug. Anything else —
# unset, empty, "staging", "testing", a typo, mixed case with stray
# whitespace — is treated as production. Silence is not consent.
_EXPLICIT_NON_PRODUCTION = "development"


def _is_production() -> bool:
    """True unless ENVIRONMENT is exactly "development" (case/space insensitive).

    The legacy `ENV` fallback was dropped with the fail-closed change. Under
    the old fail-open rule ENV could push the answer either way; under this
    one it could only ever LOOSEN it (ENV=development with ENVIRONMENT unset
    would re-open the hole just closed), so it is a second spelling of the
    escape hatch and nothing in the repo or any deploy config sets it.
    Dropping it can never make the answer less production: a host that used
    to be caught via ENV=production is now caught by ENVIRONMENT not being
    "development".
    """
    # Render sets this env var automatically on all services
    if os.getenv("RENDER"):
        return True
    return os.getenv("ENVIRONMENT", "").strip().lower() != _EXPLICIT_NON_PRODUCTION


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
