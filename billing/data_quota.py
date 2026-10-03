"""
billing/data_quota.py -- free-tier daily quota for premium data tools.

Activated only when DATA_METERING_ENABLED=true in config. Tracks per-caller
daily usage for the 3 premium data tools (verify_company_record,
screen_sanctions, map_trade_restriction).

Caller tiers:
  - Email-verified free key (key_id starts with "free_"):
      In-memory counter per key_id+date (same pattern as consume_free_daily).
      Limit: FREE_DATA_QUOTA_PER_DAY env var, default 50.
  - Anonymous (no key or unrecognised key):
      Supabase counter keyed by sha256(ip:date) -- best-effort.
      Limit: ANON_DATA_QUOTA_PER_DAY env var, default 20.
      Fail-open: if Supabase is unavailable or IP is unknown, allow the call.

When DATA_METERING_ENABLED=false (default) this module is never called;
the data tools run free/unmetered via the bypass in mcp_server.py.

Honesty invariants:
  - NEVER run the tool for free beyond quota -- return honest failure instead.
  - Beyond-quota callers can escape via x402 or credits (handled upstream,
    BEFORE this gate runs). Here we gate remaining callers: free keys + anon.
  - Tool is NOT dispatched on failure (cost=0 guaranteed).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("smb_broker.data_quota")

# The 3 premium data tools subject to metering when DATA_METERING_ENABLED=true.
PREMIUM_DATA_TOOLS: frozenset[str] = frozenset({
    "verify_company_record",
    "screen_sanctions",
    "map_trade_restriction",
})

# Upgrade / free-key URLs embedded in honest-failure messages.
_FREE_KEY_URL = "https://hatchloop.dev/agent-broker"
_UPGRADE_URL = "https://hatchloop.dev/pricing"


def _credits_clause() -> str:
    """", or top up credits at <url>" while the credits gate runs, otherwise "".

    This quota gate runs when DATA_METERING_ENABLED is on, and the config comments call "metering on,
    credits still off" the first flip. In that state there is no credit balance to top up, so the
    refusal must not send the caller to buy one (billing/switches.py is the one reader of the switch).
    """
    from billing import switches
    return f", or top up credits at {_UPGRADE_URL}" if switches.credits_enabled() else ""


# ---------------------------------------------------------------------------
# In-memory per-free-key daily counter (cleared on process restart).
# { key_id: {"count": int, "date": "YYYY-MM-DD"} }
# ---------------------------------------------------------------------------
_free_key_data_daily: dict[str, dict] = {}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# THE LIMITS COME FROM config.py, WHICH IS WHERE THEY WERE RAISED.
#
# On 2026-08-26 a deliberate generosity pass raised the premium-data quotas to
# 500/day with a free key and 100/day anonymous, and that change was propagated
# to every public surface - the site, the README, llms-install, the skill repo,
# the directory listings, two open PRs.
#
# It landed in config.py. This module never read config.py. It re-read the same
# two environment variables with its OWN defaults - the pre-raise 50 and 20 -
# so unless someone also set the env vars on the host, PRODUCTION SERVED A
# FIFTH OF WHAT WE ADVERTISED. Verified in the live quota ledger: anonymous
# buckets cap at 20 while every public surface says 100.
#
# Under-delivering against your own published numbers is worse than pricing
# badly. It is the same defect as the base-URL split fixed the same day: two
# modules reading one variable and disagreeing about what it means. There is
# one source now, and a test asserts these match the advertised figures.
def _limits() -> tuple[int, int]:
    """(free_key_limit, anonymous_limit).

    The env var still wins AT CALL TIME, and config supplies the default. That
    ordering is not incidental:

      * reading config alone would freeze the limits at import, which silently
        broke every test that raises or lowers a quota via monkeypatched env to
        exercise the exhaustion path - seven of them, and they failed only when
        run together, so a single-test rerun looked fine;
      * hardcoding the default here is the original bug.

    So: env for runtime control, config for the value nobody has overridden.
    """
    try:
        import config
        free_default = int(config.FREE_DATA_QUOTA_PER_DAY)
        anon_default = int(config.ANON_DATA_QUOTA_PER_DAY)
    except Exception:  # noqa: BLE001 - never let a config import break billing
        # Matches config.py's defaults. A fallback that silently reverts a
        # published number is exactly how this bug happened in the first place.
        free_default, anon_default = 500, 100
    return (int(os.getenv("FREE_DATA_QUOTA_PER_DAY", str(free_default))),
            int(os.getenv("ANON_DATA_QUOTA_PER_DAY", str(anon_default))))


def _get_free_limit() -> int:
    return _limits()[0]


def _get_anon_limit() -> int:
    return _limits()[1]


def _resolve_key_id(token: str) -> Optional[str]:
    """Return the key_id from a valid X-Agent-Identity bearer token, or None."""
    if not token or token in ("", "anonymous"):
        return None
    try:
        from agent_interface.identity import validate_token
        result = validate_token(token)
        if result and result.valid and result.identity:
            return result.identity.agent_id
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_free_tier_key(key_id: Optional[str]) -> bool:
    """True if this key_id was minted as a free-tier key (prefix "free_")."""
    return bool(key_id and str(key_id).startswith("free_"))


# ---------------------------------------------------------------------------
# Free-key in-memory counter
# ---------------------------------------------------------------------------

def _consume_free_key_data(key_id: str) -> tuple[bool, int]:
    """Consume one data op from the free-key daily data quota.

    Returns (allowed, remaining_after).
    Thread-safety: in-memory dict ops are GIL-protected; acceptable for a
    process-bound counter (resets on restart -- fine for a best-effort daily cap).
    """
    today = _today_utc()
    limit = _get_free_limit()
    entry = _free_key_data_daily.get(key_id)
    if not entry or entry.get("date") != today:
        _free_key_data_daily[key_id] = {"count": 1, "date": today}
        return True, limit - 1
    if entry["count"] >= limit:
        return False, 0
    entry["count"] += 1
    return True, limit - entry["count"]


def get_free_key_data_remaining(key_id: str) -> int:
    """Return remaining free data quota for today for this free key. Never raises."""
    today = _today_utc()
    limit = _get_free_limit()
    entry = _free_key_data_daily.get(key_id)
    if not entry or entry.get("date") != today:
        return limit
    return max(0, limit - entry.get("count", 0))


# ---------------------------------------------------------------------------
# Anonymous IP-based Supabase counter
# ---------------------------------------------------------------------------
#
# ROUTED THROUGH THE anon_data_quota_consume RPC, NOT RAW REST CALLS
# (2026-09-23, closing the dormant bug in
# docs/reviews/2026-09-23-agentbroker-anon-quota-root-cause.md, commit
# bb62169). The old version here did a SELECT, then an INSERT-or-PATCH, as
# three separate PostgREST calls using whichever key
# SUPABASE_SERVICE_KEY-or-SUPABASE_ANON_KEY resolved to. On the VPS -- the
# box that has actually served hatchloop.dev/api.hatchloop.dev since the
# 2026-09-22 cutover -- that resolves to the anon key, because
# SUPABASE_SERVICE_KEY is deliberately never shipped there (board row 206
# item 1; see ops/vps/deploy_agentbroker_vps.py's NEVER_SHIP_TO_CONTAINER,
# unchanged by this fix and must stay that way). Two things then happened,
# neither of which raised or logged above debug:
#
#   1. The anon key's SELECT on `anon_data_quota` returned HTTP 200 with an
#      empty array -- RLS silently filtering the row, not an error -- so
#      every call looked like "no prior entry", forever.
#   2. The anon key's INSERT was rejected by RLS (also not a Python
#      exception -- insert_row() swallows it and returns None), and that
#      None was never checked, so the call was treated as a successful
#      first write regardless.
#
# Net effect: an unverifiable counter that was silently, permanently
# permissive -- exactly the shape the module docstring's "NEVER run the
# tool for free beyond quota" invariant forbids, and it would have
# reproduced the instant DATA_METERING_ENABLED were ever set true on this
# box, with nothing in any log explaining why.
#
# THE FIX. `anon_data_quota_consume` (sql/agentbroker/002_anon_data_quota_
# security_definer_rpc.sql, NOT YET APPLIED -- see that file and
# sql/agentbroker/README.md) is a narrow SECURITY DEFINER function that does
# the whole upsert + day-rollover-reset + limit-check + increment
# atomically, server-side, under a row lock, in ONE call. The anon role has
# EXECUTE on the function and NO grant on the table at all (the direct-table
# door this bug exploited is closed). There is no longer a separate "did
# the write land" question for THIS function to answer wrong: `rpc()`
# itself raises on anything other than a 2xx response with a decodable
# body (storage/supabase_client.py's own contract, already used this way by
# storage/outcome_store.py for the identical board-row-206 problem on
# `operations`), so an unreachable, unauthorized, or not-yet-migrated
# function is a Python exception here, not a quietly-accepted empty
# success. `_verify_response_shape` below is the second, cheaper half of
# "verify its own write": even a 2xx response is checked for the exact
# shape the function is defined to return before its `allowed` field is
# trusted -- belt-and-suspenders against a future schema drift silently
# being read as `allowed=True`.


class _AnonQuotaRpcFailure(RuntimeError):
    """Raised internally when the RPC call did not produce a trustworthy
    verdict -- either it raised (network/permission/deployment failure) or
    it returned a 2xx body missing the shape this function contracts to
    return. Carries `kind` so the caller can log misconfiguration and
    outage distinguishably instead of collapsing both into one debug line
    (the exact ask: today both look identical and both log at debug)."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind  # "misconfigured" | "unconfigured" | "outage" | "bad_response_shape"


