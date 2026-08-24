"""
agent_interface/portal_logic.py -- Pure business logic for the customer portal.

No FastAPI imports here — this module is safe to test without the web stack.
All stateless helpers: token creation/verification, session cookie, masking,
and package resolution. The FastAPI router (portal.py) imports from here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("smb_broker.portal_logic")

_MAGIC_TTL_S = 900          # 15 minutes
_SESSION_TTL_S = 30 * 24 * 3600  # 30 days
_LOW_BALANCE_THRESHOLD = 500
_PACKAGE_CREDITS = {"starter": 1000, "growth": 3500, "scale": 13000}


# ---------------------------------------------------------------------------
# Signing secret
# ---------------------------------------------------------------------------

def portal_secret() -> str:
    """PORTAL_SESSION_SECRET -> KEY_VERIFY_SECRET -> JWT_SIGNING_SECRET -> fallback."""
    return (
        os.getenv("PORTAL_SESSION_SECRET")
        or os.getenv("KEY_VERIFY_SECRET")
        or os.getenv("JWT_SIGNING_SECRET")
        or "dev-portal-secret-replace"
    )


# ---------------------------------------------------------------------------
# Magic token
# ---------------------------------------------------------------------------

def make_magic_token(email: str, secret: Optional[str] = None) -> tuple[str, float]:
    """Create a signed magic login token. Returns (token, expires_at)."""
    sec = secret or portal_secret()
    expires_at = time.time() + _MAGIC_TTL_S
    payload = f"{email}|{expires_at}"
    sig = _hmac_mod.new(
        sec.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}", expires_at


def verify_magic_token(token: str, secret: Optional[str] = None) -> Optional[str]:
    """Verify magic token. Returns email if valid and unexpired, else None."""
    sec = secret or portal_secret()
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        b64, sig = parts
        padded = b64 + "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        expected_sig = _hmac_mod.new(
            sec.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not _hmac_mod.compare_digest(sig, expected_sig):
            return None
        email, exp_str = payload.rsplit("|", 1)
        if time.time() > float(exp_str):
            return None
        return email.strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

def make_session_cookie(email: str, secret: Optional[str] = None) -> str:
    """Mint a signed session cookie value (not the Set-Cookie header)."""
    sec = secret or portal_secret()
    expires_at = time.time() + _SESSION_TTL_S
    payload = f"{email}|{expires_at}"
    sig = _hmac_mod.new(
        sec.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def verify_session_cookie(cookie_value: Optional[str], secret: Optional[str] = None) -> Optional[str]:
    """Verify session cookie. Returns email if valid, else None."""
    sec = secret or portal_secret()
    if not cookie_value:
        return None
    try:
        parts = cookie_value.split(".")
        if len(parts) != 2:
            return None
        b64, sig = parts
        padded = b64 + "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        expected_sig = _hmac_mod.new(
            sec.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not _hmac_mod.compare_digest(sig, expected_sig):
            return None
        email, exp_str = payload.rsplit("|", 1)
        if time.time() > float(exp_str):
            return None
        return email.strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Token consume tracking
# ---------------------------------------------------------------------------

def token_consume_key(token: str) -> str:
    """Return a short key for the used-token set (not the full token)."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Key masking
# ---------------------------------------------------------------------------

def mask_key(token: str) -> str:
    """Mask a token: show first 12 and last 4 characters with '...' in between."""
    if len(token) <= 16:
        return token[:4] + "..." if len(token) > 4 else "***"
    return f"{token[:12]}...{token[-4:]}"


# ---------------------------------------------------------------------------
# Polar package resolution
# ---------------------------------------------------------------------------

def load_polar_packages() -> dict[str, int]:
    """Load POLAR_PACKAGES env: {product_id: credits}."""
    import json
    raw = os.getenv("POLAR_PACKAGES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): int(v) for k, v in parsed.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def product_id_for_package(package: str) -> Optional[tuple[str, int]]:
    """Return (product_id, credits) for a named package, or None if not configured."""
    pkgs = load_polar_packages()
    if not pkgs:
        return None
    target_credits = _PACKAGE_CREDITS.get(package.lower())
    if target_credits is None:
        return None
    for product_id, credits in pkgs.items():
        if credits == target_credits:
            return product_id, credits
    # Fallback: if only one product configured, use it.
    if len(pkgs) == 1:
        pid, cr = next(iter(pkgs.items()))
        return pid, cr
    return None
