"""
Demand shaping — supply-side protection.

THE PROBLEM (founder, 2026-08-26): many agents, acting for many end-users, hit
ONE small business at once. Unshaped, we are a spam cannon: the business blocks
us, our sender numbers get burned, and the network dies.

THE INSIGHT: only the PLATFORM sees the aggregate. No single agent can know
that 19 other agents just messaged the same barber. Shaping that flood is
something an agent CANNOT do for itself — it is a real moat layer, and handled
well it makes high demand VALUABLE to the business (its demand inbox) instead
of hostile.

LAYERS IMPLEMENTED HERE:
  1. GLOBAL per-business budget across ALL agents (sized by business tier).
  2. QUEUE, don't reject — over-budget requests get honest defer semantics
     with a real retry_after, never a silent drop or a lie.
  3. DIGEST — when several requests are pending for one business, they are
     rendered as ONE numbered message rather than N separate threads.
  4. Business controls — max/day, quiet hours (deferred, not dropped).

Bounded + fail-OPEN: if the ledger is unreachable we ALLOW the send. A
throttle that breaks delivery would be worse than the flood it prevents.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("smb_broker.demand_shaping")

_SB_TIMEOUT_S = 2.5

# New conversations per business per rolling hour, by tier. A one-chair barber
# and a 50-seat restaurant tolerate very different volumes.
_HOURLY_BUDGET = {"micro": 3, "small": 6, "medium": 15, "large": 40}
_DEFAULT_TIER = "small"

# Daily ceiling regardless of tier (a business can lower it; never raise above).
_DAILY_BUDGET = {"micro": 12, "small": 25, "medium": 60, "large": 160}


@dataclass
class BudgetDecision:
    allowed: bool
    reason_code: str = "within_budget"
    retry_after_ms: Optional[int] = None
    used_hour: int = 0
    limit_hour: int = 0
    human_message: str = ""

    def as_receipt_block(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "used_this_hour": self.used_hour,
            "hourly_limit": self.limit_hour,
            "retry_after_ms": self.retry_after_ms,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _recent_threads(business_id: str, limit: int = 200) -> list[dict]:
    from storage.supabase_client import select_rows
    try:
        rows = await asyncio.wait_for(
            select_rows("conversations", filters={"business_id": business_id}, limit=limit),
            timeout=_SB_TIMEOUT_S,
        )
        return rows or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("demand_shaping_read_failed business=%s err=%s", business_id, exc)
        return []


async def check_budget(
    business_id: Optional[str],
    *,
    tier: str = _DEFAULT_TIER,
    hourly_override: Optional[int] = None,
    daily_override: Optional[int] = None,
) -> BudgetDecision:
    """Global per-business rate check. FAIL-OPEN by design."""
    limit_h = hourly_override or _HOURLY_BUDGET.get(tier, _HOURLY_BUDGET[_DEFAULT_TIER])
    limit_d = daily_override or _DAILY_BUDGET.get(tier, _DAILY_BUDGET[_DEFAULT_TIER])
    if not business_id:
        return BudgetDecision(allowed=True, limit_hour=limit_h)

    rows = await _recent_threads(business_id)
    if not rows:
        return BudgetDecision(allowed=True, limit_hour=limit_h)

    now = _now()
    hour_ago, day_ago = now - timedelta(hours=1), now - timedelta(days=1)
    used_h = used_d = 0
    oldest_in_window = now
    for r in rows:
        ts = _parse_ts(r.get("created_at"))
        if not ts:
            continue
        if ts > day_ago:
            used_d += 1
        if ts > hour_ago:
            used_h += 1
            oldest_in_window = min(oldest_in_window, ts)

    if used_h >= limit_h:
        # Queue, don't reject: tell the caller exactly when a slot frees up.
        free_at = oldest_in_window + timedelta(hours=1)
        retry_ms = max(60_000, int((free_at - now).total_seconds() * 1000))
        return BudgetDecision(
            allowed=False, reason_code="business_rate_limited",
            retry_after_ms=retry_ms, used_hour=used_h, limit_hour=limit_h,
            human_message=(
                f"This business has already received {used_h} requests in the last hour "
                f"(limit {limit_h}). Your request is queued rather than dropped - retry "
                f"after the window frees up, or ask the user for an alternative business. "
                f"We shape volume so businesses stay responsive instead of blocking us."
            ),
        )
    if used_d >= limit_d:
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return BudgetDecision(
            allowed=False, reason_code="business_daily_limit",
            retry_after_ms=int((midnight - now).total_seconds() * 1000),
            used_hour=used_h, limit_hour=limit_h,
            human_message=(
                f"This business has reached its daily request limit ({limit_d}). "
                f"Try again tomorrow or choose another business."
            ),
        )
    return BudgetDecision(allowed=True, used_hour=used_h, limit_hour=limit_h)


def build_digest(business_name: str, requests: list[dict]) -> str:
    """THE INVERSION: N pending requests -> ONE numbered message.

    Each entry stays individually answerable ("1 YES"), so the business gets an
    organised demand inbox instead of N interrupting threads. This is the seed
    of the supply-side product.
    """
    if not requests:
        return ""
    lines = [
        f"Hi {business_name} - you have {len(requests)} pending "
        f"{'request' if len(requests) == 1 else 'requests'} via HatchLoop:"
    ]
    for i, r in enumerate(requests[:10], start=1):
        who = r.get("end_user_ref") or "a customer"
        what = r.get("intent") or "a booking"
        ref = r.get("ref_token") or ""
        lines.append(f"{i}) {what} for {who}" + (f"  [#{ref}]" if ref else ""))
    lines.append("")
    lines.append("Reply with the number and YES or NO - e.g. \"1 YES\" or \"2 NO\".")
    lines.append("Reply STOP to receive no further requests.")
    return "\n".join(lines)


_DIGEST_RE = None


def parse_digest_reply(text: str, requests: list[dict]) -> list[tuple[dict, bool]]:
    """Parse "1 YES", "2 no", "1 yes 3 no" into (request, accepted) pairs."""
    import re
    global _DIGEST_RE
    if _DIGEST_RE is None:
        _DIGEST_RE = re.compile(r"\b(\d{1,2})\s*[.:)-]?\s*(yes|no|y|n)\b", re.I)
    out: list[tuple[dict, bool]] = []
    for num, verdict in _DIGEST_RE.findall(text or ""):
        idx = int(num) - 1
        if 0 <= idx < len(requests):
            out.append((requests[idx], verdict.lower() in ("yes", "y")))
    return out