def _classify_rpc_exception(exc: Exception) -> tuple[str, str]:
    """Turn whatever storage.supabase_client.rpc() raised into a (kind,
    human_detail) pair.

    rpc() raises a plain RuntimeError with a message it builds itself (see
    its own docstring/source) -- there is no structured status code
    attached today, so this reads the message it already contracts to
    produce rather than inventing a second parallel channel. Buckets:

      * "unconfigured"  -- SUPABASE_URL / a key were never set for this
        process. Common and expected in local dev / most unit tests (this
        repo's tests deliberately run unconfigured); not escalated to
        misconfigured severity.
      * "misconfigured"  -- the request reached Supabase and was refused
        for a reason a human must fix: permission denied (401/403), the
        function does not exist yet (404 / PGRST202 -- exactly what this
        RPC returns TODAY, before sql/agentbroker/002_*.sql is applied), or
        a client-shaped error (400/422, e.g. a parameter mismatch). This is
        never a "try again later" condition.
      * "outage"  -- a transport failure (DNS, connection refused, TLS,
        timeout) or a 5xx from Supabase itself. Expected to self-resolve;
        this is the ONE case the module docstring's fail-open design is
        actually for.
      * "bad_response_shape"  -- rpc() returned normally (2xx, valid JSON)
        but the body was not proven at all (see _verify_response_shape) --
        never reached from here, kept only so both call sites share one
        exception type.
    """
    msg = str(exc)
    if "not configured" in msg:
        return "unconfigured", msg
    if "transport error" in msg:
        return "outage", msg
    m = re.search(r"HTTP (\d{3})", msg)
    if m:
        status = int(m.group(1))
        if status in (400, 401, 403, 404, 422):
            return "misconfigured", msg
        return "outage", msg
    if "JSON decode error" in msg:
        # A 2xx whose body is not JSON -- e.g. a proxy/WAF interstitial in
        # front of Supabase. Ambiguous by nature; treated as an outage
        # (transport-adjacent) rather than misconfigured, since it is not a
        # credential/grant problem this workspace can fix in SQL.
        return "outage", msg
    return "outage", msg  # unrecognised shape: never invent "misconfigured" without evidence


