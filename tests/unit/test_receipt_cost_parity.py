"""
A receipt's cost must agree with the price table.

Receipts hardcoded their own dollar figures and several disagreed with
billing/pricing.py (found 2026-08-26):

  find_business          receipt $0.01     table 0 credits (FREE)
  get_status/get_outcome receipt $0.001    table 0 credits (FREE)
  schedule_appointment   receipt $1.00     table 15 base / 50 max

The last one had teeth. billing/credits.py derives the ACTUAL charge from
`receipt.cost.amount` and clamps it to the tool's max, so a successful booking
charged the MAXIMUM 50 credits while preview_cost quoted 15 - breaking the
invariant credits.py states in its own docstring, that preview_cost equals the
actual charge. The clamp hid it: it prevented a $1.00 charge and produced a
silently-wrong one instead.

The free-tool cases charged nothing (free tools never enter the metered path)
but the receipt still told an agent it had paid.
"""
from __future__ import annotations

import os
import re

import pytest

from billing.pricing import _PRICING_CENTS, _MAX_PRICING_CENTS, price_cents, receipt_usd

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "core")


def test_receipt_usd_matches_the_table():
    for op, cents in _PRICING_CENTS.items():
        assert receipt_usd(op) == round(cents / 100, 4)


def test_receipt_usd_at_max_matches_the_max_table():
    for op, mx in _MAX_PRICING_CENTS.items():
        assert receipt_usd(op, at_max=True) == round(mx / 100, 4)


def test_free_tools_report_zero():
    """A free tool must never report a cost. It used to."""
    for op, cents in _PRICING_CENTS.items():
        if cents == 0:
            assert receipt_usd(op) == 0.0, f"{op} is free but reports a cost"


def test_no_core_handler_hardcodes_a_dollar_cost():
    """The structural fix: costs are DERIVED, so they cannot drift apart again.

    Scans core/ for a literal dollar amount in a CostRecord. send_message is
    exempt because its cost is genuinely computed per channel at runtime.
    """
    pattern = re.compile(r"CostRecord\(\s*amount\s*=\s*[0-9]")
    offenders = []
    for fname in sorted(os.listdir(CORE)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(CORE, fname)
        src = open(path, encoding="utf-8").read()
        for m in pattern.finditer(src):
            line = src[:m.start()].count("\n") + 1
            snippet = src[m.start():m.start() + 70].replace("\n", " ")
            # A literal 0.0 is fine - it is unambiguous and cannot drift.
            if re.match(r"CostRecord\(\s*amount\s*=\s*0(\.0+)?\s*,", snippet):
                continue
            offenders.append(f"{fname}:{line} {snippet}")
    assert not offenders, (
        "hardcoded receipt costs found - derive from billing.pricing.receipt_usd:\n"
        + "\n".join(offenders))


def test_the_booking_quote_now_matches_the_charge():
    """The bug with teeth: quoted 15, charged 50.

    Reproduces credits.py's derivation - cents from receipt.cost.amount, then
    clamped to the tool's max - and asserts it lands on a value the table
    actually contains rather than the max by accident.
    """
    op = "schedule_appointment"
    base, mx = price_cents(op), _MAX_PRICING_CENTS[op]

    # what a CONFIRMED booking now reports
    confirmed = receipt_usd(op, at_max=True)
    charged = max(0, min(round(confirmed * 100), mx))
    assert charged == mx, "a confirmed booking should settle at the reserved max"

    # what an ATTEMPT now reports
    attempt = receipt_usd(op)
    charged_attempt = max(0, min(round(attempt * 100), mx))
    assert charged_attempt == base, "an attempt should settle at the base price"

    # the old hardcoded $1.00 would have clamped to the max on BOTH paths,
    # which is precisely how an attempt got charged like a confirmation
    old = max(0, min(round(1.00 * 100), mx))
    assert old == mx and base != mx, "regression guard: $1.00 clamped everything to max"
