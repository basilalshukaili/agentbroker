"""
Weekly report generator — aggregates all optimization signals into a
single WinRate health report.

Inputs:
  - SelectionAnalytics: selection rate per operation / persona / variant
  - EvaluationReport:   quality scores from outcome_evaluator
  - AttributionSummary: failure layer breakdown from attribution_engine
  - SimulationResult:   latest agent simulation WinRate estimate

Output:
  reports/weekly_winrate_<YYYY-WNN>.json
  reports/weekly_winrate_<YYYY-WNN>.md   (human-readable summary)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from optimizer.selection_analytics import SelectionAnalytics
from feedback.outcome_evaluator import EvaluationReport
from feedback.attribution_engine import AttributionSummary


REPORTS_DIR = Path("reports")


def generate_weekly_report(
    analytics: Optional[SelectionAnalytics] = None,
    evaluation: Optional[EvaluationReport] = None,
    attribution: Optional[AttributionSummary] = None,
    simulated_win_rate: Optional[float] = None,
    week_label: Optional[str] = None,
) -> dict:
    """
    Build and persist the weekly WinRate health report.
    All inputs are optional — the report emits what data it has.
    """
    now = time.time()
    week_label = week_label or time.strftime("%Y-W%W", time.gmtime(now))
    week_ago = now - 7 * 86400

    report: dict = {
        "report_type": "weekly_winrate",
        "week": week_label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }

    # --- Selection analytics section ---
    if analytics:
        selection_section = analytics.generate_weekly_report(
            week_label=week_label,
        )
        report["selection"] = {
            "overall_selection_rate": selection_section["overall_selection_rate"],
            "operation_breakdown": selection_section["operation_breakdown"],
            "persona_breakdown": selection_section["persona_breakdown"],
            "total_events": selection_section["total_events"],
        }
    else:
        report["selection"] = {"note": "No analytics data this period."}

    # --- Outcome quality section ---
    if evaluation:
        report["quality"] = {
            "period": evaluation.period_label,
            "total_evaluated": evaluation.total_evaluated,
            "mean_overall": evaluation.mean_overall,
            "mean_correctness": evaluation.mean_correctness,
            "mean_latency_score": evaluation.mean_latency_score,
            "mean_cost_score": evaluation.mean_cost_score,
            "mean_channel_score": evaluation.mean_channel_score,
            "mean_compliance_score": evaluation.mean_compliance_score,
            "win_rate_estimate_from_quality": evaluation.win_rate_estimate,
            "by_operation": evaluation.by_operation,
        }
    else:
        report["quality"] = {"note": "No evaluation data this period."}

    # --- Failure attribution section ---
    if attribution:
        report["attribution"] = {
            "total_failures": attribution.total_failures,
            "by_layer": attribution.by_layer,
            "by_failure_class": attribution.by_failure_class,
            "top_operations": attribution.top_operations,
            "top_recommendations": attribution.top_recommendations,
        }
    else:
        report["attribution"] = {"note": "No attribution data this period."}

    # --- Simulation section ---
    if simulated_win_rate is not None:
        report["simulation"] = {
            "simulated_win_rate": simulated_win_rate,
            "note": "From latest agent_sim harness run.",
        }

    # --- North-star WinRate ---
    wr_candidates = []
    if evaluation:
        wr_candidates.append(evaluation.win_rate_estimate)
    if analytics:
        wr_candidates.append(selection_section.get("overall_selection_rate", 0.0))
    if simulated_win_rate is not None:
        wr_candidates.append(simulated_win_rate)

    report["north_star_win_rate"] = round(
        sum(wr_candidates) / len(wr_candidates), 4
    ) if wr_candidates else None

    # --- Persist ---
    REPORTS_DIR.mkdir(exist_ok=True)
    json_path = REPORTS_DIR / f"weekly_winrate_{week_label}.json"
    md_path = REPORTS_DIR / f"weekly_winrate_{week_label}.md"

    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(_render_markdown(report))

    return report


def _render_markdown(report: dict) -> str:
    week = report.get("week", "unknown")
    wr = report.get("north_star_win_rate")
    wr_str = f"{wr:.1%}" if wr is not None else "N/A"

    lines = [
        f"# Weekly WinRate Report — {week}",
        f"Generated: {report.get('generated_at', '')}",
        "",
        f"## North-Star WinRate: **{wr_str}**",
        "",
    ]

    # Selection
    sel = report.get("selection", {})
    lines += ["## Selection Rate"]
    if "overall_selection_rate" in sel:
        lines.append(f"- Overall: {sel['overall_selection_rate']:.1%}")
        lines.append(f"- Total events: {sel.get('total_events', 0)}")
        lines.append("")
        lines.append("| Operation | Selection Rate | Events |")
        lines.append("|-----------|---------------|--------|")
        for op, data in sorted(sel.get("operation_breakdown", {}).items()):
            sr = data.get("selection_rate", 0)
            ev = data.get("events", 0)
            lines.append(f"| {op} | {sr:.1%} | {ev} |")
    else:
        lines.append(sel.get("note", "N/A"))
    lines.append("")

    # Quality
    q = report.get("quality", {})
    lines += ["## Outcome Quality"]
    if "mean_overall" in q:
        lines.append(f"- Mean overall score: {q['mean_overall']:.3f}")
        lines.append(f"- Correctness: {q['mean_correctness']:.3f}")
        lines.append(f"- Latency SLO hit: {q['mean_latency_score']:.3f}")
        lines.append(f"- Cost accuracy: {q['mean_cost_score']:.3f}")
        lines.append(f"- WinRate estimate: {q['win_rate_estimate_from_quality']:.3f}")
    else:
        lines.append(q.get("note", "N/A"))
    lines.append("")

    # Attribution
    attr = report.get("attribution", {})
    lines += ["## Failure Attribution"]
    if "by_layer" in attr:
        lines.append(f"- Total failures: {attr['total_failures']}")
        for layer, count in sorted(attr["by_layer"].items(), key=lambda x: -x[1]):
            lines.append(f"  - {layer}: {count}")
        lines.append("")
        lines.append("**Top Recommendations:**")
        for rec in attr.get("top_recommendations", []):
            lines.append(f"- {rec}")
    else:
        lines.append(attr.get("note", "N/A"))
    lines.append("")

    # Simulation
    sim = report.get("simulation", {})
    if "simulated_win_rate" in sim:
        lines += [
            "## Agent Simulation",
            f"- Simulated WinRate: {sim['simulated_win_rate']:.1%}",
            f"- {sim.get('note', '')}",
        ]

    return "\n".join(lines) + "\n"
