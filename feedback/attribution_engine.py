"""
Attribution engine — given a classified failure, attribute it to the responsible layer.

Layers:
  agent_logic     — the calling agent sent wrong params or chose wrong operation
  manifest        — manifest was unclear / missing guidance; caused selection_miss
  supply          — SMB data is stale, missing capabilities, or unreachable
  compliance      — compliance pre-check blocked the operation
  channel         — channel adapter failed (non-environmental)
  infrastructure  — timeouts, rate limits, storage failures
  unknown         — insufficient signal
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.models import FailureClass
from feedback.failure_classifier import ClassifiedFailure


class AttributionLayer(str, Enum):
    AGENT_LOGIC = "agent_logic"
    MANIFEST = "manifest"
    SUPPLY = "supply"
    COMPLIANCE = "compliance"
    CHANNEL = "channel"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


@dataclass
class AttributionResult:
    layer: AttributionLayer
    confidence: float           # 0.0 – 1.0
    failure_class: FailureClass
    operation: str
    reason_code: str
    actionable_owner: str       # who should act: "agent_developer", "manifest_team", etc.
    recommendation: str


# Attribution rules: FailureClass → (layer, actionable_owner, recommendation)
_ATTRIBUTION_RULES: dict[FailureClass, tuple[AttributionLayer, str, str]] = {
    FailureClass.DISCOVERY_MISS: (
        AttributionLayer.SUPPLY,
        "supply_team",
        "Expand SMB coverage in the target zip/city. Add more businesses to the directory.",
    ),
    FailureClass.SELECTION_MISS: (
        AttributionLayer.MANIFEST,
        "manifest_team",
        "Improve when_to_use / when_not_to_use guidance in the manifest for clearer agent guidance.",
    ),
    FailureClass.PARAM_MISUSE: (
        AttributionLayer.AGENT_LOGIC,
        "agent_developer",
        "Agent sent invalid parameters. Review input_schema for the operation and fix the calling code.",
    ),
    FailureClass.EXECUTION_FAILURE: (
        AttributionLayer.CHANNEL,
        "reliability_team",
        "Investigate channel adapter health. Review circuit breaker state and fallback chain coverage.",
    ),
    FailureClass.OUTCOME_REJECTED: (
        AttributionLayer.SUPPLY,
        "supply_team",
        "SMB rejected booking or outcome. Verify SMB acceptance rate and onboarding quality.",
    ),
    FailureClass.SUPPLY_UNREACHABLE: (
        AttributionLayer.SUPPLY,
        "supply_team",
        "SMB is not reachable. Update contact info or remove from active directory.",
    ),
    FailureClass.COMPLIANCE_VIOLATION: (
        AttributionLayer.COMPLIANCE,
        "compliance_team",
        "Pre-check blocked the operation. Review consent store, opt-out status, and jurisdiction rules.",
    ),
    FailureClass.ENVIRONMENTAL: (
        AttributionLayer.INFRASTRUCTURE,
        "infrastructure_team",
        "Rate limit or timeout. Review upstream SLA, add retry budget, or scale infrastructure.",
    ),
}


def attribute_failure(classified: ClassifiedFailure) -> AttributionResult:
    """
    Attribute a classified failure to its responsible layer.
    """
    rule = _ATTRIBUTION_RULES.get(classified.failure_class)
    if not rule:
        return AttributionResult(
            layer=AttributionLayer.UNKNOWN,
            confidence=0.10,
            failure_class=classified.failure_class,
            operation=classified.operation,
            reason_code=classified.reason_code,
            actionable_owner="engineering",
            recommendation="Insufficient signal — add more context to the failure.",
        )

    layer, owner, recommendation = rule
    # Confidence inherits from classifier, slightly discounted per layer uncertainty
    layer_discount = 0.05 if layer == AttributionLayer.INFRASTRUCTURE else 0.0
    confidence = max(0.1, classified.confidence - layer_discount)

    return AttributionResult(
        layer=layer,
        confidence=confidence,
        failure_class=classified.failure_class,
        operation=classified.operation,
        reason_code=classified.reason_code,
        actionable_owner=owner,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Batch attribution for weekly reports
# ---------------------------------------------------------------------------

@dataclass
class AttributionSummary:
    total_failures: int
    by_layer: dict[str, int]
    by_failure_class: dict[str, int]
    top_operations: list[tuple[str, int]]    # (operation, count) sorted desc
    top_recommendations: list[str]


def summarize_attributions(results: list[AttributionResult]) -> AttributionSummary:
    """Aggregate a list of AttributionResult into a summary for weekly reporting."""
    by_layer: dict[str, int] = {}
    by_fc: dict[str, int] = {}
    by_op: dict[str, int] = {}
    recs: dict[str, int] = {}

    for r in results:
        by_layer[r.layer.value] = by_layer.get(r.layer.value, 0) + 1
        by_fc[r.failure_class.value] = by_fc.get(r.failure_class.value, 0) + 1
        by_op[r.operation] = by_op.get(r.operation, 0) + 1
        recs[r.recommendation] = recs.get(r.recommendation, 0) + 1

    top_ops = sorted(by_op.items(), key=lambda x: x[1], reverse=True)[:5]
    top_recs = sorted(recs.keys(), key=lambda r: recs[r], reverse=True)[:3]

    return AttributionSummary(
        total_failures=len(results),
        by_layer=by_layer,
        by_failure_class=by_fc,
        top_operations=top_ops,
        top_recommendations=top_recs,
    )
