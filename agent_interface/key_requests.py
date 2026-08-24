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
    # Re-export for mcp_server.py to import from here (backward compat)
    is_free_key,
    consume_free_daily,
    get_free_daily_remaining,
)

logger = logging.getLogger("smb_broker.key_requests")

router = APIRouter(prefix="/keys", tags=["Free Keys"])

_FREE_TIER_TTL_DAYS = 90


class KeyRequestBody(BaseModel):
    email: str


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
                "Request a new key at <a href='/keys/request'>/keys/request</a>.",
            ),
        )

    # Consume the pending row from Supabase (best-effort cleanup — doesn't
    # affect the grant; signature check above is the real gate).
    await consume_pending(token)

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
