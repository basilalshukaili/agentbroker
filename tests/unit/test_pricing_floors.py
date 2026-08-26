"""
A credit is sold for less than a cent, so break-even is not 1:1.

billing/pricing.py's header says "1 credit = 1 US cent". That is true when a
credit is SPENT against a vendor bill and FALSE at the point of sale, because
the packages discount by volume:

    Starter $9  -> 1,000cr  = 0.90000 cents/credit
    Growth  $29 -> 3,500cr  = 0.82857 cents/credit
    Scale   $99 -> 13,000cr = 0.76154 cents/credit

So a price set at "cost in cents" under-recovers by ~31% for the customers who
spend the MOST (found 2026-08-26). Every at-cost floor must be computed against
the worst realised rate.

These tests exist so the constant cannot drift away from the packages silently
- change a package and this fails until the constant is updated with it.
"""
from __future__ import annotations

import pytest

from billing.pricing import (
    WORST_CENTS_PER_CREDIT, floor_credits, price_cents, max_credits,
    is_paid, _PRICING_CENTS, _MAX_PRICING_CENTS,
)

# The live package prices, as published on hatchloop.dev/pricing.
PACKAGE_USD = {"starter": 9, "growth": 29, "scale": 99}


def _realised_rates():
    from billing.packages import PACKAGE_CREDITS
    return {name: PACKAGE_USD[name] * 100 / credits
            for name, credits in PACKAGE_CREDITS.items()
            if name in PACKAGE_USD}


def test_constant_matches_the_actual_worst_package_rate():
    """If a package changes, this fails until the constant is updated."""
    rates = _realised_rates()
    assert rates, "no packages found to check against"
    worst = min(rates.values())
    assert abs(WORST_CENTS_PER_CREDIT - worst) < 1e-6, (
        f"WORST_CENTS_PER_CREDIT is {WORST_CENTS_PER_CREDIT} but the worst "
        f"package rate is {worst:.7f} ({rates})")


def test_a_credit_is_worth_less_than_a_cent():
    """The premise. If this ever stops being true, the floors can relax."""
    assert WORST_CENTS_PER_CREDIT < 1.0


def test_floor_exceeds_the_naive_cent_for_cent_price():
    """The whole point: naive pricing under-recovers."""
    for cost in (0.0079, 0.0682, 0.1092, 0.30):
        naive = round(cost * 100)          # "cost in cents" = credits
        real = floor_credits(cost, cushion=1.0)
        assert real >= naive, f"floor {real} should not be below naive {naive}"
    # and at a realistic cushion the gap is material
    assert floor_credits(0.30) > round(0.30 * 100)


def test_floor_is_zero_for_a_free_call():
    assert floor_credits(0) == 0
    assert floor_credits(-1) == 0


def test_floor_never_returns_a_fractional_credit():
    for cost in (0.001, 0.0079, 0.011, 0.0682, 0.1092, 0.30, 1.25):
        f = floor_credits(cost)
        assert isinstance(f, int) and f >= 0


# ---------------------------------------------------------------------------
# The paid-set bug
# ---------------------------------------------------------------------------

def test_paid_set_is_keyed_off_the_maximum_not_the_minimum():
    """is_paid() gates BOTH billing rails. Keying it off the MINIMUM meant a
    tool whose cheapest outcome is free was excluded from billing on EVERY
    outcome - which is how call_business kept charging nothing after Vapi went
    live."""
    src = open(__import__("billing.pricing", fromlist=["x"]).__file__,
               encoding="utf-8").read()
    assert "_PRICING_CENTS.items() if c > 0" not in src, (
        "_PAID_OPS must not be derived from the minimum price")

    # behavioural: a tool free at minimum but costly at maximum must be billable
    hypothetical_min, hypothetical_max = 0, 40
    assert (_MAX_PRICING_CENTS.get("send_message", 0) > 0) is True
    # every tool with a non-zero MAX must be billable
    for op in _PRICING_CENTS:
        if max_credits(op) > 0:
            assert is_paid(op), f"{op} has max {max_credits(op)} but is not billable"


def test_no_tool_is_billable_at_zero_maximum():
    for op in _PRICING_CENTS:
        if max_credits(op) == 0:
            assert not is_paid(op), f"{op} costs nothing but is marked billable"


def test_call_business_is_flagged_until_it_carries_a_price():
    """Vapi is live and voice costs us real money. This test is a deliberate
    reminder, not a pass: when call_business gets its price it should become
    billable, and this assertion should be inverted rather than deleted."""
    if max_credits("call_business") == 0:
        pytest.skip("call_business still priced 0 - pending the pricing decision; "
                    "voice costs ~$0.30/call and floor_credits(0.30) = "
                    f"{floor_credits(0.30)} credits")
    assert is_paid("call_business")
