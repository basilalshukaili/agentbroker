"""
Budget guard -- NOT WIRED. Nothing in production imports this module.

WARNING: read this before believing a budget cap is enforced.

`agent_interface/identity.py` mints `budget_cap_usd` into every token scope,
and `ErrorCode.BUDGET_EXCEEDED` exists in core/models.py -- so the machinery
LOOKS present from three different angles. It is not connected: no dispatch
path calls check_budget(), and budget_exceeded_error() is never raised.
docs/AGENT_INTEGRATION_GUIDE.md claimed we "track rolling 30-day spend per
agent and refuse operations that would exceed it". That was false; the claim
has been removed (audit 2026-08-26).

WHAT ACTUALLY CAPS SPEND, and why this being unwired is not an overspend hole:
  - Free keys are minted with budget_cap_usd=0.0 and are gated by the daily
    op counter in agent_interface/key_request_logic.py, which IS enforced.
  - Paid usage is PREPAID CREDITS. billing/credits.py deducts from a balance,
    so the balance is the ceiling -- you cannot spend credits you have not
    bought.
  - x402 settles per call on-chain; there is nothing to overspend.

So the real ceiling is the credit balance, not this module. Wiring a second
cap on top would add a failure mode (a wrongly-refused legitimate call) for
very little gain. If it is ever wired, it must read prices from
billing/pricing.py so there stays exactly one price table -- and the docs must
change in the SAME commit.

Also note: this module defines a `check_budget` with the same name as
core/demand_shaping.check_budget, which is live and unrelated (per-business
demand shaping, not per-agent spend). Name-based code audits will conflate
them; they are different functions with different jobs.
"""
from __future__ import annotations

from core.models import APIError, ErrorCode, ErrorCategory


def check_budget(
    budget_cap: float | None,
    estimated_cost: float,
    agent_id: str,
    operation: str,
) -> None:
    """
    Raises APIError(budget_exceeded) if the estimated cost exceeds the cap.
    budget_cap=None means no cap enforced.
    """
    if budget_cap is not None and estimated_cost > budget_cap:
        raise ValueError(
            f"Estimated cost ${estimated_cost:.4f} exceeds Budget-Cap ${budget_cap:.4f} "
            f"for operation {operation}. Use preview_cost to verify before calling."
        )


def budget_exceeded_error(estimated: float, cap: float, operation: str) -> APIError:
    return APIError(
        code=ErrorCode.BUDGET_EXCEEDED,
        category=ErrorCategory.POLICY_ERROR,
        retriable=False,
        message=f"Estimated cost ${estimated:.4f} exceeds Budget-Cap ${cap:.4f} for {operation}.",
        next_action=f"Increase Budget-Cap header to at least ${estimated:.4f} or use preview_cost to check cost first.",
    )
