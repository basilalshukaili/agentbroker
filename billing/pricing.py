"""
billing/pricing.py -- canonical price table for AgentBroker.

SINGLE SOURCE OF TRUTH: 1 credit = 1 US cent. Both rails (x402 + credits)
derive their numbers from this module. Never hard-code prices in x402_gate,
preview_cost, or the edge TypeScript -- import from here instead.

Design decisions (locked 2026-08-24, from credits-billing-spec.md):
- Variable-price ops: reserve MAX, settle ACTUAL from receipt.cost.amount.
- import_booking_url = 0cr (adoption wedge -- agents import freely, pay for action).
- call_business = 0cr until voice is provisioned (parity with x402_gate).
- Reads and probes are always free; gating them would break catalog scorers.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Integer-cents table (1 cent = 1 credit). Zero = free.
# Variable-price ops list their MIN here; see _MAX_PRICING_CENTS for the MAX.
# ---------------------------------------------------------------------------

_PRICING_CENTS: dict[str, int] = {
    # FREE -- reads, probes, adoption wedges (no key required, unmetered)
    "find_business":               0,
    "verify_business":             0,
    "get_status":                  0,
    "get_outcome":                 0,
    "preview_cost":                0,
    "self_test":                   0,
    "check_booking_link":          0,
    "check_compliance":            0,
    "import_booking_url":          0,   # adoption wedge -- free so agents import without friction
    "call_business":               0,   # voice not provisioned; 0cr until enabled
    # PREMIUM DATA TOOLS (2cr/call when DATA_METERING_ENABLED=true; free within daily quota)
    # When DATA_METERING_ENABLED=false (default) these run free/unmetered via the bypass in mcp_server.py.
    "verify_company_record":       2,   # live GLEIF LEI + SEC EDGAR lookup
    "screen_sanctions":            2,   # OFAC SDN + OpenSanctions 40+ lists
    "map_trade_restriction":       2,   # OFAC embargo + export-control + sanctioned-party screening
    # PAID writes
    "send_message":                2,   # variable: min 2cr, max 22cr (Twilio SMS)
    "capture_lead":                5,
    "schedule_appointment":        15,  # variable: min 15cr, max 50cr (Cal.com + success bonus)
    "send_transactional_confirmation": 2,
    "handle_inbound":              3,
    "escalate_to_human":           20,
}

# MAX credits for variable-price ops (what we RESERVE before dispatch).
# Fixed-price ops: max == min == price_cents. Only variable ops listed here.
_MAX_PRICING_CENTS: dict[str, int] = {
    "send_message":         22,
    "schedule_appointment": 50,
}

# All known operations (for validation / parity tests)
ALL_OPERATIONS: frozenset[str] = frozenset(_PRICING_CENTS)

# x402 paid-tool set: those with price > 0 (import_booking_url and call_business
# are both 0 so they are naturally excluded)
_PAID_OPS: frozenset[str] = frozenset(op for op, c in _PRICING_CENTS.items() if c > 0)


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def price_cents(op: str) -> int:
    """Return the base/min price in integer cents (0 = free). 1 cent = 1 credit.
    For variable ops this is the MIN; use max_credits() for the reserve amount."""
    return _PRICING_CENTS.get(op, 0)


# Alias used by credits rail (billing/credits.py)
credit_cents = price_cents


def max_credits(op: str) -> int:
    """Return the MAX credits to reserve for variable-price ops.
    Equal to price_cents(op) for fixed-price ops."""
    return _MAX_PRICING_CENTS.get(op, price_cents(op))


def price_usd_str(op: str) -> str:
    """Return the price as a USD decimal string, e.g. '0.02'. Used by x402 gate.
    Derived from integer cents to avoid float representation drift."""
    c = price_cents(op)
    # Integer division avoids float repr surprises; always two decimal places.
    whole = c // 100
    frac = c % 100
    return f"{whole}.{frac:02d}"


def price_atomic(op: str) -> int:
    """Return the price in USDC atomic units (6 decimals; 1 cent = 10000 atomic).
    Matches the edge x402.ts PRICING_ATOMIC values for every paid op.
    Zero = free."""
    return price_cents(op) * 10_000


def is_paid(op: str) -> bool:
    """True if this operation costs credits (price > 0)."""
    return op in _PAID_OPS


# Alias for x402 gate usage
is_x402_paid = is_paid
