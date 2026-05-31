"""
Telemetry summary — aggregates dashboard metrics about agent requests and operations.
Tracks: agents requested, businesses found, messages sent, operations completed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict


@dataclass
class DashboardMetrics:
    """Aggregated metrics for dashboard display."""
    total_agents_requested: int = 0
    total_businesses_found: int = 0
    total_messages_sent: int = 0
    total_operations_completed: int = 0
    # --- Buyer funnel (distinguishes real buyers from indexing crawlers) ---
    # A crawler does tools/list + free reads. A BUYER calls a paid tool (gets a
    # 402) and, if real, sends an x402 payment. These counters make that visible
    # so "1202 agents, 0 sales" can be read correctly: are they buyers stuck at a
    # wall, or just bots indexing us?
    total_paid_tool_attempts: int = 0   # a paid tool was invoked (402 issued or paid)
    total_payment_attempts: int = 0     # an x402 payment payload arrived (real buyer)
    total_payments_settled: int = 0     # payment cleared on-chain
    last_updated: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_agents_requested": self.total_agents_requested,
            "total_businesses_found": self.total_businesses_found,
            "total_messages_sent": self.total_messages_sent,
            "total_operations_completed": self.total_operations_completed,
            "total_paid_tool_attempts": self.total_paid_tool_attempts,
            "total_payment_attempts": self.total_payment_attempts,
            "total_payments_settled": self.total_payments_settled,
            "last_updated": self.last_updated.isoformat(),
        }


# Global metrics instance
_metrics = DashboardMetrics()


def record_agent_request() -> None:
    """Record an agent request."""
    _metrics.total_agents_requested += 1
    _metrics.last_updated = datetime.now()


def record_business_found() -> None:
    """Record a business that was found."""
    _metrics.total_businesses_found += 1
    _metrics.last_updated = datetime.now()


def record_message_sent() -> None:
    """Record a message that was sent."""
    _metrics.total_messages_sent += 1
    _metrics.last_updated = datetime.now()


def record_operation_completed() -> None:
    """Record a completed operation."""
    _metrics.total_operations_completed += 1
    _metrics.last_updated = datetime.now()


async def get_telemetry_summary() -> Dict[str, Any]:
    """Get current dashboard metrics summary."""
    return _metrics.to_dict()


def increment_agents_requested(count: int = 1) -> None:
    """Increment agents requested counter."""
    _metrics.total_agents_requested += count
    _metrics.last_updated = datetime.now()


def increment_businesses_found(count: int = 1) -> None:
    """Increment businesses found counter."""
    _metrics.total_businesses_found += count
    _metrics.last_updated = datetime.now()


def increment_messages_sent(count: int = 1) -> None:
    """Increment messages sent counter."""
    _metrics.total_messages_sent += count
    _metrics.last_updated = datetime.now()


def increment_operations_completed(count: int = 1) -> None:
    """Increment operations completed counter."""
    _metrics.total_operations_completed += count
    _metrics.last_updated = datetime.now()


def record_paid_tool_attempt() -> None:
    """A paid tool was invoked (a 402 will be issued unless payment is attached)."""
    _metrics.total_paid_tool_attempts += 1
    _metrics.last_updated = datetime.now()


def record_payment_attempt() -> None:
    """An x402 payment payload arrived — a real buyer is attempting to pay.
    Crawlers/scorers do not construct signed payments, so this is high-signal."""
    _metrics.total_payment_attempts += 1
    _metrics.last_updated = datetime.now()


def record_payment_settled() -> None:
    """A payment cleared on-chain."""
    _metrics.total_payments_settled += 1
    _metrics.last_updated = datetime.now()


def reset_metrics() -> None:
    """Reset all metrics (for testing)."""
    global _metrics
    _metrics = DashboardMetrics()
