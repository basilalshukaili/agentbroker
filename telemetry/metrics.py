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
    last_updated: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_agents_requested": self.total_agents_requested,
            "total_businesses_found": self.total_businesses_found,
            "total_messages_sent": self.total_messages_sent,
            "total_operations_completed": self.total_operations_completed,
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


def reset_metrics() -> None:
    """Reset all metrics (for testing)."""
    global _metrics
    _metrics = DashboardMetrics()
