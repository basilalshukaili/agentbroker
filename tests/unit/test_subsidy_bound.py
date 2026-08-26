"""
A deliberate loss must still be a BOUNDED loss.

Founder decision (2026-08-26): "I do not care if we lose... i do no mind losing
now to attract people and gain trust, but NOT HUGE LOSS."

The first half is a pricing decision. The second half is an engineering
requirement: a bound nobody measures is a hope. These tests pin the mechanism
that makes it real.

The danger is never the per-call loss - it is pennies. It is VOLUME: one
enthusiastic or abusive agent turning pennies into a real bill while every
individual call still looks perfectly reasonable.
"""
from __future__ import annotations

import asyncio

import pytest

from billing import subsidy


def test_subsidy_is_cost_minus_charge():
    assert subsidy.compute(0.30, 0.20) == 0.10
    assert subsidy.compute(0.0079, 0.02) == 0.0      # profitable = no subsidy
    assert subsidy.compute(0.1092, 0.05) == pytest.approx(0.0592)


def test_profit_never_offsets_loss_for_ceiling_purposes():
    """Profit on a cheap tool must not silently license unlimited loss on an
    expensive one - they are different products with different abuse curves."""
    assert subsidy.compute(0.01, 5.00) == 0.0


def test_ceiling_is_small_and_explicit():
    """The whole operation runs on ~$13 of DeepSeek and ~$15 of Twilio credit.
    'Huge' here is tens of dollars, so the default ceiling must be modest."""
    assert 0 < subsidy.MONTHLY_CEILING_USD <= 200


def test_status_reports_ok_warn_and_ceiling(monkeypatch):
    async def _total(month=None):
        return _total.value
    monkeypatch.setattr(subsidy, "month_total", _total)

    _total.value = 1.0
    assert asyncio.run(subsidy.status())["state"] == "ok"

    _total.value = subsidy.MONTHLY_CEILING_USD * (subsidy.WARN_AT + 0.05)
    assert asyncio.run(subsidy.status())["state"] == "approaching"

    _total.value = subsidy.MONTHLY_CEILING_USD
    s = asyncio.run(subsidy.status())
    assert s["state"] == "ceiling_reached"
    assert s["free_tier_should_close"] is True


def test_free_tier_closes_at_the_ceiling(monkeypatch):
    async def _total(month=None):
        return subsidy.MONTHLY_CEILING_USD + 1
    monkeypatch.setattr(subsidy, "month_total", _total)
    assert asyncio.run(subsidy.free_tier_open()) is False


def test_measurement_outage_fails_OPEN(monkeypatch):
    """A broken meter must not switch the product off. The ceiling is a budget
    guard, not a safety interlock - and the founder is alerted long before it
    binds."""
    async def _boom(month=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(subsidy, "month_total", _boom)
    assert asyncio.run(subsidy.free_tier_open()) is True


def test_recording_never_blocks_a_send(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("supabase down")
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _boom)
    amount = asyncio.run(subsidy.record("call_business", 0.30, 0.20))
    assert amount == 0.10, "the subsidy is still computed even if it cannot be stored"


def test_zero_subsidy_is_not_written(monkeypatch):
    written = []

    async def _capture(table, row):
        written.append(row)
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _capture)
    asyncio.run(subsidy.record("find_business", 0.0, 0.0))
    assert not written, "a free call with no cost is not a subsidy event"


# ---------------------------------------------------------------------------
# The specific decision: voice is subsidised, not free
# ---------------------------------------------------------------------------

def test_call_business_is_priced_below_cost_but_not_free():
    """Free voice is the fastest route to the loss the founder ruled out."""
    from billing.pricing import price_cents, floor_credits, is_paid
    price = price_cents("call_business")
    floor = floor_credits(0.30)
    assert price > 0, "a free voice call is an unbounded loss per caller"
    assert price < floor, "this is meant to be a deliberate subsidy"
    assert is_paid("call_business"), "it must actually reach the billing gate"


def test_the_voice_subsidy_is_a_known_number():
    """We should be able to say exactly what generosity costs per call."""
    from billing.pricing import price_cents
    per_call = subsidy.compute(0.30, price_cents("call_business") / 100)
    assert 0 < per_call <= 0.20, f"unexpected voice subsidy ${per_call}"
