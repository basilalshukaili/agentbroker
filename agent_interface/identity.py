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
# Budget caps are a per-30-day soft guard against runaway bills, sized to
# match the op counts the /pricing page advertises (10k / 100k / negotiated)
# at an ~$0.05 blended cost per op, with headroom for premium ops like
# schedule_appointment and escalate_to_human. They are NOT a billing
# enforcement mechanism - Polar, the merchant of record, handles that - the
# broker only soft-throttles agents that blow past the cap.
#
# This said "Paddle handles that". Paddle was evaluated and never adopted:
# there is no PADDLE_API_KEY and no PADDLE_WEBHOOK_SECRET in any environment,
# so /webhooks/paddle 401s on every request (verified against production
# 2026-08-30 - it fails closed, which is the right direction). Polar is the
# fiat rail; x402 is the crypto one.
_PLAN_SCOPES: dict[str, tuple[list[str], float, list[str], int]] = {
    "developer":  (["*"],    500.0, ["*"], _NINETY_DAYS),
    "business":   (["*"],   5000.0, ["*"], _NINETY_DAYS),
    "enterprise": (["*"],  25000.0, ["*"], _ONE_YEAR),
}


def issue_subscription_token(
    customer_id: str,
    plan: str,
    customer_email: str,
) -> TokenResponse:
    """
    Mint a long-lived Agent-Identity token for a paying subscriber.

    Called from billing/polar_webhook.py on a completed order, from the
    customer portal, and from the admin `/auth/token` route. (It said "the
    Paddle webhook"; that rail was never adopted - see the note above
    _PLAN_SCOPES.) Unknown plan strings fall back to "developer" so we never
    fail-closed on a paid customer.

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

# Durable, customer-level revocation (e.g. a Polar refund). Distinct from
# `_revoked_jtis` above (single-token revoke_token()): a customer can hold
# tokens whose jti we never recorded (they were minted by a webhook, not the
# admin revoke path), so refund-driven revocation has to key on the customer
# identity embedded in the token, not a jti we may not have.
_revoked_customer_ids: set[str] = set()
_revocation_hydrated = False
_revocation_next_try = 0.0
_REVOCATION_RETRY_S = 60.0


def _hydrate_revocations() -> None:
    """One-time, best-effort load of durably-revoked customer ids from
    Supabase so a revocation survives a process restart (e.g. a Render
    redeploy between the refund event and the next validate_token call).
    No-ops safely when Supabase isn't configured (local/dev/tests) -- same
    "durable is a bonus, in-memory always works" pattern as durable_meter.py
    and supply/smb_directory.py."""
    global _revocation_hydrated, _revocation_next_try
    if _revocation_hydrated:
        return

    # THE LATCH USED TO BE SET BEFORE THE LOAD, AND THE LOAD COULD NOT FAIL
    # LOUDLY. select_rows_sync returns [] on any error, so one failed read
    # marked hydration "done" with an empty revocation set - permanently, for
    # the life of the process. is_customer_revoked() then answered False for
    # everyone, and refunded customers kept paid access until the next
    # redeploy happened to succeed.
    #
    # Now: latch only on SUCCESS, and retry on a backoff so it self-heals.
    now = time.time()
    if now < _revocation_next_try:
        return
    _revocation_next_try = now + _REVOCATION_RETRY_S

    log = logging.getLogger("smb_broker.identity")
    try:
        from storage.supabase_client import select_rows_sync_strict
        # ORDERED AND BOUNDED. A bare limit with no ORDER BY returns an
        # ARBITRARY PostgREST slice - supabase_client's own docstring warns
        # about it - so past the default 1000 revocations we would hydrate a
        # random subset and some refunded customers would silently keep
        # access. Newest first, and the count is checked below.
        # PAGED, because the previous single read could not be complete.
        #
        # It asked for 5000 rows, logged an error if it got 5000, and then
        # latched hydration as DONE anyway - so past that boundary the extra
        # revocations were never loaded and never retried, and those refunded
        # customers kept paid access for the life of the process. That is the
        # same bug as the one this function's own comment describes, one level
        # down: the loud log made it look handled.
        rows = []
        _page = 1000
        for _p in range(50):                    # 50k revocations, then complain
            _chunk = select_rows_sync_strict(
                "polar_order_events", filters={"status": "revoked"},
                order="ts.desc", limit=_page, offset=_p * _page)
            rows.extend(_chunk)
            if len(_chunk) < _page:
                break
        else:
            # Ran out of pages rather than rows. Do NOT latch - leaving
            # hydration incomplete means the backoff retries, which is the
            # honest state, and this log says what a fix looks like.
            log.error(
                "REVOCATION_HYDRATION_INCOMPLETE after %d rows - raise the "
                "page ceiling; hydration is NOT latched so it will retry",
                len(rows))
            for row in rows:
                if row.get("customer_id"):
                    _revoked_customer_ids.add(str(row["customer_id"]))
            return
    except Exception as exc:  # noqa: BLE001
        # DELIBERATELY FAIL OPEN, and say so. Denying every paying customer
        # during a database blip is a worse outcome than briefly honouring a
        # refunded token, and the retry above bounds how long "briefly" is.
        # What must not happen is the previous behaviour: failing open FOR
        # EVER while reporting success.
        log.warning(
            "revocation_hydrate_failed err=%s -- refunded customers may still "
            "validate until this succeeds; retrying in %ss",
            exc, _REVOCATION_RETRY_S)
        return

    for row in rows:
        cid = row.get("customer_id")
        if cid:
            _revoked_customer_ids.add(str(cid))
    _revocation_hydrated = True
    log.info("revocation_hydrated count=%d", len(_revoked_customer_ids))


async def revoke_customer(
    customer_id: str, order_id: Optional[str] = None, reason: str = "refund",
) -> bool:
    """Revoke every token for `customer_id` (e.g. on a Polar
    order.refunded/refund.created/subscription.revoked webhook).

    Takes effect immediately in this process (in-memory set, checked by
    every validate_token() call) and is durably persisted so the revocation
    also survives a restart. Mirrors billing/durable_meter.py's write
    pattern: the in-memory effect always applies even if the durable write
    fails -- never raises."""
    if not customer_id:
        return
    _revoked_customer_ids.add(str(customer_id))
    logging.getLogger("smb_broker.identity").info(
        "customer_revoked customer_id=%s order_id=%s reason=%s",
        customer_id, order_id, reason,
    )
    try:
        # STRICT. This is the WRITE half of the bug whose read half was fixed
        # earlier today. `insert_row` returns None on failure and cannot
        # raise, so the handler below was dead code and
        # "revocation_persist_failed" could never be logged.
        #
        # The consequence is the whole point of the function: the revocation
        # holds in memory until the next restart, _hydrate_revocations then
        # reads a table that never got the row, and the refunded customer
        # keeps paid access permanently. Fixing only the read half left the
        # same outcome reachable by a different route.
        from storage.supabase_client import insert_row_strict
        from datetime import datetime, timezone
        await insert_row_strict("polar_order_events", {
            "order_id": order_id or "",
            "event_type": reason,
            "customer_id": str(customer_id),
            "status": "revoked",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("smb_broker.identity").error(
            "revocation_persist_failed customer_id=%s err=%s -- the revocation "
            "is IN-MEMORY ONLY and will not survive a restart",
            customer_id, exc,
        )
        return False


def is_customer_revoked(customer_id: str) -> bool:
    """True if `customer_id` has been revoked (this process, or durably
    before this process started)."""
    _hydrate_revocations()
    return str(customer_id) in _revoked_customer_ids


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

    # Check durable customer-level revocation (e.g. the order behind this
    # token was refunded). Keyed on principal.id, which issue_subscription_
    # token() sets to the Polar customer_id.
    principal_id = (claims.get("principal") or {}).get("id")
    if principal_id and is_customer_revoked(principal_id):
        return ValidationResult(
            valid=False, error="Token has been revoked (order refunded).",
        )

    # Check expiry
    if claims.get("exp", 0) < time.time():
        return ValidationResult(valid=False, error="Token has expired.")

    # Build AgentIdentity. The pydantic models live in core/models.py and
    # have specific field shapes that don't match the JWT claim keys 1:1 —
    # bridge the names here so a valid token always produces a usable
    # identity (this is the bug a paid customer would hit on their first
    # authenticated call).
    from datetime import datetime, timezone
    from core.models import Vertical

    scope_raw = claims.get("scope", {}) or {}
    principal_raw = claims.get("principal", {}) or {}

    # JWT claim is `budget_cap_usd`; pydantic field is `budget_cap`.
    budget_cap = float(
        scope_raw.get("budget_cap_usd",
                      scope_raw.get("budget_cap", 0.0)) or 0.0
    )

    # JWT claim verticals are strings or ["*"]. The model expects
    # Optional[list[Vertical]]. Drop unknowns + treat "*" as None (= all).
    raw_verticals = scope_raw.get("verticals") or []
    typed_verticals: Optional[list[Vertical]]
    if not raw_verticals or "*" in raw_verticals:
        typed_verticals = None
    else:
        typed_verticals = []
        for v in raw_verticals:
            try:
                typed_verticals.append(Vertical(v))
            except ValueError:
                # silently drop unknown verticals — better than 401-ing the
                # whole token because of one stale enum value
                pass
        if not typed_verticals:
            typed_verticals = None

    scope = AgentScope(
        operations=scope_raw.get("operations", ["*"]),
        budget_cap=budget_cap,
        verticals=typed_verticals,
    )

    # JWT claim has principal.type ∈ {"system", "human"} from the issuance
    # path. The PrincipalKind enum only allows "consumer" / "business".
    # Map system→business, human→consumer. If the principal is missing
    # entirely (older tokens), default to None — AgentIdentity.principal is
    # Optional so that's still valid.
    principal_kind_raw = principal_raw.get("type") or principal_raw.get("kind")
    if principal_kind_raw == "human":
        principal_kind_raw = "consumer"
    elif principal_kind_raw == "system":
        principal_kind_raw = "business"

    principal: Optional[Principal]
    if principal_raw.get("id") and principal_kind_raw in {"consumer", "business"}:
        from core.models import PrincipalKind
        principal = Principal(
            kind=PrincipalKind(principal_kind_raw),
            id=str(principal_raw["id"]),
        )
    else:
        principal = None

    # JWT exp is epoch seconds; model expects a datetime.
    expiry_dt = datetime.fromtimestamp(
        float(claims.get("exp", 0.0)),
        tz=timezone.utc,
    )

    identity = AgentIdentity(
        agent_id=claims["agent_id"],
        principal=principal,
        scope=scope,
        expiry=expiry_dt,
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
    # None or empty = unrestricted (matches the JWT "*" sentinel).
    if not verts:
        return True
    # `verts` are now Vertical enum members (validate_token typed them);
    # compare against both the value and the enum.
    return any(
        (getattr(v, "value", v) == vertical) or (v == vertical)
        for v in verts
    )


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
