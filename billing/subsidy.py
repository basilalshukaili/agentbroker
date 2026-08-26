"""
Subsidy ledger — what our generosity actually costs, and a hard ceiling on it.

FOUNDER DECISION (2026-08-26): "I do not care if we lose, put that this is
limited and later prices will change, the tools will cost more credits in
future, i do no mind losing now to attract people and gain trust, but NOT HUGE
LOSS."

Two halves, and the second is the engineering.

  "I don't mind losing"   -> we may price BELOW cost deliberately.
  "but not huge loss"     -> that is a BOUND, and a bound nobody measures is a
                             hope. This module measures it.

WHAT SUBSIDY MEANS HERE: for one call, (what the vendor charged us) minus (what
we charged the customer). Positive = we paid the difference. We record it per
call, sum it per calendar month, and compare against a ceiling.

WHY A CEILING RATHER THAN JUST CHEAP PRICES: a loss-leader with a free tier is
an open invoice. The per-call loss is pennies; the DANGER is volume - a single
enthusiastic agent, or an abusive one, turning pennies into a real bill while
every individual call looks perfectly reasonable. Per-call generosity is safe
only when the aggregate is bounded.

WHAT HAPPENS AT THE CEILING: we do NOT start charging more than we advertised -
that would break the price we published, and the honesty invariants forbid it.
Instead the subsidised tools stop being free-tier-available and require credits,
which is the honest way to stop bleeding: the price never changes, only who is
eligible for the subsidy. The founder is alerted well before that point.

BILL-SAFETY IS CARDINAL, and this is how that rule survives a deliberate
loss-leader.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("smb_broker.subsidy")

# Monthly ceiling in USD. Deliberately small: the founder's whole operation runs
# on a $13 DeepSeek balance and a $15 Twilio balance, so "huge" here is tens of
# dollars, not thousands. Raise it consciously, never by accident.
MONTHLY_CEILING_USD: float = float(os.getenv("SUBSIDY_MONTHLY_CEILING_USD", "50"))

# Warn the founder at this fraction of the ceiling, so the first he hears of it
# is not the tools changing behaviour.
WARN_AT = 0.60

_LEDGER = "subsidy_ledger"


def _month_key(ts: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(ts or time.time(), timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def compute(our_cost_usd: float, charged_usd: float) -> float:
    """Subsidy for one call. Never negative - a profit is not a negative loss
    for ceiling purposes, because profit on one tool must not silently license
    unlimited loss on another."""
    return max(0.0, round(float(our_cost_usd) - float(charged_usd), 6))


async def record(tool: str, our_cost_usd: float, charged_usd: float,
                 agent_id: Optional[str] = None) -> float:
    """Log one call's subsidy. Returns the amount. Never raises, never blocks
    a send - a failure here loses a measurement, not a message."""
    amount = compute(our_cost_usd, charged_usd)
    if amount <= 0:
        return 0.0
    try:
        from storage.supabase_client import insert_row
        import asyncio
        await asyncio.wait_for(insert_row(_LEDGER, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "month": _month_key(),
            "tool": tool,
            "agent_id": agent_id,
            "our_cost_usd": round(float(our_cost_usd), 6),
            "charged_usd": round(float(charged_usd), 6),
            "subsidy_usd": amount,
        }), timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("subsidy_record_failed tool=%s err=%s", tool, exc)
    return amount


async def month_total(month: Optional[str] = None) -> float:
    """Total subsidy this calendar month, in USD."""
    month = month or _month_key()
    try:
        from storage.supabase_client import select_rows
        rows = await select_rows(_LEDGER, filters={"month": month}, limit=10000)
        return round(sum(float(r.get("subsidy_usd") or 0) for r in (rows or [])), 4)
    except Exception as exc:  # noqa: BLE001
        logger.warning("subsidy_total_failed err=%s", exc)
        return 0.0


async def status() -> dict:
    """Where we are against the ceiling. Safe to call from a monitor."""
    total = await month_total()
    ceiling = MONTHLY_CEILING_USD
    frac = (total / ceiling) if ceiling > 0 else 0.0
    if frac >= 1.0:
        state = "ceiling_reached"
    elif frac >= WARN_AT:
        state = "approaching"
    else:
        state = "ok"
    return {
        "month": _month_key(),
        "subsidy_usd": total,
        "ceiling_usd": ceiling,
        "used_fraction": round(frac, 4),
        "state": state,
        "free_tier_should_close": frac >= 1.0,
    }


async def free_tier_open() -> bool:
    """False once the month's subsidy ceiling is spent.

    The PRICE never changes when this flips - only eligibility for the
    subsidised free tier. Changing an advertised price mid-month would break
    the thing we publish; withdrawing a free tier we described as limited-time
    does not.
    """
    try:
        s = await status()
        return not s["free_tier_should_close"]
    except Exception:  # noqa: BLE001
        # Fail OPEN: a measurement outage must not switch off the product. The
        # ceiling is a budget guard, not a safety interlock, and the founder is
        # alerted long before this matters.
        return True
