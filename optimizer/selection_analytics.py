"""
Selection analytics — tracks and computes selection rate metrics for WinRate optimization.

Selection rate = P(our service selected | agent reached selection decision point)
Win rate = selection_rate × success_rate_when_selected

Feeds into weekly optimization report.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Selection event
# ---------------------------------------------------------------------------

@dataclass
class SelectionEvent:
    timestamp: float
    agent_id: str
    operation: str
    our_service_selected: bool
    reason: str                     # why selected / why not
    variant_id: str = "control"     # which manifest variant was shown
    persona: Optional[str] = None   # cost_minimizer, quality_maximizer, etc.
    competitor_chosen: Optional[str] = None  # if not us


# ---------------------------------------------------------------------------
# Analytics store
# ---------------------------------------------------------------------------

class SelectionAnalytics:
    """
    In-memory selection analytics store with JSON persistence.
    Periodically flushed to reports/.
    """

    def __init__(self, report_dir: str = "reports") -> None:
        self._events: list[SelectionEvent] = []
        self._report_dir = Path(report_dir)

    def record(self, event: SelectionEvent) -> None:
        self._events.append(event)

    # ---------------------------------------------------------------------------
    # Rate calculations
    # ---------------------------------------------------------------------------

    def selection_rate(
        self,
        *,
        operation: Optional[str] = None,
        variant_id: Optional[str] = None,
        persona: Optional[str] = None,
        since: Optional[float] = None,
    ) -> float:
        events = self._filter(operation=operation, variant_id=variant_id,
                              persona=persona, since=since)
        if not events:
            return 0.0
        selected = sum(1 for e in events if e.our_service_selected)
        return round(selected / len(events), 4)

    def success_rate_placeholder(self) -> float:
        """
        In production this would join selection events with OutcomeReceipt results.
        For now returns a stub — replaced by outcome_evaluator feed in production.
        """
        return 0.0

    def win_rate(
        self,
        success_rate_when_selected: float,
        *,
        operation: Optional[str] = None,
        variant_id: Optional[str] = None,
        persona: Optional[str] = None,
        since: Optional[float] = None,
    ) -> float:
        sr = self.selection_rate(
            operation=operation, variant_id=variant_id,
            persona=persona, since=since,
        )
        return round(sr * success_rate_when_selected, 4)

    # ---------------------------------------------------------------------------
    # By-variant comparison
    # ---------------------------------------------------------------------------

    def variant_comparison(
        self,
        experiment_id: str,
        variant_ids: list[str],
        since: Optional[float] = None,
    ) -> dict[str, dict]:
        """Returns per-variant selection rate and event count."""
        result = {}
        for vid in variant_ids:
            events = self._filter(variant_id=vid, since=since)
            n = len(events)
            selected = sum(1 for e in events if e.our_service_selected)
            result[vid] = {
                "events": n,
                "selected": selected,
                "selection_rate": round(selected / n, 4) if n else 0.0,
            }
        return result

    # ---------------------------------------------------------------------------
    # Persona breakdown
    # ---------------------------------------------------------------------------

    def persona_breakdown(self, since: Optional[float] = None) -> dict[str, dict]:
        personas = {e.persona for e in self._events if e.persona}
        result = {}
        for p in personas:
            events = self._filter(persona=p, since=since)
            n = len(events)
            selected = sum(1 for e in events if e.our_service_selected)
            result[p] = {
                "events": n,
                "selected": selected,
                "selection_rate": round(selected / n, 4) if n else 0.0,
            }
        return result

    # ---------------------------------------------------------------------------
    # Operation breakdown
    # ---------------------------------------------------------------------------

    def operation_breakdown(self, since: Optional[float] = None) -> dict[str, dict]:
        operations = {e.operation for e in self._events}
        result = {}
        for op in sorted(operations):
            events = self._filter(operation=op, since=since)
            n = len(events)
            selected = sum(1 for e in events if e.our_service_selected)
            result[op] = {
                "events": n,
                "selected": selected,
                "selection_rate": round(selected / n, 4) if n else 0.0,
            }
        return result

    # ---------------------------------------------------------------------------
    # Weekly report
    # ---------------------------------------------------------------------------

    def generate_weekly_report(
        self,
        success_rate_by_operation: Optional[dict[str, float]] = None,
        week_label: Optional[str] = None,
    ) -> dict:
        """
        Generate a full weekly analytics report.

        success_rate_by_operation: fed in from outcome_evaluator.
        """
        now = time.time()
        week_ago = now - 7 * 86400
        week_label = week_label or time.strftime("%Y-W%W", time.gmtime(now))

        overall_sr = self.selection_rate(since=week_ago)
        op_breakdown = self.operation_breakdown(since=week_ago)
        persona_bd = self.persona_breakdown(since=week_ago)

        # Compute win_rate per operation if success rates provided
        win_rates: dict[str, float] = {}
        if success_rate_by_operation:
            for op, sr_when_selected in success_rate_by_operation.items():
                op_sr = (
                    op_breakdown.get(op, {}).get("selection_rate", 0.0)
                )
                win_rates[op] = round(op_sr * sr_when_selected, 4)

        report = {
            "week": week_label,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "total_events": len([e for e in self._events if e.timestamp >= week_ago]),
            "overall_selection_rate": overall_sr,
            "win_rates_by_operation": win_rates,
            "operation_breakdown": op_breakdown,
            "persona_breakdown": persona_bd,
        }

        # Persist report
        self._report_dir.mkdir(exist_ok=True)
        report_path = self._report_dir / f"selection_analytics_{week_label}.json"
        report_path.write_text(json.dumps(report, indent=2))

        return report

    # ---------------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------------

    def _filter(
        self,
        *,
        operation: Optional[str] = None,
        variant_id: Optional[str] = None,
        persona: Optional[str] = None,
        since: Optional[float] = None,
    ) -> list[SelectionEvent]:
        events = self._events
        if operation:
            events = [e for e in events if e.operation == operation]
        if variant_id:
            events = [e for e in events if e.variant_id == variant_id]
        if persona:
            events = [e for e in events if e.persona == persona]
        if since:
            events = [e for e in events if e.timestamp >= since]
        return events