def _verify_response_shape(payload) -> dict:
    """The second half of "verify its own write": even a 2xx, valid-JSON
    response from anon_data_quota_consume is checked against the exact
    shape the function contracts to return before `allowed` is trusted.
    Raises _AnonQuotaRpcFailure(kind="bad_response_shape") otherwise --
    never returns a best-guess default, which is how the original bug's
    sibling ("assume success because nothing raised") would reappear one
    layer up.
    """
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("allowed"), bool)
        or not isinstance(payload.get("remaining"), int)
        or not isinstance(payload.get("count"), int)
    ):
        raise _AnonQuotaRpcFailure(
            "bad_response_shape",
            f"anon_data_quota_consume returned an unexpected shape: {payload!r} "
            f"(expected a dict with allowed: bool, remaining: int, count: int)",
        )
    return payload


async def _consume_anon_data(ip: str) -> tuple[bool, int]:
    """Consume one data op from the anon IP daily quota (Supabase-backed).

    Returns (allowed, remaining_after).
    Fail-open: allows the call if IP is empty, Supabase is unconfigured, or
    the RPC call fails for any reason (misconfiguration or outage alike --
    this function never blocks a caller because OUR infrastructure is
    broken; that is a deliberate availability choice, unchanged by this
    fix, and is exactly what scripts/check_anon_quota_credential.py exists
    to catch BEFORE DATA_METERING_ENABLED is ever flipped on, rather than
    relying on this fail-open path to announce it at runtime). What
    changed: every failure now logs LOUDLY (warning/error, never debug)
    with a `kind` that distinguishes "this credential cannot see the table
    at all" from a genuine transient outage -- see
    _classify_rpc_exception's docstring. The anon quota is still
    best-effort; minor over-counting at the edges (race conditions between
    two processes) is acceptable and unchanged.
    """
    limit = _get_anon_limit()

    if not ip:
        # No IP available -- use the generous fallback.
        return True, limit

    today = _today_utc()
    # Key: sha256(ip:date) -- avoids storing raw IPs in Supabase.
    raw = f"{ip}:{today}".encode()
    bucket = hashlib.sha256(raw).hexdigest()

    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    svc_key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not svc_key:
        return True, limit  # no Supabase config -- generous fallback, silent (expected in dev/tests)

    try:
        from storage.supabase_client import rpc

        # Hard 2-second timeout, same budget as before. A hang raises
        # asyncio.TimeoutError, which the except below classifies as an
        # "outage" (its message contains neither "not configured" nor
        # "HTTP ###" nor "JSON decode error", so it falls through to the
        # final outage bucket -- see _classify_rpc_exception).
        payload = await asyncio.wait_for(
            rpc("anon_data_quota_consume", {
                "p_bucket_key": bucket,
                "p_quota_date": today,
                "p_limit": limit,
            }),
            timeout=2.0,
        )
        result = _verify_response_shape(payload)
        if result["allowed"]:
            return True, result["remaining"]
        return False, 0

    except _AnonQuotaRpcFailure as exc:
        # Our own shape check tripped -- always a real defect, never noise.
        log.error(
            "anon_data_quota_unavailable kind=%s bucket=%.8s... err=%s -- "
            "failing OPEN (allow) per the module's fail-open design; this is "
            "NOT expected to happen and needs investigation, not a retry",
            exc.kind, bucket, exc,
        )
        return True, limit

    except Exception as exc:  # noqa: BLE001 -- includes asyncio.TimeoutError
        kind, detail = _classify_rpc_exception(exc)
        if kind in ("misconfigured",):
            # Loud and at error level on purpose: if this ever fires while
            # DATA_METERING_ENABLED=true, anonymous callers are getting
            # unlimited free calls again, silently, right now -- this is
            # the exact defect this fix exists to make impossible to miss.
            log.error(
                "anon_data_quota_unavailable kind=misconfigured bucket=%.8s... "
                "detail=%s -- this credential cannot use anon_data_quota_consume "
                "(permission denied, or sql/agentbroker/002_anon_data_quota_"
                "security_definer_rpc.sql has not been applied yet). Failing "
                "OPEN (allow) per the module's fail-open design, but this is a "
                "configuration defect, not an outage -- run "
                "scripts/check_anon_quota_credential.py before enabling "
                "metering on this box.",
                bucket, detail,
            )
        elif kind == "outage":
            log.warning(
                "anon_data_quota_unavailable kind=outage bucket=%.8s... detail=%s "
                "-- failing OPEN (allow); treated as a transient Supabase "
                "availability issue, expected to self-resolve",
                bucket, detail,
            )
        else:  # "unconfigured" -- expected in dev/tests; quiet by design
            log.debug(
                "anon_data_quota_unavailable kind=%s bucket=%.8s... detail=%s",
                kind, bucket, detail,
            )
        return True, limit  # fail-open


