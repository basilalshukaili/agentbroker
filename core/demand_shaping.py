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


class _LedgerUnavailable(RuntimeError):
    """The conversations ledger could not be read, so a budget cannot be judged."""


# Fail-open events, most recent last. Bounded - this is a signal, not a log.
# The health monitor reads it so a silently-disabled protection layer becomes
# visible instead of looking like a quiet day.
_FAIL_OPEN: list[float] = []
_FAIL_OPEN_MAX = 200


def _record_fail_open() -> None:
    _FAIL_OPEN.append(_now().timestamp())
    if len(_FAIL_OPEN) > _FAIL_OPEN_MAX:
        del _FAIL_OPEN[:-_FAIL_OPEN_MAX]


def fail_open_count(window_s: int = 3600) -> int:
    """How many budget checks ran BLIND in the last window."""
    cut = _now().timestamp() - window_s
    return sum(1 for ts in _FAIL_OPEN if ts >= cut)


@dataclass
class BudgetDecision:
    allowed: bool
    reason_code: str = "within_budget"
    retry_after_ms: Optional[int] = None
    used_hour: int = 0
    limit_hour: int = 0
    human_message: str = ""
    # True when we allowed the send WITHOUT being able to check the budget.
    degraded: bool = False

    def as_receipt_block(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "used_this_hour": self.used_hour,
            "hourly_limit": self.limit_hour,
            "retry_after_ms": self.retry_after_ms,
            **({"degraded": True,
                "degraded_reason": "demand ledger unreachable - allowed without a budget check"}
               if self.degraded else {}),
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ceil_ms(delta: timedelta) -> int:
    """Milliseconds, rounded UP, plus a 1s margin.

    A retry_after that lands even a millisecond early gets refused again, which
    makes our advertised ETA a lie. Round up and add margin.
    """
    import math
    return int(math.ceil(delta.total_seconds() * 1000)) + 1000


def _parse_ts(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _recent_threads(business_id: str, since: datetime, limit: int = 500) -> list[dict]:
    """Threads for this business created since `since`, NEWEST FIRST.

    Both the time window and the ordering are pushed into the query: reading an
    unordered 200-row slice of a repeat business's history and filtering it
    client-side meant a busy business could escape the budget entirely, because
    the arbitrary rows returned were mostly old (review 2026-08-26).
    """
    from storage.supabase_client import select_rows
    try:
        rows = await asyncio.wait_for(
            select_rows("conversations", filters={"business_id": business_id},
                        limit=limit, order="created_at.desc",
                        gte={"created_at": since.isoformat()}),
            timeout=_SB_TIMEOUT_S,
        )
        return rows or []
    except Exception as exc:  # noqa: BLE001
        # An empty list used to mean TWO different things - "this business is
        # quiet" and "we could not look" - and the caller could not tell them
        # apart. So a ledger outage silently switched the whole protection off
        # and looked exactly like a calm day (flagged by an independent review
        # 2026-08-26, confirmed here).
        #
        # Failing open stays correct: a throttle that breaks delivery is worse
        # than the flood it prevents. What was wrong was the SILENCE. Raising a
        # distinct error lets check_budget still allow the send while recording
        # that it decided blind.
        logger.warning("demand_shaping_read_failed business=%s err=%s", business_id, exc)
        raise _LedgerUnavailable(str(exc)) from exc


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

    now = _now()
    day_ago = now - timedelta(days=1)
    try:
        rows = await _recent_threads(business_id, since=day_ago)
    except _LedgerUnavailable:
        # FAIL OPEN, deliberately - but say so. The send proceeds; the receipt
        # carries `degraded`, and the health monitor counts these so a
        # protection layer that has quietly switched off gets noticed.
        _record_fail_open()
        return BudgetDecision(allowed=True, reason_code="shaping_degraded",
                              limit_hour=limit_h, degraded=True)
    if not rows:
        return BudgetDecision(allowed=True, limit_hour=limit_h)

    hour_ago = now - timedelta(hours=1)
    # Keep the actual timestamps: retry_after must be computed from the slot that
    # genuinely frees capacity, not merely the oldest one (review 2026-08-26 -
    # with used >> limit the old formula told callers to retry while still over
    # budget, i.e. an dishonest ETA).
    in_day = sorted(ts for ts in (_parse_ts(r.get("created_at")) for r in rows) if ts)
    in_hour = [ts for ts in in_day if ts > hour_ago]
    used_d, used_h = len(in_day), len(in_hour)

    if used_h >= limit_h:
        # Queue, don't reject: the (used_h - limit_h)-th oldest in-window thread
        # is the one whose expiry actually opens a slot.
        idx = min(used_h - limit_h, len(in_hour) - 1)
        free_at = in_hour[idx] + timedelta(hours=1)
        # Round UP (+1s margin): int() truncation put the advertised time a
        # fraction BEFORE the slot actually frees, so an agent retrying exactly
        # when told was refused again - a small but real dishonesty. Always err
        # on the side of the retry succeeding.
        retry_ms = max(60_000, _ceil_ms(free_at - now))
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
        # Rolling 24h window, so the honest ETA is when enough of the oldest
        # threads age out - not an arbitrary calendar midnight.
        idx_d = min(used_d - limit_d, len(in_day) - 1)
        free_at_d = in_day[idx_d] + timedelta(days=1)
        return BudgetDecision(
            allowed=False, reason_code="business_daily_limit",
            retry_after_ms=max(60_000, _ceil_ms(free_at_d - now)),
            used_hour=used_h, limit_hour=limit_h,
            human_message=(
                f"This business has reached its 24h request limit ({limit_d}). "
                f"Queued, not dropped - retry after the window frees up "
                f"(see retry_after_ms) or choose another business."
            ),
        )
    return BudgetDecision(allowed=True, used_hour=used_h, limit_hour=limit_h)


DIGEST_MAX = 10


def digest_slice(requests: list[dict]) -> list[dict]:
    """The addressable set: exactly what build_digest renders.

    Callers MUST parse replies against this same slice - the header used to
    announce len(requests) while rendering only 10, so "12 YES" was accepted for
    an item the business never saw (review 2026-08-26).
    """
    return requests[:DIGEST_MAX]


def build_digest(business_name: str, requests: list[dict]) -> str:
    """THE INVERSION: N pending requests -> ONE numbered message.

    Each entry stays individually answerable ("1 YES"), so the business gets an
    organised demand inbox instead of N interrupting threads. This is the seed
    of the supply-side product.
    """
    if not requests:
        return ""
    shown = digest_slice(requests)
    more = len(requests) - len(shown)
    lines = [
        f"Hi {business_name} - you have {len(shown)} pending "
        f"{'request' if len(shown) == 1 else 'requests'} via HatchLoop"
        + (f" (showing the first {len(shown)} of {len(requests)})" if more > 0 else "")
        + ":"
    ]
    for i, r in enumerate(shown, start=1):
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
    shown = digest_slice(requests)      # only what the business actually saw
    out: list[tuple[dict, bool]] = []
    for num, verdict in _DIGEST_RE.findall(text or ""):
        idx = int(num) - 1
        if 0 <= idx < len(shown):
            out.append((shown[idx], verdict.lower() in ("yes", "y")))
    return out
