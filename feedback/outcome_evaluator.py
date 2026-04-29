"""
Outcome evaluator — scores the quality of an OutcomeReceipt and feeds
back into the WinRate optimization loop.

Quality dimensions:
  - Correctness: did the operation achieve what the agent intended?
  - Latency:     was SLO met?
  - Cost:        was actual cost within 10% of preview?
  - Channel:     was a fallback needed? (fallback = partial quality deduction)
  - Compliance:  no violations encountered
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from core.models import OutcomeReceipt, OperationStatus, ErrorCode


# ---------------------------------------------------------------------------
# SLO targets per operation (seconds)
# ---------------------------------------------------------------------------

_LATENCY_SLO_SECONDS: dict[str, float] = {
    "find_business": 2.0,
    "verify_business": 2.0,
    "send_message": 5.0,
    "capture_lead": 2.0,
    "schedule_appointment": 5.0,
    "send_transactional_confirmation": 5.0,
    "handle_inbound": 5.0,
    "escalate_to_human": 5.0,
    "get_status": 1.0,
    "get_outcome": 1.0,
    "preview_cost": 0.5,
    "self_test": 2.0,
}

# Expected cost per operation (USD) — from manifest cost_model
_EXPECTED_COST_USD: dict[str, float] = {
    "find_business": 0.01,
    "verify_business": 0.01,
    "send_message": 0.05,
    "capture_lead": 0.02,
    "schedule_appointment": 0.15,
    "send_transactional_confirmation": 0.04,
    "handle_inbound": 0.03,
    "escalate_to_human": 0.10,
    "get_status": 0.001,
    "get_outcome": 0.001,
    "preview_cost": 0.0,
    "self_test": 0.0,
}


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------

@dataclass
class OutcomeScore:
    operation: str
    overall: float              # 0.0 – 1.0 — WinRate contribution weight

    correctness: float          # 1.0 = success, 0.5 = pending_async, 0.0 = failure
    latency_score: float        # 1.0 = within SLO, scales down proportionally
    cost_score: float           # 1.0 = within ±10% of expected, 0.0 = >50% over
    channel_score: float        # 1.0 = primary channel, 0.7 = 1st fallback, 0.5 = 2+
    compliance_score: float     # 1.0 = no violations, 0.0 = violation encountered

    latency_ms: Optional[int] = None
    actual_cost_usd: Optional[float] = None
    channel_used: Optional[str] = None
    fallback_depth: int = 0
    notes: list[str] = field(default_factory=list)


def evaluate_outcome(receipt: OutcomeReceipt, operation: str) -> OutcomeScore:
    """
    Score an OutcomeReceipt on all quality dimensions.
    Returns an OutcomeScore with overall in [0.0, 1.0].
    """
    notes: list[str] = []

    # --- Correctness ---
    if receipt.status == OperationStatus.SUCCESS:
        correctness = 1.0
    elif receipt.status == OperationStatus.PENDING_ASYNC:
        correctness = 0.6
        notes.append("Operation returned pending_async — partial credit.")
    else:
        correctness = 0.0
        notes.append(f"Operation failed with reason_code={receipt.reason_code}.")

    # --- Latency ---
    slo_s = _LATENCY_SLO_SECONDS.get(operation, 5.0)
    slo_ms = slo_s * 1000
    latency_ms = receipt.latency_ms
    if latency_ms is None:
        latency_score = 0.8  # unknown — give benefit of the doubt
        notes.append("latency_ms not recorded.")
    elif latency_ms <= slo_ms:
        latency_score = 1.0
    elif latency_ms <= slo_ms * 2:
        latency_score = 0.7
        notes.append(f"Latency {latency_ms}ms exceeded SLO ({slo_ms}ms).")
    elif latency_ms <= slo_ms * 5:
        latency_score = 0.4
        notes.append(f"Latency {latency_ms}ms significantly exceeded SLO.")
    else:
        latency_score = 0.1
        notes.append(f"Latency {latency_ms}ms severely exceeded SLO.")

    # --- Cost ---
    expected = _EXPECTED_COST_USD.get(operation, 0.05)
    actual_cost = receipt.cost.amount if receipt.cost else None
    if actual_cost is None or expected == 0.0:
        cost_score = 1.0  # free or unknown — full credit
    else:
        ratio = actual_cost / expected
        if ratio <= 1.10:
            cost_score = 1.0
        elif ratio <= 1.25:
            cost_score = 0.8
            notes.append(f"Cost ${actual_cost:.4f} over expected ${expected:.4f}.")
        elif ratio <= 1.50:
            cost_score = 0.5
            notes.append(f"Cost ${actual_cost:.4f} significantly over expected.")
        else:
            cost_score = 0.2
            notes.append(f"Cost ${actual_cost:.4f} severely over expected ${expected:.4f}.")

    # --- Channel ---
    fallback_chain = receipt.channel_fallback_chain or []
    fallback_depth = len(fallback_chain) - 1 if fallback_chain else 0
    if fallback_depth <= 0:
        channel_score = 1.0
    elif fallback_depth == 1:
        channel_score = 0.75
        notes.append(f"One channel fallback used: {fallback_chain}.")
    elif fallback_depth == 2:
        channel_score = 0.55
        notes.append(f"Two channel fallbacks used: {fallback_chain}.")
    else:
        channel_score = 0.35
        notes.append(f"Extensive fallback chain: {fallback_chain}.")

    # --- Compliance ---
    if receipt.reason_code == "compliance_violation":
        compliance_score = 0.0
        notes.append("Compliance violation recorded.")
    else:
        compliance_score = 1.0

    # --- Overall (weighted combination) ---
    # Correctness is the dominant signal — if it failed, overall is low regardless
    overall = (
        correctness * 0.50
        + latency_score * 0.15
        + cost_score * 0.15
        + channel_score * 0.10
        + compliance_score * 0.10
    )

    return OutcomeScore(
        operation=operation,
        overall=round(overall, 4),
        correctness=correctness,
        latency_score=round(latency_score, 4),
        cost_score=round(cost_score, 4),
        channel_score=round(channel_score, 4),
        compliance_score=compliance_score,
        latency_ms=latency_ms,
        actual_cost_usd=actual_cost,
        channel_used=receipt.channel_used,
        fallback_depth=fallback_depth,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Aggregate evaluator for weekly reports
# ---------------------------------------------------------------------------

@dataclass
class EvaluationReport:
    period_label: str
    total_evaluated: int
    mean_overall: float
    mean_correctness: float
    mean_latency_score: float
    mean_cost_score: float
    mean_channel_score: float
    mean_compliance_score: float
    win_rate_estimate: float     # correctness * channel_score (selection × success)
    by_operation: dict[str, float]  # operation → mean_overall


def build_evaluation_report(
    scores: list[OutcomeScore],
    period_label: str = "weekly",
) -> EvaluationReport:
    if not scores:
        return EvaluationReport(
            period_label=period_label,
            total_evaluated=0,
            mean_overall=0.0,
            mean_correctness=0.0,
            mean_latency_score=0.0,
            mean_cost_score=0.0,
            mean_channel_score=0.0,
            mean_compliance_score=0.0,
            win_rate_estimate=0.0,
            by_operation={},
        )

    n = len(scores)
    by_op: dict[str, list[float]] = {}
    for s in scores:
        by_op.setdefault(s.operation, []).append(s.overall)

    return EvaluationReport(
        period_label=period_label,
        total_evaluated=n,
        mean_overall=round(sum(s.overall for s in scores) / n, 4),
        mean_correctness=round(sum(s.correctness for s in scores) / n, 4),
        mean_latency_score=round(sum(s.latency_score for s in scores) / n, 4),
        mean_cost_score=round(sum(s.cost_score for s in scores) / n, 4),
        mean_channel_score=round(sum(s.channel_score for s in scores) / n, 4),
        mean_compliance_score=round(sum(s.compliance_score for s in scores) / n, 4),
        win_rate_estimate=round(
            sum(s.correctness * s.channel_score for s in scores) / n, 4
        ),
        by_operation={
            op: round(sum(vals) / len(vals), 4)
            for op, vals in by_op.items()
        },
    )
