"""
Budget guard — enforces per-request Budget-Cap headers and per-agent budget limits.
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
