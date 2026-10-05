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
import re
import time
import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

from core.env_guard import is_production_for_security_guards
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
#
# Board row 250: the condition below used to be
# `os.getenv("ENVIRONMENT") == "production"`, which reads False when
# ENVIRONMENT is unset — the exact state production ran in, so this never
# logged a word while every issued Agent-Identity JWT (scope, budget_cap,
# verticals — real operating authority) was forgeable. It now uses the
# shared fail-CLOSED helper: unset/unrecognised counts as production.
#
# Kept as a LOG, not a raise, deliberately: this module sits on main.py's
# import chain for the entire service (every route, health check, and piece
# of operator tooling passes through it). Raising here trades "a forgeable
# secret" for "the whole service refuses to boot" on ANY environment
# ambiguity, including a transient misconfiguration — an availability outage
# layered on top of the security gap, for a condition real production is not
# expected to hit (JWT_SIGNING_SECRET is provisioned as a real Render secret
# there). The failure this task found was that the log never fired, not that
# logging was too soft a response — fixing the condition so it reliably
# fires is the fix; escalating severity is a separate call this file does
# not need to make today. See core/env_guard.py for the shared decision and
# agent_interface/unsubscribe.py for why THAT guard reasons independently
# to the same log-not-raise answer instead of inheriting this one.
if is_production_for_security_guards() and (
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
    # Additional signed claims (the OAuth sign-in adds `aud` - the resource the token was issued for - and
    # the client/grant it belongs to). Never allowed to replace a claim this module sets itself.
    extra_claims: dict = field(default_factory=dict)


@dataclass
class TokenResponse:
    token: str
    agent_id: str
    expires_at: float
    issued_at: float


def issue_token(req: TokenRequest, *, issued_at: float | None = None,
                token_id: str | None = None) -> TokenResponse:
    """Issue a signed Agent-Identity token."""
    if (issued_at is None) != (token_id is None):
        raise ValueError("Stable issuance requires both timestamp and token id")
    if issued_at is None:
        now = time.time()
        jti = uuid.uuid4().hex
    else:
        if (isinstance(issued_at, bool) or not isinstance(issued_at, (int, float))
                or not math.isfinite(issued_at) or issued_at <= 0):
            raise ValueError("Invalid stable issuance timestamp")
        now = float(issued_at)
        try:
            jti = uuid.UUID(token_id).hex
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Invalid stable issuance token id") from None
    claims = {
        "jti": jti,
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
    for _k, _v in (req.extra_claims or {}).items():
        if _k not in claims:
            claims[_k] = _v
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

# Immutable fulfillment format 1. Never change this mapping when ordinary plan
# defaults evolve; a persisted order must recreate the same token after release.
_FULFILLMENT_V1_SCOPES = {
    "developer": (["*"], 500.0, ["*"], 7776000),
    "business": (["*"], 5000.0, ["*"], 7776000),
    "enterprise": (["*"], 25000.0, ["*"], 31536000),
}


def issue_subscription_token(
    customer_id: str,
    plan: str,
    customer_email: str,
    *,
    issued_at: float | None = None,
    token_id: str | None = None,
    issuance_version: int | None = None,
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
    if issuance_version is not None:
        if (type(issuance_version) is not int or issuance_version != 1
                or issued_at is None or token_id is None or plan_key not in _FULFILLMENT_V1_SCOPES):
            raise ValueError("Unsupported stable fulfillment entitlement")
        ops, cap, verticals, ttl = _FULFILLMENT_V1_SCOPES[plan_key]
    else:
        ops, cap, verticals, ttl = _PLAN_SCOPES.get(plan_key, _PLAN_SCOPES["developer"])
    request = TokenRequest(
        agent_id=f"sub_{customer_id}",
        principal_id=customer_id,
        principal_type="human",
        allowed_operations=ops,
        budget_cap_usd=cap,
        allowed_verticals=verticals,
        ttl_seconds=ttl,
    )
    if issued_at is None and token_id is None:
        return issue_token(request)
    # The fulfillment store supplies the immutable timestamp/id for this order.
    # Signing is pure: another worker or restart reproduces the same identity.
    return issue_token(request, issued_at=issued_at, token_id=token_id)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    valid: bool
    identity: Optional[AgentIdentity] = None
    error: Optional[str] = None


_revoked_jtis: set[str] = set()

# Durable, single-token revocation (e.g. an operator killing one leaked
# capability token). AUDIT-2026-09-28: this used to be in-memory only --
# revoke_token() added the jti here and reported success, but nothing wrote
# it anywhere durable, so the jti was valid again after the next restart or
# container redeploy. _hydrate_jti_revocations() below loads
# durably-revoked jtis the same way _hydrate_revocations() (further down)
# loads durably-revoked customer ids -- same "latch only on complete
# success, retry on failure, in-memory always works" shape -- because this
# is the identical durability problem keyed on a different column. See
# migrations/revoked_jtis_table.sql for the table this hydrates from.
_jti_revocation_hydrated = False
_jti_revocation_next_try = 0.0
_JTI_REVOCATION_RETRY_S = 60.0
_REVOKED_JTIS_TABLE = "revoked_jtis"

# Durable, customer-level revocation (e.g. a Polar refund). Distinct from
# `_revoked_jtis` above (single-token revoke_token()): a customer can hold
# tokens whose jti we never recorded (they were minted by a webhook, not the
# admin revoke path), so refund-driven revocation has to key on the customer
# identity embedded in the token, not a jti we may not have.
_revoked_customer_ids: set[str] = set()
_revocation_hydrated = False
_revocation_next_try = 0.0
_REVOCATION_RETRY_S = 30.0

# AUDIT-2026-09-29: both hydration loops below used to read their table
# directly via storage.supabase_client.select_rows_sync_strict(). In
# production this container holds ONLY SUPABASE_ANON_KEY (never the service
# key -- see sql/agentbroker/007_revocation_read_rpc.sql's header), and:
#   * revoked_jtis has RLS with a service_role-only policy, so the raw read
#     got HTTP 401 (permission denied) on every attempt -- an honest error,
#     but one the old except-branch could only log as an endlessly-retried
#     WARNING, so _hydrate_jti_revocations() never latched and
#     revoke_jti()'s durability fix (0505c23) was inert.
#   * polar_order_events has RLS with zero policies and anon still holds the
#     table SELECT grant, so the raw read got HTTP 200 with an EMPTY ARRAY --
#     not an error -- so _hydrate_revocations() LATCHED complete with zero
#     revocations loaded, forever, even with real revoked rows in the table.
# Both now read through a SECURITY DEFINER RPC (007) that anon can call and
# that reproduces the exact same paged, ordered, filtered SELECT. Paging,
# the latch-only-on-complete-success rule, and the in-memory sets are
# unchanged; only the transport is new, plus the REVOCATION_READ_DENIED
# marker below for when the RPC boundary itself is the thing that is broken.
_REVOKED_JTIS_RPC = "revoked_jtis_list"
_REVOKED_CUSTOMERS_RPC = "polar_order_events_revoked_customer_ids"


def _is_permission_or_missing_rpc_error(exc: Exception) -> bool:
    """True when `exc` (raised by storage.supabase_client.rpc_sync) means the
    RPC call was refused for a reason a human must fix -- no grant, no such
    function, or a client-shaped rejection -- rather than a transient outage
    or "Supabase isn't configured at all" (expected in local dev and this
    repo's tests, which run with no SUPABASE_URL). Mirrors billing/
    data_quota.py's _classify_rpc_exception bucketing (misconfigured vs
    outage vs unconfigured), narrowed to the one distinction the two
    hydration loops below need: log a denial LOUDLY and distinctly, never
    folded into the same WARNING line an ordinary Supabase blip already
    produces -- a denial here does not self-heal on the backoff retry the
    way an outage does; it needs a human to apply
    sql/agentbroker/007_revocation_read_rpc.sql or fix a grant.

    rpc_sync() raises a plain RuntimeError built from its own message
    (storage/supabase_client.py's own contract) -- there is no structured
    status code attached, so this reads the message rpc_sync already
    contracts to produce rather than inventing a second parallel channel.
    """
    msg = str(exc)
    if "not configured" in msg:
        return False
    match = re.search(r"HTTP (\d{3})", msg)
    if match:
        # 401/403: PostgREST's shape for "no grant to call this function".
        # An RLS-only block on a table PostgREST can still see returns 200
        # with rows filtered out, never these codes -- see 007's own header
        # for revoked_jtis' measured 401. 404: PGRST202, the function does
        # not exist yet -- i.e. 007 has not been applied. Anything else
        # (400/422 malformed call, 5xx) is not a permission question.
        return int(match.group(1)) in (401, 403, 404)
    return False


def _hydrate_jti_revocations() -> None:
    """One-time (then backoff-retried), best-effort load of durably-revoked
    jtis from Supabase, so a single-token revoke_token() call survives a
    process restart -- the read half of AUDIT-2026-09-28. Reads via the
    revoked_jtis_list SECURITY DEFINER RPC (AUDIT-2026-09-29; see the
    constants above for why a raw table read cannot work with only the anon
    key). No-ops safely when Supabase isn't configured (local/dev/tests),
    exactly like _hydrate_revocations() below.

    Latches ONLY on a complete, successful read. A failed or truncated read
    returns without setting the latch, so the backoff below retries it --
    the previous customer-revocation bug (latching "done" on a partial page,
    or on a failed read) is the one shape this function must never repeat
    for jtis either. Because the latch never clears anything it already
    knows, and revoke_token()/revoke_jti() add to `_revoked_jtis`
    synchronously before ever touching the network, a jti this process has
    already learned is revoked (by calling revoke_token() itself, or by an
    earlier successful hydration) can never be un-learned by a later outage
    here -- only new information is ever added.
    """
    global _jti_revocation_hydrated, _jti_revocation_next_try
    if _jti_revocation_hydrated:
        return

    now = time.time()
    if now < _jti_revocation_next_try:
        return
    _jti_revocation_next_try = now + _JTI_REVOCATION_RETRY_S

    log = logging.getLogger("smb_broker.identity")
    try:
        from storage.supabase_client import rpc_sync
        # ORDERED, PAGED, latch-on-complete-success only -- see the identical
        # reasoning above _hydrate_revocations(); duplicated here rather than
        # shared because the two loops read different RPCs into different
        # sets and unifying them would trade this comment for an
        # indirection that has to be re-read anyway.
        rows = []
        _page = 1000
        for _p in range(50):                    # 50k revocations, then complain
            _chunk = rpc_sync(
                _REVOKED_JTIS_RPC, {"p_limit": _page, "p_offset": _p * _page})
            if not isinstance(_chunk, list) or not all(
                isinstance(_row, dict) and "jti" in _row for _row in _chunk
            ):
                raise RuntimeError(
                    f"{_REVOKED_JTIS_RPC} returned a page that is not a "
                    f"list of dicts carrying jti -- refusing to trust a shape it does "
                    f"not contract to return")
            rows.extend(_chunk)
            if len(_chunk) < _page:
                break
        else:
            log.error(
                "JTI_REVOCATION_HYDRATION_INCOMPLETE after %d rows - raise "
                "the page ceiling; hydration is NOT latched so it will retry",
                len(rows))
            for row in rows:
                if row.get("jti"):
                    _revoked_jtis.add(str(row["jti"]))
            return

        # Everything below reads `rows` -- keep it INSIDE the try so a
        # malformed row (rejected above) or any other surprise here takes
        # the except branch below (log + no latch) instead of raising
        # past is_jti_revoked() into validate_token() on the live auth path.
        for row in rows:
            jti = row.get("jti")
            if jti:
                _revoked_jtis.add(str(jti))
        _jti_revocation_hydrated = True
        log.info("jti_revocation_hydrated count=%d", len(_revoked_jtis))
    except Exception as exc:  # noqa: BLE001
        if _is_permission_or_missing_rpc_error(exc):
            # LOUD AND DISTINCT: this does not self-heal on the backoff retry
            # below the way an outage does -- it means the anon key cannot
            # call revoked_jtis_list at all (007 not applied, or a grant
            # regressed). Every jti revoked on another process, or before
            # this process's last restart, is silently un-honoured here
            # until a human fixes this.
            log.error(
                "REVOCATION_READ_DENIED rpc=%s err=%s -- jti revocation is "
                "INERT in this process until this is fixed (apply "
                "sql/agentbroker/007_revocation_read_rpc.sql or check its "
                "grants); never read this as 'no jtis are revoked'",
                _REVOKED_JTIS_RPC, exc)
        else:
            # DELIBERATELY FAIL OPEN ON HYDRATION, and say so -- matches
            # _hydrate_revocations(). A Supabase blip must not turn into a
            # service-wide outage for every other valid token; what it must
            # never do is claim to have loaded a revocation list it did not.
            log.warning(
                "jti_revocation_hydrate_failed err=%s -- a jti revoked on "
                "another process may not be honoured here until this succeeds; "
                "retrying in %ss",
                exc, _JTI_REVOCATION_RETRY_S)
        return


def is_jti_revoked(jti: str) -> bool:
    """True if `jti` has been revoked -- this process (revoke_token()/
    revoke_jti() called here), or durably before this process started
    (revoked on a peer, loaded via _hydrate_jti_revocations()).

    Checks the in-memory set FIRST, before hydrating: a jti this process
    already knows is revoked must never depend on a hydration call
    succeeding to keep testing as revoked. Mirrors is_customer_revoked().
    """
    if not jti:
        return False
    if jti in _revoked_jtis:
        return True
    _hydrate_jti_revocations()
    return jti in _revoked_jtis


def _hydrate_revocations() -> None:
    """Refresh durably-revoked customer ids at least every 30 seconds.
    Supabase so a revocation survives a process restart (e.g. a Render
    redeploy between the refund event and the next validate_token call).
    Reads via the polar_order_events_revoked_customer_ids SECURITY DEFINER
    RPC (AUDIT-2026-09-29; see the constants above for why a raw table read
    cannot work with only the anon key). No-ops safely when Supabase isn't
    configured (local/dev/tests). With a configured backend, paid identities
    deny access if this refresh cannot establish a current revocation view."""
    global _revocation_hydrated, _revocation_next_try
    if _revocation_hydrated and time.time() < _revocation_next_try:
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
    _revocation_hydrated = False  # An expired cache is not proof of current authorization.
    _revocation_next_try = now + _REVOCATION_RETRY_S

    log = logging.getLogger("smb_broker.identity")
    try:
        from storage.supabase_client import rpc_sync
        # ORDERED AND BOUNDED, PAGED -- see revoked_jtis_list's identical
        # reasoning above _hydrate_jti_revocations(). The RPC itself fixes
        # the filter (status = 'revoked') and the ordering (ts desc,
        # customer_id asc) server-side; see
        # sql/agentbroker/007_revocation_read_rpc.sql.
        #
        # It used to ask for 5000 rows, log an error if it got 5000, and then
        # latch hydration as DONE anyway - so past that boundary the extra
        # revocations were never loaded and never retried, and those refunded
        # customers kept paid access for the life of the process. That is the
        # same bug as the one this function's own comment describes, one level
        # down: the loud log made it look handled.
        rows = []
        _page = 1000
        for _p in range(50):                    # 50k revocations, then complain
            _chunk = rpc_sync(
                _REVOKED_CUSTOMERS_RPC, {"p_limit": _page, "p_offset": _p * _page})
            if not isinstance(_chunk, list) or not all(
                isinstance(_row, dict) and "customer_id" in _row for _row in _chunk
            ):
                raise RuntimeError(
                    f"{_REVOKED_CUSTOMERS_RPC} returned a page that is "
                    f"not a list of dicts carrying customer_id -- refusing to trust a shape "
                    f"it does not contract to return")
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

        # Everything below reads `rows` -- keep it INSIDE the try so a
        # malformed row (rejected above) or any other surprise here takes
        # the except branch below (log + no latch) instead of raising past
        # is_customer_revoked() into validate_token() on the live auth path.
        for row in rows:
            cid = row.get("customer_id")
            if cid:
                _revoked_customer_ids.add(str(cid))
        _revocation_hydrated = True
        log.info("revocation_hydrated count=%d", len(_revoked_customer_ids))
    except Exception as exc:  # noqa: BLE001
        if _is_permission_or_missing_rpc_error(exc):
            # LOUD AND DISTINCT: this does not self-heal on the backoff retry
            # below the way an outage does -- it means the anon key cannot
            # call polar_order_events_revoked_customer_ids at all (007 not
            # applied, or a grant regressed). A refunded/revoked customer's
            # token is silently un-revoked here until a human fixes this.
            log.error(
                "REVOCATION_READ_DENIED rpc=%s err=%s -- customer-level "
                "revocation is INERT in this process until this is fixed "
                "(apply sql/agentbroker/007_revocation_read_rpc.sql or check "
                "its grants); never read this as 'no customers are revoked'",
                _REVOKED_CUSTOMERS_RPC, exc)
        else:
            # Leave hydration incomplete: paid identities with a configured
            # backend fail closed until the retry succeeds. Free identities
            # retain their existing authorization behavior.
            log.warning(
                "revocation_hydrate_failed err=%s -- paid revocation view unavailable; retrying in %ss",
                exc, _REVOCATION_RETRY_S)
        return


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


def remember_customer_revocation(customer_id: str) -> None:
    """Honor a customer revocation already committed by the scoped fulfillment RPC."""
    if customer_id:
        _revoked_customer_ids.add(str(customer_id))


def is_customer_revoked(customer_id: str) -> bool:
    """True if `customer_id` has been revoked (this process, or durably
    before this process started)."""
    _hydrate_revocations()
    return str(customer_id) in _revoked_customer_ids


def paid_customer_revoked(customer_id: str) -> bool:
    """Configured paid identities deny access if the revocation view is stale."""
    revoked = is_customer_revoked(customer_id)
    return revoked or bool(os.getenv("SUPABASE_URL") and not _revocation_hydrated)


def validate_token(token: str) -> ValidationResult:
    """Verify signature and expiry. Return AgentIdentity if valid."""
    try:
        claims = _verify(token)
    except ValueError as e:
        return ValidationResult(valid=False, error=str(e))

    # AUDIENCE (MCP authorization: "MCP servers MUST validate that access tokens were issued specifically
    # for them as the intended audience"). Tokens minted by the OAuth sign-in carry `aud`; every other token
    # this service has ever issued has none and is unaffected. A token bound to a resource that is not one of
    # ours is refused, so a token this service signed for another purpose can never be replayed here.
    if "aud" in claims:
        from agent_interface.oauth.resources import is_our_resource
        if not is_our_resource(claims.get("aud")):
            return ValidationResult(valid=False, error="Token audience is not this server.")

    # Check revocation (durable: is_jti_revoked() hydrates from Supabase on
    # top of the in-memory set, so a jti revoked on another process, or
    # before this process's last restart, is still honoured here).
    jti = claims.get("jti", "")
    if is_jti_revoked(jti):
        return ValidationResult(valid=False, error="Token has been revoked.")

    # Check durable customer-level revocation (e.g. the order behind this
    # token was refunded). Keyed on principal.id, which issue_subscription_
    # token() sets to the Polar customer_id.
    principal_id = (claims.get("principal") or {}).get("id")
    revoked = (paid_customer_revoked(principal_id) if str(claims.get("agent_id", "")).startswith("sub_")
               else is_customer_revoked(principal_id)) if principal_id else False
    if revoked:
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


def agent_id_from_token(raw_token: Optional[str]) -> str:
    """The PARSED agent_id in a bearer token, or 'anonymous'.

    Never returns any slice of the raw token value, so the result is safe to
    log and safe to store as a row's owner (core/ownership.py converts the
    sentinel to NULL rather than letting it become a shared account).

    This lives here rather than in mcp_server because /ops/* needs it too:
    the ownership guards this feeds have to be closed on BOTH surfaces, and
    main.py cannot import mcp_server without a cycle. mcp_server keeps its
    `_agent_id_from_token` name and delegates here.
    """
    from core.ownership import ANONYMOUS

    if not raw_token or raw_token in ("", ANONYMOUS):
        return ANONYMOUS
    try:
        result = validate_token(raw_token)
        if result.valid and result.identity:
            return result.identity.agent_id
    except Exception:  # noqa: BLE001
        pass
    return ANONYMOUS


def peek_agent_id(raw_token: Optional[str]) -> Optional[str]:
    """The agent_id in a correctly SIGNED, unexpired token - or None. No I/O, never raises.

    WHY THIS IS NOT validate_token. The rate limiter needs to know whether a request carries a
    real key before the body is read, on the event loop, for every /mcp call. validate_token also
    consults the durable revocation lists, which can hydrate over the network. This does the pure
    HMAC and expiry check only, so it is safe in middleware. It is NOT an authorisation decision:
    a revoked key still peeks as valid, and the only thing it is used for is choosing a rate-limit
    bucket - where "a signed key gets its own bucket" is the whole point and a revoked key doing
    so costs nothing.
    """
    if not raw_token or not isinstance(raw_token, str):
        return None
    try:
        claims = _verify(raw_token.strip())
        if float(claims.get("exp", 0) or 0) < time.time():
            return None
        agent_id = claims.get("agent_id")
        return str(agent_id) if agent_id else None
    except Exception:  # noqa: BLE001
        return None


async def revoke_jti(jti: str, reason: str = "manual") -> bool:
    """Revoke a specific jti directly, for callers that already hold the
    jti value rather than a raw token (e.g. portal.py's key/regenerate,
    which stores `key_jti` as its own column and never re-derives it from
    the old raw token). Shares the exact durability contract as
    revoke_token(), which parses a token down to its jti and calls this.

    Takes effect immediately in this process (in-memory set, checked by
    every validate_token() call via is_jti_revoked()) and is durably
    persisted so the revocation also survives a restart. Mirrors
    revoke_customer()'s write pattern: the in-memory effect always applies
    even if the durable write fails -- never raises.

    Returns True only when the durable write also lands. A caller that gets
    False must treat the revocation as NOT yet safe against a restart --
    same honesty contract as revoke_customer()'s return value.
    """
    log = logging.getLogger("smb_broker.identity")
    if not jti:
        return False
    _revoked_jtis.add(jti)
    # Never log a jti next to the token it came from -- this line never has
    # the token in scope, only the jti, which is safe to log (an opaque
    # identifier used for matching, not a bearer credential).
    log.info("jti_revoked jti=%s reason=%s", jti, reason)
    try:
        from storage.supabase_client import insert_row_strict
        from datetime import datetime, timezone
        await insert_row_strict(_REVOKED_JTIS_TABLE, {
            "jti": jti,
            "reason": reason,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        log.error(
            "jti_revocation_persist_failed jti=%s err=%s -- the revocation "
            "is IN-MEMORY ONLY and will not survive a restart",
            jti, exc,
        )
        return False


async def revoke_token(token: str, reason: str = "manual") -> bool:
    """Revoke a token's jti: immediately in-memory (this process -- always
    happens for a well-formed token, unconditionally) and durably in
    Supabase (survives a restart; may fail if the store is unreachable).

    Returns True only if BOTH held -- i.e. only when the caller can trust
    this revocation to survive a redeploy without needing to re-check. A
    malformed/unparseable token, or one with no jti claim, returns False
    without touching any state. This is a change from the old contract
    ("True if I parsed a jti and added it to a set"): that reported success
    on a write that could vanish at the next restart, which is exactly the
    defect this function exists to close (AUDIT-2026-09-28). A caller that
    needs to know "is this token rejected right now, in this process"
    rather than "will this survive a restart" can rely on the in-memory
    effect always having applied when this returns for a parseable token --
    only the return value's truthiness for the DURABLE guarantee changed.
    """
    try:
        claims = _verify(token)
    except ValueError:
        return False
    jti = claims.get("jti", "")
    if not jti:
        return False
    return await revoke_jti(jti, reason=reason)


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
