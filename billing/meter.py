"""
Usage meter — records every operation's actual cost for billing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class UsageRecord:
    record_id: str
    agent_id: str
    operation: str
    operation_id: str
    amount_usd: float
    basis: str
    channel_used: Optional[str]
    success: bool
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class UsageMeter:
    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(
        self,
        agent_id: str,
        operation: str,
        operation_id: str,
        amount_usd: float,
        basis: str,
        channel_used: Optional[str] = None,
        success: bool = True,
    ) -> UsageRecord:
        import uuid
        rec = UsageRecord(
            record_id=str(uuid.uuid4()),
            agent_id=agent_id,
            operation=operation,
            operation_id=operation_id,
            amount_usd=amount_usd,
            basis=basis,
            channel_used=channel_used,
            success=success,
        )
        self._records.append(rec)
        return rec

    def total_for_agent(self, agent_id: str) -> float:
        return sum(r.amount_usd for r in self._records if r.agent_id == agent_id)

    def all_records(self) -> list[UsageRecord]:
        return list(self._records)


_meter = UsageMeter()


def get_meter() -> UsageMeter:
    return _meter
