"""
Agent identity — issues, validates, and revokes Agent-Identity JWTs.

JWT claims (from api/identity.md):
  agent_id      — unique agent identifier
  principal     — human or system principal that owns the agent
  scope.operations  — list of allowed operations (or ["*"] for all)
  scope.budget_cap  — max spend per 30-day window in USD
  scope.verticals   — list of allowed verticals (or ["*"] for all)
  iat, exp          — issued at / expiry
  iss               — issuer ("smb-broker-v1")

Stub implementation: uses HS256 signing with a local secret.
Production: replace with proper PKI / short-lived tokens from auth service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from core.models import AgentIdentity, AgentScope, Principal


# ---------------------------------------------------------------------------
# Config (override via environment in production)
# ---------------------------------------------------------------------------

_DEFAULT_SECRET = "dev-secret-replace-in-production"
_SIGNING_SECRET = os.getenv("JWT_SIGNING_SECRET", _DEFAULT_SECRET)
_ISSUER = "smb-broker-v1"
_DEFAULT_TTL_SECONDS = 3600 * 24  # 24 hours

# Startup-time guard: in production, refuse-to-deploy is too aggressive (would
# break the app on missing env). Instead, loudly log so the operator notices
# in the deploy logs and rotates the key before the first customer arrives.
if os.getenv("ENVIRONMENT") == "production" and (
    not os.getenv("JWT_SIGNING_SECRET") or _SIGNING_SECRET == _DEFAULT_SECRET
):
    logging.getLogger("smb_broker.identity").error(
        "SECURITY: JWT_SIGNING_SECRET is missing or set to the development "
        "default in a production environment. All issued tokens are forgeable. "
        "Set a strong JWT_SIGNING_SECRET (>= 32 chars) and redeploy immediately."
    )


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------

@dataclass
class TokenRequest:
    agent_id: str
    principal_id: str
    principal_type: str = "system"         # "system" | "human"
    allowed_operations: list[str] = field(default_factory=lambda: ["*"])
    budget_cap_usd: float = 10.0
    allowed_verticals: list[str] = field(default_factory=lambda: ["*"])
    ttl_seconds: int = _DEFAULT_TTL_SECONDS


@dataclass
class TokenResponse:
    token: str
    agent_id: str
    expires_at: float
    issued_at: float


def issue_token(req: TokenRequest) -> TokenResponse:
    """Issue a signed Agent-Identity token."""
    now = time.time()
    claims = {
        "jti": uuid.uuid4().hex,
        "iss": _ISSUER,
        "agent_id": req.agent_id,
        "principal": {
            "id": req.principal_id,
            "type": req.principal_type,
        },
        "scope": {
            "operations": req.allowed_operations,
            "budget_cap_usd": req.budget_cap_usd,
            "verticals": req.allowed_verticals,
        },
        "iat": now,
        "exp": now + req.ttl_seconds,
    }
    token = _sign(claims)
    return TokenResponse(
        token=token,
        agent_id=req.agent_id,
        issued_at=now,
        expires_at=now + req.ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Subscription-plan → token mapping
# ---------------------------------------------------------------------------

_ONE_DAY = 86400
_NINETY_DAYS = 90 * _ONE_DAY
_ONE_YEAR = 365 * _ONE_DAY

# (allowed_operations, budget_cap_usd, allowed_verticals, ttl_seconds)
_PLAN_SCOPES: dict[str, tuple[list[str], float, list[str], int]] = {
    "developer":  (["*"],  50.0, ["*"], _NINETY_DAYS),
    "business":   (["*"], 500.0, ["*"], _NINETY_DAYS),
    "enterprise": (["*"], 500.0, ["*"], _ONE_YEAR),
}


def issue_subscription_token(
    customer_id: str,
    plan: str,
    customer_email: str,
) -> TokenResponse:
    """
    Mint a long-lived Agent-Identity token for a paying subscriber.

    Called from the Paddle webhook on `subscription.activated` and from the
    admin `/auth/token` route. Unknown plan strings fall back to "developer"
    so we never fail-closed on a paid customer.

    `customer_email` is accepted for signature parity with the call sites
    (the email is consumed by the delivery layer, not embedded in the JWT
    to keep tokens small and reduce PII exposure if a token is leaked).
    """
    _ = customer_email  # delivery-layer concern; intentionally unused here
    plan_key = (plan or "").strip().lower()
    ops, cap, verticals, ttl = _PLAN_SCOPES.get(plan_key, _PLAN_SCOPES["developer"])
    return issue_token(TokenRequest(
        agent_id=f"sub_{customer_id}",
        principal_id=customer_id,
        principal_type="human",
        allowed_operations=ops,
        budget_cap_usd=cap,
        allowed_verticals=verticals,
        ttl_seconds=ttl,
    ))


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    valid: bool
    identity: Optional[AgentIdentity] = None
    error: Optional[str] = None


_revoked_jtis: set[str] = set()


def validate_token(token: str) -> ValidationResult:
    """Verify signature and expiry. Return AgentIdentity if valid."""
    try:
        claims = _verify(token)
    except ValueError as e:
        return ValidationResult(valid=False, error=str(e))

    # Check revocation
    jti = claims.get("jti", "")
    if jti in _revoked_jtis:
        return ValidationResult(valid=False, error="Token has been revoked.")

    # Check expiry
    if claims.get("exp", 0) < time.time():
        return ValidationResult(valid=False, error="Token has expired.")

    # Build AgentIdentity
    scope_raw = claims.get("scope", {})
    principal_raw = claims.get("principal", {})

    scope = AgentScope(
        operations=scope_raw.get("operations", ["*"]),
        budget_cap_usd=scope_raw.get("budget_cap_usd", 0.0),
        verticals=scope_raw.get("verticals", ["*"]),
    )
    principal = Principal(
        id=principal_raw.get("id", "unknown"),
        type=principal_raw.get("type", "system"),
    )
    identity = AgentIdentity(
        agent_id=claims["agent_id"],
        principal=principal,
        scope=scope,
        issued_at=claims.get("iat", 0.0),
        expires_at=claims.get("exp", 0.0),
        issuer=claims.get("iss", "unknown"),
    )
    return ValidationResult(valid=True, identity=identity)


def revoke_token(token: str) -> bool:
    """Add token's JTI to revocation set. Returns True if revocation succeeded."""
    try:
        claims = _verify(token)
        jti = claims.get("jti", "")
        if jti:
            _revoked_jtis.add(jti)
            return True
    except ValueError:
        pass
    return False


def check_operation_allowed(identity: AgentIdentity, operation: str) -> bool:
    ops = identity.scope.operations
    return "*" in ops or operation in ops


def check_vertical_allowed(identity: AgentIdentity, vertical: str) -> bool:
    verts = identity.scope.verticals
    return "*" in verts or vertical in verts


# ---------------------------------------------------------------------------
# Signing primitives (HS256-like, simplified for stub)
# ---------------------------------------------------------------------------

def _sign(claims: dict) -> str:
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(
        _SIGNING_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    # Simple format: base64url(payload) + "." + sig
    import base64
    b64_payload = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64_payload}.{sig}"


def _verify(token: str) -> dict:
    import base64
    parts = token.split(".")
    if len(parts) != 2:
        raise ValueError("Malformed token: expected 2 parts.")
    b64_payload, sig = parts
    # Re-pad
    padded = b64_payload + "=" * (-len(b64_payload) % 4)
    payload = base64.urlsafe_b64decode(padded).decode()
    expected_sig = hmac.new(
        _SIGNING_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Invalid token signature.")
    return json.loads(payload)
