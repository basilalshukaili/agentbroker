"""
Metrics emitter — tracks selection_rate, success_rate, WinRate, cost, latency.
In production: emits to Prometheus/OpenTelemetry. For tests: in-memory counters.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricCounters:
    requests_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    successes_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failures_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latency_sum_ms: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    cost_sum_usd: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    compliance_violations: int = 0
    selection_count: int = 0  # times our service was chosen in simulation

    def record(self, operation: str, success: bool, latency_ms: int, cost_usd: float) -> None:
        self.requests_total[operation] += 1
        if success:
            self.successes_total[operation] += 1
        else:
            self.failures_total[operation] += 1
        self.latency_sum_ms[operation] += latency_ms
        self.cost_sum_usd[operation] += cost_usd

    def success_rate(self, operation: str) -> float:
        total = self.requests_total[operation]
        if total == 0:
            return 0.0
        return self.successes_total[operation] / total

    def avg_latency_ms(self, operation: str) -> float:
        total = self.requests_total[operation]
        if total == 0:
            return 0.0
        return self.latency_sum_ms[operation] / total

    def summary(self) -> dict[str, Any]:
        ops = set(self.requests_total.keys())
        return {
            op: {
                "requests": self.requests_total[op],
                "success_rate": round(self.success_rate(op), 4),
                "avg_latency_ms": round(self.avg_latency_ms(op), 1),
                "total_cost_usd": round(self.cost_sum_usd[op], 4),
            }
            for op in ops
        }


_counters = MetricCounters()


def get_metrics() -> MetricCounters:
    return _counters
