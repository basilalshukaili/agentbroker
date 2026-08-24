"""
Parity test: billing/pricing.py is the single source of truth.

For every operation:
  price_cents(op) == round(float(_PRICING_USD[op]) * 100)  [x402 gate, paid ops]
  price_cents(op) * 10_000 == edge_atomic                  [edge x402.ts, paid ops]
  preview_cost min_usd * 100 == price_cents(op)            [no false prices shown to agents]

Regression proof: x402 still charges the SAME numbers it did before the
pricing.py refactor (no behavior change on the live x402 rail).
"""
from __future__ import annotations

import pytest

from billing.pricing import (
    price_cents,
    price_usd_str,
    price_atomic,
    max_credits,
    is_paid,
    ALL_OPERATIONS,
    _PRICING_CENTS,
    _MAX_PRICING_CENTS,
)
from billing.x402_gate import _PRICING_USD
from core.preview_cost import _PRICING as _PREVIEW_PRICING


# ---------------------------------------------------------------------------
# Known values from the locked spec (credits-billing-spec.md 2026-08-24)
# ---------------------------------------------------------------------------
_SPEC_CENTS = {
    "send_message":                2,
    "capture_lead":                5,
    "schedule_appointment":        15,
    "send_transactional_confirmation": 2,
    "handle_inbound":              3,
    "escalate_to_human":           20,
}

_SPEC_MAX_CENTS = {
    "send_message":         22,
    "schedule_appointment": 50,
}

_SPEC_FREE = {
    "find_business", "verify_business", "verify_company_record",
    "screen_sanctions", "map_trade_restriction", "get_status", "get_outcome",
    "preview_cost", "self_test", "check_booking_link", "check_compliance",
    "import_booking_url", "call_business",
}

# Edge x402.ts PRICING_ATOMIC (atomic units; 1 cent = 10000 atomic).
# Source: edge/src/x402.ts -- the values we assert against (regression).
_EDGE_ATOMIC = {
    "send_message": 20000,
    "capture_lead": 50000,
    "schedule_appointment": 150000,
    "send_transactional_confirmation": 20000,
    "handle_inbound": 30000,
    "escalate_to_human": 200000,
    # Edge divergences (known, not a Python regression):
    # "import_booking_url": 5000,  -- edge charges, Python rail is FREE (spec: 0cr)
    # "call_business": 500000,     -- edge charges, Python rail is FREE (spec: 0cr)
    # The Python pricing.py is authoritative for credits; edge divergences are
    # documented here and should be codegen-fixed in a later slice.
}


# ---------------------------------------------------------------------------
# Spec correctness
# ---------------------------------------------------------------------------

class TestSpecValues:
    """pricing.py values match the locked spec."""

    def test_paid_ops_match_spec(self):
        for op, cents in _SPEC_CENTS.items():
            assert price_cents(op) == cents, (
                f"{op}: price_cents={price_cents(op)}, spec={cents}"
            )

    def test_free_ops_are_zero(self):
        for op in _SPEC_FREE:
            assert price_cents(op) == 0, (
                f"{op}: expected 0cr (FREE), got {price_cents(op)}"
            )

    def test_max_credits_variable_ops(self):
        for op, mx in _SPEC_MAX_CENTS.items():
            assert max_credits(op) == mx, (
                f"{op}: max_credits={max_credits(op)}, spec={mx}"
            )

    def test_max_equals_min_for_fixed_ops(self):
        """For non-variable ops, max == min (no range)."""
        variable = set(_MAX_PRICING_CENTS.keys())
        for op in ALL_OPERATIONS:
            if op not in variable:
                assert max_credits(op) == price_cents(op), (
                    f"{op}: expected max==min={price_cents(op)}, got max={max_credits(op)}"
                )

    def test_is_paid_correct(self):
        for op in _SPEC_FREE:
            assert not is_paid(op), f"{op} should be free"
        for op in _SPEC_CENTS:
            assert is_paid(op), f"{op} should be paid"


# ---------------------------------------------------------------------------
# x402 gate parity (REGRESSION: proves x402 charges same amounts as before)
# ---------------------------------------------------------------------------