# ---------------------------------------------------------------------------
# Public entry point called from mcp_server._h_tools_call
# ---------------------------------------------------------------------------

async def consume_data_quota(
    name: str,
    token: str,
    ip: str,
    headers: Optional[dict] = None,
) -> dict:
    """Check and consume one premium-data-tool op from the caller's free quota.

    Returns:
      {"allowed": True, "remaining": int}     -- within quota, call may proceed free
      {"allowed": False, "response": dict}    -- beyond quota, honest failure

    The "response" dict on failure has status="failure", reason_code="free_quota_exceeded",
    cost.amount=0.0, and a human_message with upgrade paths. Tool is NOT dispatched.

    Never raises: any unexpected error falls back to allow (fail-open for quota).
    """
    try:
        key_id = _resolve_key_id(token)

        if _is_free_tier_key(key_id):
            limit = _get_free_limit()
            allowed, remaining = _consume_free_key_data(key_id)
            if allowed:
                return {"allowed": True, "remaining": remaining}
            return {
                "allowed": False,
                "response": {
                    "status": "failure",
                    "reason_code": "free_quota_exceeded",
                    "human_message": (
                        f"Free daily limit reached ({limit}/day for email-verified keys). "
                        f"Get a free key for more daily quota at {_FREE_KEY_URL}" + _credits_clause() + "."
                    ),
                    "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"},
                },
            }

        # Anonymous caller (no key, unrecognised token, or non-free key not yet
        # handled by x402/credits gates upstream).
        limit = _get_anon_limit()
        allowed, remaining = await _consume_anon_data(ip)
        if allowed:
            return {"allowed": True, "remaining": remaining}
        return {
            "allowed": False,
            "response": {
                "status": "failure",
                "reason_code": "free_quota_exceeded",
                "human_message": (
                    f"Free daily limit reached ({limit}/day for anonymous callers). "
                    f"Get a free key for more at {_FREE_KEY_URL}" + _credits_clause() + "."
                ),
                "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"},
            },
        }

    except Exception as exc:  # noqa: BLE001
        # Any unexpected failure: fail-open so a quota bug never blocks a caller.
        log.error("consume_data_quota unexpected error name=%s err=%s", name, exc)
        return {"allowed": True, "remaining": -1}
