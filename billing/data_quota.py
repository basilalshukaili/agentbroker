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
    """(free_key_limit, anonymous_limit) from the single source of truth."""
    try:
        import config
        return int(config.FREE_DATA_QUOTA_PER_DAY), int(config.ANON_DATA_QUOTA_PER_DAY)
    except Exception:  # noqa: BLE001 - never let a config import break billing
        # Match config.py's defaults, not the retired ones. A fallback that
        # silently reverts a published price is how this bug happened.
        return (int(os.getenv("FREE_DATA_QUOTA_PER_DAY", "500")),
                int(os.getenv("ANON_DATA_QUOTA_PER_DAY", "100")))


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

async def _consume_anon_data(ip: str) -> tuple[bool, int]:
    """Consume one data op from the anon IP daily quota (Supabase-backed).

    Returns (allowed, remaining_after).
    Fail-open: allows the call if IP is empty, Supabase is unreachable, or
    any error occurs. The anon quota is best-effort; minor over-counting at
    the edges (race conditions) is acceptable.
    """
    limit = _get_anon_limit()

    if not ip:
        # No IP available -- use the generous fallback.
        return True, limit

    today = _today_utc()
    # Key: sha256(ip:date) -- avoids storing raw IPs in Supabase.
    raw = f"{ip}:{today}".encode()
    bucket = hashlib.sha256(raw).hexdigest()

    try:
        sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        svc_key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
        if not sb_url or not svc_key:
            return True, limit  # no Supabase config -- generous fallback

        from storage.supabase_client import select_rows, insert_row
        import httpx

        # Hard 2-second timeout on every Supabase call -- a hang is NOT an
        # exception and would bypass the except Exception fail-open below.
        # asyncio.TimeoutError IS a subclass of Exception, so the outer
        # except block catches it and returns fail-open.
        rows = await asyncio.wait_for(
            select_rows("anon_data_quota", filters={"bucket_key": bucket}, limit=1),
            timeout=2.0,
        )
        if not rows:
            # First call today for this IP bucket.
            await asyncio.wait_for(
                insert_row("anon_data_quota", {
                    "bucket_key": bucket,
                    "count": 1,
                    "quota_date": today,
                }),
                timeout=2.0,
            )
            return True, limit - 1

        row = rows[0]
        count = int(row.get("count", 0))
        stored_date = row.get("quota_date", "")

        if stored_date != today:
            # New UTC day -- reset the counter for this bucket.
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.patch(
                    f"{sb_url}/rest/v1/anon_data_quota",
                    headers={
                        "apikey": svc_key,
                        "Authorization": f"Bearer {svc_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    params={"bucket_key": f"eq.{bucket}"},
                    json={"count": 1, "quota_date": today},
                )
            return True, limit - 1

        if count >= limit:
            return False, 0

        # Increment.
        new_count = count + 1
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.patch(
                f"{sb_url}/rest/v1/anon_data_quota",
                headers={
                    "apikey": svc_key,
                    "Authorization": f"Bearer {svc_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                params={"bucket_key": f"eq.{bucket}"},
                json={"count": new_count},
            )
        return True, limit - new_count

    except Exception as exc:  # noqa: BLE001
        log.debug("anon_data_quota fallback ip=%.8s err=%s", ip, exc)
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
                        f"Get a free key for more daily quota at {_FREE_KEY_URL}, "
                        f"or top up credits at {_UPGRADE_URL}."
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
                    f"Get a free key for more at {_FREE_KEY_URL}, "
                    f"or top up credits at {_UPGRADE_URL}."
                ),
                "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"},
            },
        }

    except Exception as exc:  # noqa: BLE001
        # Any unexpected failure: fail-open so a quota bug never blocks a caller.
        log.error("consume_data_quota unexpected error name=%s err=%s", name, exc)
        return {"allowed": True, "remaining": -1}
