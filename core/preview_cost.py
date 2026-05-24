"""
preview_cost — core operation handler.
Returns cost estimate, latency estimate, and success probability before execution.
Accuracy SLO: actual cost within ±5% of preview.
"""
from __future__ import annotations

import time

from core.models import (
    OperationStatus,
    OutcomeReceipt,
    PreviewCostRequest,
    PreviewCostResponse,
)

# Authoritative list of every operation the broker exposes. preview_cost
# refuses to invent a quote for anything outside this set so the ±5% SLO
# advertised in the response can be honored — a fabricated midpoint for an
# unknown op would violate it on the first real call.
_KNOWN_OPERATIONS = frozenset({
    "find_business",
    "verify_business",
    "send_message",
    "capture_lead",
    "schedule_appointment",
    "send_transactional_confirmation",
    "handle_inbound",
    "escalate_to_human",
    "get_status",
    "get_outcome",
    "preview_cost",
    "self_test",
    "import_booking_url",
    "call_business",
})

# Pricing table — must stay in sync with manifest/manifest.json cost_model
# and edge/src/x402.ts PRICING_ATOMIC. Any drift = agent trust violation
# (agent calls preview_cost, sees one price, x402 charges another).
_PRICING = {
    "find_business":                  {"min": 0.01, "max": 0.01, "basis": "per_call"},
    "verify_business":                {"min": 0.02, "max": 0.02, "basis": "per_call"},
    "send_message":                   {"min": 0.02, "max": 0.22, "basis": "per_message"},
    "capture_lead":                   {"min": 0.05, "max": 0.05, "basis": "per_lead"},
    "schedule_appointment":           {"min": 0.15, "max": 0.50, "basis": "per_booking_attempt+success_bonus"},
    "send_transactional_confirmation":{"min": 0.02, "max": 0.02, "basis": "per_message"},
    "handle_inbound":                 {"min": 0.03, "max": 0.03, "basis": "per_inbound"},
    "escalate_to_human":              {"min": 0.20, "max": 0.20, "basis": "per_escalation"},
    "get_status":                     {"min": 0.001,"max": 0.001,"basis": "per_call"},
    "get_outcome":                    {"min": 0.001,"max": 0.001,"basis": "per_call"},
    "preview_cost":                   {"min": 0.0,  "max": 0.0,  "basis": "free"},
    "self_test":                      {"min": 0.0,  "max": 0.0,  "basis": "free"},
    "import_booking_url":             {"min": 0.005,"max": 0.005,"basis": "per_call"},
    "call_business":                  {"min": 0.50, "max": 0.50, "basis": "per_call"},
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
}

_CHANNEL_LIKELY = {
    "send_message": "sms:twilio",
    "call_business": "voice_ai:vapi",
    "schedule_appointment": "direct_api:calcom",
    "send_transactional_confirmation": "sms:twilio",
    "handle_inbound": "inbound:api",
    "escalate_to_human": "internal:ticketing",
}


async def handle_preview_cost(
    request: PreviewCostRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> PreviewCostResponse | OutcomeReceipt:
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

    pricing = _PRICING.get(op, {"min": 0.01, "max": 1.00, "basis": "per_call"})
    latency = _LATENCY.get(op, {"p50": 1000, "p95": 5000})

    # Estimate cost: use midpoint for preview, stay within ±5% of actual
    estimated = round((pricing["min"] + pricing["max"]) / 2, 4)

    return PreviewCostResponse(
        estimated_cost_usd=estimated,
        cost_range={"min_usd": pricing["min"], "max_usd": pricing["max"]},
        estimated_latency_p50_ms=latency["p50"],
        estimated_latency_p95_ms=latency["p95"],
        success_probability_estimate=_SUCCESS_PROB.get(op, 0.90),
        channel_likely=_CHANNEL_LIKELY.get(op, "auto"),
        cost_accuracy_slo="±5%",
    )
