"""
preview_cost — core operation handler.
Returns cost estimate, latency estimate, and success probability before execution.
Reports the BASIS of the estimate - 'exact' for fixed prices, a
min/max range otherwise. It does NOT promise a tolerance: nothing
measures preview against the eventual charge, and a midpoint cannot
be within 5% of both ends of an 11x range.
"""
from __future__ import annotations

import time

from core.models import (
    OperationStatus,
    OutcomeReceipt,
    PreviewCostRequest,
    PreviewCostResponse,
)
from billing.pricing import price_cents as _price_cents, max_credits as _max_credits, ALL_OPERATIONS

# Authoritative list of every operation the broker exposes. preview_cost
# refuses to invent a quote for anything outside this set so the +-5% SLO
# advertised in the response can be honored.
_KNOWN_OPERATIONS = ALL_OPERATIONS

# Pricing table -- min/max/basis, sourced from billing/pricing.py (single
# source of truth). Min = base price_cents/100; max = max_credits/100 for
# variable ops. Free ops are now consistently 0.0 (fixes a prior bug where
# find_business=0.01, verify_business=0.02, get_status/get_outcome=0.001,
# import_booking_url=0.005, call_business=0.50 were falsely shown as paid).
_PRICING_BASIS: dict[str, str] = {
    "find_business":               "per_call",
    "verify_business":             "per_call",
    "send_message":                "per_message",
    "capture_lead":                "per_lead",
    "schedule_appointment":        "per_booking_attempt+success_bonus",
    "send_transactional_confirmation": "per_message",
    "handle_inbound":              "per_inbound",
    "escalate_to_human":           "per_escalation",
    "get_status":                  "per_call",
    "get_outcome":                 "per_call",
    "preview_cost":                "free",
    "self_test":                   "free",
    "import_booking_url":          "per_call",
    "call_business":               "per_call",
    "check_booking_link":          "free",
    "check_compliance":            "free",
    "verify_company_record":       "per_call",
    "screen_sanctions":            "per_call",
    "map_trade_restriction":       "per_call",
}

_PRICING: dict[str, dict] = {
    op: {
        "min": _price_cents(op) / 100,
        "max": _max_credits(op) / 100,
        "basis": _PRICING_BASIS.get(op, "per_call"),
    }
    for op in ALL_OPERATIONS
}

_LATENCY = {
    "find_business":                  {"p50": 200,  "p95": 800},
    "verify_business":                {"p50": 500,  "p95": 2000},
    "send_message":                   {"p50": 800,  "p95": 4000},
    "capture_lead":                   {"p50": 600,  "p95": 3000},
    "schedule_appointment":           {"p50": 5000, "p95": 60000},
    "send_transactional_confirmation":{"p50": 500,  "p95": 2000},
    "handle_inbound":                 {"p50": 3000, "p95": 15000},
    "escalate_to_human":              {"p50": 2000, "p95": 10000},
    "get_status":                     {"p50": 50,   "p95": 200},
    "get_outcome":                    {"p50": 50,   "p95": 200},
    "preview_cost":                   {"p50": 100,  "p95": 500},
    "self_test":                      {"p50": 200,  "p95": 1000},
    "import_booking_url":             {"p50": 800,  "p95": 3000},
    "call_business":                  {"p50": 45000,"p95": 180000},
    "check_booking_link":             {"p50": 20,   "p95": 100},
    "check_compliance":               {"p50": 15,   "p95": 80},
    "verify_company_record":          {"p50": 800,  "p95": 4000},
    "screen_sanctions":               {"p50": 2000, "p95": 8000},
    "map_trade_restriction":          {"p50": 3000, "p95": 10000},
}

_SUCCESS_PROB = {
    "find_business": 0.98,
    "verify_business": 0.95,
    "send_message": 0.93,
    "capture_lead": 0.97,
    "schedule_appointment": 0.87,
    "send_transactional_confirmation": 0.99,
    "handle_inbound": 0.96,
    "escalate_to_human": 0.99,
    "get_status": 0.999,
    "get_outcome": 0.999,
    "preview_cost": 0.999,
    "self_test": 0.999,
    "import_booking_url": 0.92,
    "call_business": 0.80,
    "check_booking_link": 0.999,
    "check_compliance": 0.999,
    "verify_company_record": 0.95,
    "screen_sanctions": 0.90,
    "map_trade_restriction": 0.92,
}

_CHANNEL_LIKELY = {
    "send_message": "sms:twilio",
    "call_business": "voice_ai:vapi",
    "schedule_appointment": "direct_api:calcom",
    "send_transactional_confirmation": "sms:twilio",
    "handle_inbound": "inbound:api",
    "escalate_to_human": "internal:ticketing",
}


_PREMIUM_DATA_TOOLS: frozenset = frozenset({
    "verify_company_record", "screen_sanctions", "map_trade_restriction",
})

_ZERO_PRICING = {"min": 0.0, "max": 0.0, "basis": "free_while_metering_off"}


async def handle_preview_cost(
    request: PreviewCostRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> PreviewCostResponse | OutcomeReceipt:
    import os as _os_pc
    op = request.operation
    if op not in _KNOWN_OPERATIONS:
        valid = sorted(_KNOWN_OPERATIONS)
        return OutcomeReceipt(
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=(
                f"Unknown operation '{op}'. Valid operations: {valid}"
            ),
            trace_id=trace_id,
        )

    # Honesty invariant: preview_cost == real charge.
    # When DATA_METERING_ENABLED is off (the default), the 3 premium data tools
    # are unconditionally free (bypass gate). Show $0.00 so the preview matches.
    _data_metering_on = _os_pc.getenv("DATA_METERING_ENABLED", "").lower() in (
        "1", "true", "yes"
    )
    if op in _PREMIUM_DATA_TOOLS and not _data_metering_on:
        pricing = _ZERO_PRICING
    else:
        pricing = _PRICING.get(op, {"min": 0.01, "max": 1.00, "basis": "per_call"})
    latency = _LATENCY.get(op, {"p50": 1000, "p95": 5000})

    # Estimate: midpoint for preview. NOTE the honest caveat - for variable
    # operations the range can be wide (send_message spans 0.02-0.22), so the
    # midpoint is an expectation, not a bound. That is why cost_accuracy_slo
    # below reports the BASIS rather than a percentage nobody measures.
    estimated = round((pricing["min"] + pricing["max"]) / 2, 4)

    # DERIVED, not asserted. "exact" is a claim we can support: min == max means
    # the price is fixed and the preview IS the charge. Anything else is a
    # range, and saying so beats quoting a tolerance we never check.
    is_exact = pricing["min"] == pricing["max"]
    accuracy = "exact" if is_exact else "range: see cost_range (min/max)"

    return PreviewCostResponse(
        estimated_cost_usd=estimated,
        cost_range={"min_usd": pricing["min"], "max_usd": pricing["max"]},
        estimated_latency_p50_ms=latency["p50"],
        estimated_latency_p95_ms=latency["p95"],
        success_probability_estimate=_SUCCESS_PROB.get(op, 0.90),
        channel_likely=_CHANNEL_LIKELY.get(op, "auto"),
        cost_accuracy_slo=accuracy,
    )
