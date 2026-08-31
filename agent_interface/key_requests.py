"""
FastAPI router for the email-verified free API key flow.

POST /keys/request  {email}
  -> stores pending verification row in Supabase `pending_keys`
  -> sends a signed verification link via Resend
  -> returns 200 + instructions (never leaks whether the email already exists)

GET /keys/verify?token=<signed_token>
  -> validates the token, mints a free-tier JWT (tier='free', 90d)
  -> emails the key and returns an HTML confirmation page

Pure logic (token signing, rate limiting) lives in key_request_logic.py
so it can be unit-tested without importing FastAPI.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from agent_interface.key_request_logic import (
    make_verify_token,
    verify_token,
    store_pending,
    consume_pending,
    send_verification_email,
    send_key_email,
    html_error,
    html_success,
    verify_machine_signature,
    store_machine_minted,
    # Re-export for mcp_server.py to import from here (backward compat)
    is_free_key,
    consume_free_daily,
    get_free_daily_remaining,
)

logger = logging.getLogger("smb_broker.key_requests")

router = APIRouter(prefix="/keys", tags=["Free Keys"])

_FREE_TIER_TTL_DAYS = 90


def _public_base() -> str:
    """The branded host, not whatever the request happened to arrive on."""
    import os
    return (os.environ.get("PUBLIC_BASE_URL") or "https://api.hatchloop.dev").rstrip("/")


class KeyRequestBody(BaseModel):
    email: str


@router.get("/request")
async def describe_free_key_flow():
    """What to do here, for whoever followed the link.

    THIS URL IS NAMED IN OUR OWN `auth_required` ERROR - it is the first thing
    a blocked agent or a curious human is told to visit. It answered GET with a
    bare 405 Method Not Allowed, and a POST with no body with a raw FastAPI
    validation dump. So the one instruction we give at the moment someone hits
    the paywall led to a dead end for both audiences.

    An error that tells you where to go, to a place that refuses you, is worse
    than no instruction: it spends the caller's remaining patience.
    """
    return {
        "what": "Free API key for HatchLoop AgentBroker write tools.",
        "how": {
            "method": "POST",
            "url": f"{_public_base()}/keys/request",
            "body": {"email": "you@example.com"},
            "curl": (f"curl -X POST {_public_base()}/keys/request "
                     f"-H 'Content-Type: application/json' "
                     f"-d '{{\"email\":\"you@example.com\"}}'"),
        },
        "then": "We email a verification link. Opening it returns your key.",
        "note": ("You may not need one: 12 of our 20 tools work with no key at "
                 "all, including sanctions screening and company verification."),
        "no_email_available": {
            "reason": "Autonomous agents often have no inbox.",
            "alternative": ("Pay per call with x402 (USDC on Base) - no signup, "
                            "no card, no email. Attach a signed payment as "
                            "params._meta['x402/payment'] on any paid tool and "
                            "retry; the server replies with a priced offer."),
        },
    }


@router.post("/request")
async def request_free_key(body: KeyRequestBody):
    """
    Step 1 of the free-key flow.

    Always returns 200 (to avoid email enumeration). Sends a verification
    email to the supplied address if it looks valid.
    """
    email = body.email.strip().lower()
    if not email or "@" not in email or len(email) > 320:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_email", "detail": "Provide a valid email address."},
        )

    token, expires_at = make_verify_token(email)

    # Derive base URL — use the Render origin (PUBLIC_BASE_URL) not the edge
    # worker URL (MCP_PUBLIC_URL), since /keys/* routes live on the origin.
    base_url = os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")
    verify_url = f"{base_url}/keys/verify?token={token}"

    # Store pending (best-effort) + send email
    await store_pending(email, token, expires_at)
    await send_verification_email(email, verify_url)

    return JSONResponse(content={
        "status": "verification_sent",
        "detail": (
            "Check your inbox for a verification link. "
            "It expires in 1 hour. If you don't see it, check spam."
        ),
    })


@router.get("/verify")
async def verify_free_key(token: str = Query(..., description="Signed verification token")):
    """
    Step 2 of the free-key flow.

    Validates the signed token, mints a free-tier JWT, emails it, and returns
    an HTML confirmation page. GET so it works when clicked directly from email.
    """
    # Primary guard: signature + expiry (no DB needed)
    email = verify_token(token)
    if not email:
        return HTMLResponse(
            status_code=400,
            content=html_error(
                "Verification failed",
                "This link is invalid or has expired. "
                "Request a new key at "
                "<a href='https://hatchloop.dev/docs/#key'>hatchloop.dev/docs</a> - it takes a few seconds and costs nothing.",
            ),
        )

    # SINGLE USE WHEN WE CAN TELL, signature-only when we cannot.
    #
    # This was a bare best-effort cleanup, so the link stayed valid for its
    # whole hour and could be replayed by anyone holding it - verified during
    # a walkthrough by clicking the same link twice and getting a key both
    # times. Enforcing single use USED to be impossible without also breaking
    # signup during a database blip, because consume_pending returned None
    # both for "already used" and for "could not check". It now raises for the
    # second, so the two can be told apart:
    #
    #   row found      -> consume it and continue
    #   row absent     -> already used; say so instead of minting again
    #   cannot look up -> continue on the signature, which is still a real gate
    #
    # The key is deterministic per email, so a replay never produced a second
    # identity or extra quota. What it produced was a link that kept working
    # after the person had already used it.
    from agent_interface.key_request_logic import PendingLookupUnavailable
    try:
        if await consume_pending(token, email=email) is None:
            return HTMLResponse(
                status_code=400,
                content=html_error(
                    "This link has already been used",
                    "Your key was issued the first time you clicked it. If you "
                    "still have the email, the key is in it. Otherwise request "
                    "a fresh one at <a href='https://hatchloop.dev/docs/#key'>hatchloop.dev/docs</a> "
                    "- it takes a few seconds and costs nothing.",
                ),
            )
    except PendingLookupUnavailable as exc:
        # Availability wins here: the signature and expiry above are a real
        # gate on their own, and refusing every signup during a Supabase blip
        # is a worse failure than honouring a replayed link.
        logger.warning(
            "pending_lookup_unavailable err=%s -- issuing on signature alone; "
            "single-use is NOT enforced for this request", exc)

    # Mint a free-tier JWT
    customer_id = f"free_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
    ttl_seconds = _FREE_TIER_TTL_DAYS * 86400

    from agent_interface.identity import issue_token, TokenRequest
    token_resp = issue_token(TokenRequest(
        agent_id=customer_id,
        principal_id=customer_id,
        principal_type="human",
        allowed_operations=["*"],
        budget_cap_usd=0.0,   # free tier — no budget spend allowed
        allowed_verticals=["*"],
        ttl_seconds=ttl_seconds,
    ))
    token_value = token_resp.token
    expires_iso = datetime.fromtimestamp(
        token_resp.expires_at, tz=timezone.utc
    ).strftime("%Y-%m-%d")

    # Email the key (best-effort)
    await send_key_email(email, token_value, expires_iso)

    logger.info(
        "free_key_issued customer_id=%s email_domain=%s",
        customer_id, email.split("@")[-1],
    )

    paid_url = os.getenv("POLAR_CHECKOUT_URL", "https://buy.polar.sh")

    return HTMLResponse(
        status_code=200,
        content=html_success(token_value, expires_iso, customer_id, paid_url),
    )


class MachineMintBody(BaseModel):
    agent_id: str
    timestamp: int
    nonce: str
    signature: str


@router.post("/mint")
async def machine_mint_key(body: MachineMintBody):
    """
    Agent self-serve key issuance (no email required).

    An AI agent that cannot receive email can prove identity with a signed
    payload instead. The caller computes:

        signature = HMAC-SHA256(agent_id + str(timestamp) + nonce, MACHINE_MINT_SECRET)

    where MACHINE_MINT_SECRET is the public challenge secret documented at
    https://hatchloop.dev/docs/#machine-mint.

    Requirements:
      - timestamp must be a Unix epoch integer within 60s of server time.
      - nonce is an arbitrary string; use a UUID or random hex to prevent replay.
      - The HMAC input is the raw concatenation (no separators) of the three fields.
      - The signature must be lowercase hex.

    Returns:
      200 {ok, key, key_id, expires_at, tier, daily_limit}
      401 {error: "invalid_request"}  — bad signature, expired timestamp, or
                                         MACHINE_MINT_SECRET not yet configured
    """
    ok, reason = verify_machine_signature(
        body.agent_id.strip(),
        body.timestamp,
        body.nonce,
        body.signature,
    )
    if not ok:
        if reason == "not_configured":
            # Environment not set — the server is not yet configured for
            # machine-mintable keys; return 503 to be distinct from auth failure.
            return JSONResponse(
                status_code=503,
                content={"error": "not_configured",
                         "detail": "MACHINE_MINT_SECRET is not set on this server."},
            )
        # Generic 401 for ALL auth failures — never distinguish bad-secret from
        # bad-clock to the caller (timing oracle).
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_request",
                     "detail": ("Signature verification failed. "
                                "Check that your timestamp is within 60s of server time, "
                                "your nonce is unique, and your HMAC key is correct.")},
        )

    agent_id = body.agent_id.strip()[:200]   # guard against absurdly long IDs
    customer_id = f"free_machine_{hashlib.sha256(agent_id.encode()).hexdigest()[:16]}"
    ttl_seconds = _FREE_TIER_TTL_DAYS * 86400

    from agent_interface.identity import issue_token, TokenRequest
    token_resp = issue_token(TokenRequest(
        agent_id=customer_id,
        principal_id=customer_id,
        principal_type="system",           # machine, not human
        allowed_operations=["*"],
        budget_cap_usd=0.0,                # free tier — no budget spend allowed
        allowed_verticals=["*"],
        ttl_seconds=ttl_seconds,
    ))
    token_value = token_resp.token
    expires_iso = datetime.fromtimestamp(
        token_resp.expires_at, tz=timezone.utc
    ).strftime("%Y-%m-%d")

    # Durable record in Supabase (best-effort — key is already issued)
    await store_machine_minted(agent_id, token_value, token_resp.expires_at)

    logger.info(
        "machine_key_issued customer_id=%s agent_id_hash=%s",
        customer_id, hashlib.sha256(agent_id.encode()).hexdigest()[:8],
    )

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "key": token_value,
            "key_id": customer_id,
            "expires_at": expires_iso,
            "tier": "free",
            "daily_limit": 100,
            "usage": ("Send as the X-Agent-Identity header on every call to "
                      "https://hatchloop.dev/mcp/agent-broker"),
        },
    )