class TestX402GateParity:
    """x402_gate._PRICING_USD derived from pricing.py matches prior hard-coded values."""

    # These were the hard-coded values before the refactor.
    _PRIOR_PRICING_USD = {
        "send_message": "0.02",
        "capture_lead": "0.05",
        "schedule_appointment": "0.15",
        "send_transactional_confirmation": "0.02",
        "handle_inbound": "0.03",
        "escalate_to_human": "0.20",
    }

    def test_x402_paid_ops_unchanged(self):
        """Every op that was paid before is still paid with the same USD string."""
        for op, usd in self._PRIOR_PRICING_USD.items():
            assert op in _PRICING_USD, f"{op} missing from _PRICING_USD"
            assert _PRICING_USD[op] == usd, (
                f"{op}: _PRICING_USD={_PRICING_USD[op]!r}, expected={usd!r}"
            )

    def test_x402_no_new_paid_ops(self):
        """The refactor must not add any newly-paid ops to the x402 gate."""
        for op in _PRICING_USD:
            assert op in self._PRIOR_PRICING_USD, (
                f"{op} is newly paid in _PRICING_USD -- regression!"
            )

    def test_x402_free_ops_absent(self):
        """import_booking_url and call_business must NOT appear in _PRICING_USD."""
        for free_op in ("import_booking_url", "call_business"):
            assert free_op not in _PRICING_USD, (
                f"{free_op} is in _PRICING_USD but should be free"
            )

    def test_price_cents_matches_x402_usd(self):
        """price_cents(op) == round(float(_PRICING_USD[op]) * 100) for all x402 paid ops."""
        for op, usd in _PRICING_USD.items():
            expected = round(float(usd) * 100)
            actual = price_cents(op)
            assert actual == expected, (
                f"{op}: price_cents={actual}, round(x402 usd*100)={expected}"
            )


# ---------------------------------------------------------------------------
# Edge x402.ts parity
# ---------------------------------------------------------------------------

class TestEdgeAtomicParity:
    """price_atomic(op) matches edge PRICING_ATOMIC for every shared paid op."""

    def test_atomic_matches_edge_for_paid_ops(self):
        for op, atomic in _EDGE_ATOMIC.items():
            actual = price_atomic(op)
            assert actual == atomic, (
                f"{op}: price_atomic={actual}, edge atomic={atomic}"
            )

    def test_atomic_derivation_formula(self):
        """price_atomic == price_cents * 10000 for all ops."""
        for op in ALL_OPERATIONS:
            assert price_atomic(op) == price_cents(op) * 10_000, (
                f"{op}: price_atomic={price_atomic(op)} != price_cents*10000"
            )

    def test_free_ops_atomic_is_zero(self):
        for op in _SPEC_FREE:
            assert price_atomic(op) == 0, f"{op}: atomic should be 0"


# ---------------------------------------------------------------------------
# preview_cost parity
# ---------------------------------------------------------------------------

class TestPreviewCostParity:
    """preview_cost._PRICING min matches price_cents for all ops (no false prices)."""

    def test_preview_min_matches_price_cents(self):
        for op in ALL_OPERATIONS:
            expected = price_cents(op) / 100
            actual = _PREVIEW_PRICING[op]["min"]
            assert abs(actual - expected) < 1e-9, (
                f"{op}: preview min={actual}, price_cents/100={expected}"
            )

    def test_preview_max_matches_max_credits(self):
        for op in ALL_OPERATIONS:
            expected = max_credits(op) / 100
            actual = _PREVIEW_PRICING[op]["max"]
            assert abs(actual - expected) < 1e-9, (
                f"{op}: preview max={actual}, max_credits/100={expected}"
            )

    def test_free_ops_preview_is_zero(self):
        """Regression: find_business, verify_business, get_status, get_outcome,
        import_booking_url, call_business were showing non-zero previews. Now fixed."""
        for op in _SPEC_FREE:
            assert _PREVIEW_PRICING[op]["min"] == 0.0, (
                f"{op}: preview min should be 0.0 (free op), got {_PREVIEW_PRICING[op]['min']}"
            )
            assert _PREVIEW_PRICING[op]["max"] == 0.0, (
                f"{op}: preview max should be 0.0 (free op), got {_PREVIEW_PRICING[op]['max']}"
            )

    def test_price_cents_eq_x402_eq_preview_for_paid_ops(self):
        """Triple parity for all paid ops: price_cents == round(x402*100) == round(preview_min*100)."""
        for op, usd in _PRICING_USD.items():
            pc = price_cents(op)
            from_x402 = round(float(usd) * 100)
            from_preview = round(_PREVIEW_PRICING[op]["min"] * 100)
            assert pc == from_x402 == from_preview, (
                f"{op}: price_cents={pc}, x402={from_x402}, preview={from_preview}"
            )

    def test_all_known_ops_in_preview(self):
        for op in ALL_OPERATIONS:
            assert op in _PREVIEW_PRICING, f"{op} missing from _PREVIEW_PRICING"


# ---------------------------------------------------------------------------
# price_usd_str formatting
# ---------------------------------------------------------------------------

class TestPriceUsdStr:
    def test_format_two_decimal_places(self):
        assert price_usd_str("send_message") == "0.02"
        assert price_usd_str("capture_lead") == "0.05"
        assert price_usd_str("schedule_appointment") == "0.15"
        assert price_usd_str("handle_inbound") == "0.03"
        assert price_usd_str("escalate_to_human") == "0.20"
        assert price_usd_str("find_business") == "0.00"

    def test_no_float_repr_drift(self):
        """Verify exact string equality -- no 0.019999... style drift."""
        for op, usd in _PRICING_USD.items():
            assert price_usd_str(op) == usd, (
                f"{op}: price_usd_str={price_usd_str(op)!r}, expected={usd!r}"
            )
