"""
Durable billing meter — wraps UsageMeter and fire-and-forgets every recorded
usage event to the Supabase `billing_events` table.

x402 is disabled (2026-06), so amount_usd will be $0 for most tool calls right
now. The durable path is wired NOW so that when x402 / paid tiers are re-enabled
every call already writes a row — no schema migration, no code change, just flip
X402_ENABLED=true.

Usage pattern:
    from billing.durable_meter import get_durable_meter
    meter = get_durable_meter()
    meter.record(agent_id="...", operation="find_business", ...)

The durable_meter is a drop-in superset of UsageMeter: it delegates to the
in-memory meter first (so existing in-memory aggregations keep working), then
schedules an async Supabase write in the background.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from billing.meter import UsageMeter, UsageRecord, get_meter

logger = logging.getLogger("smb_broker.durable_meter")


class DurableMeter(UsageMeter):
    """
    Extends UsageMeter: every record() call also attempts to persist the event
    to Supabase billing_events (best-effort, fire-and-forget).
    """

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
        # 1. Always record in-memory first (never fails).
        rec = super().record(
            agent_id=agent_id,
            operation=operation,
            operation_id=operation_id,
            amount_usd=amount_usd,
            basis=basis,
            channel_used=channel_used,
            success=success,
        )
        # 2. Fire-and-forget durable write.
        self._schedule_persist(rec)
        return rec

    def _schedule_persist(self, rec: UsageRecord) -> None:
        """Schedule durable write without blocking the caller."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._persist(rec))
            else:
                # Called from sync context (tests, startup) — run in a thread.
                import threading
                def _run() -> None:
                    asyncio.run(self._persist(rec))
                t = threading.Thread(target=_run, daemon=True)
                t.start()
        except Exception as exc:  # noqa: BLE001
            logger.debug("durable_meter_schedule_error err=%s", exc)

    @staticmethod
    async def _persist(rec: UsageRecord) -> None:
        """Write one UsageRecord to Supabase billing_events. Never raises."""
        try:
            from storage.supabase_client import insert_row
            row = {
                "record_id":    rec.record_id,
                "agent_id":     rec.agent_id,
                "tool":         rec.operation,
                "operation_id": rec.operation_id,
                "amount_usd":   float(rec.amount_usd),
                "basis":        rec.basis,
                "channel_used": rec.channel_used,
                "status":       "recorded",
                "success":      rec.success,
                "ts":           rec.recorded_at.isoformat(),
            }
            result = await insert_row("billing_events", row)
            if result:
                logger.debug(
                    "billing_event_persisted record_id=%s op=%s amount=%.6f",
                    rec.record_id, rec.operation, rec.amount_usd,
                )
            # If result is None, insert_row already logged the warning.
        except Exception as exc:  # noqa: BLE001
            logger.warning("billing_event_persist_failed record_id=%s err=%s", rec.record_id, exc)


# Module-level singleton.
_durable_meter = DurableMeter()


def get_durable_meter() -> DurableMeter:
    """Return the module-level DurableMeter singleton."""
    return _durable_meter
