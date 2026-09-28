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

AUDIT-2026-09-28: shares the exact defect billing/usage_logger.py had —
`_schedule_persist` handed the coroutine to `asyncio.ensure_future(...)` and
kept no reference to the returned Task, which CPython's docs warn may be
garbage collected "at any time, even before it's done" since the loop only
holds it weakly, and its own failure path logged at `logger.debug` (dropped
by the production LOG_LEVEL=INFO). Fixed the same way: `_pending_tasks`
holds a strong reference until the done-callback releases it, and every
outcome is counted in `_stats`, exposed via `get_durable_meter_health()` for
agent_interface/self_test.py's `_check_metering_pipeline`.

AUDIT-2026-09-28 (cont'd, tm_requirements row 1181): the diagnostic half of
the fix above is what surfaced the REAL, second cause the GC bug had been
masking: `billing_events` has RLS enabled with zero policies, and this
container runs with only SUPABASE_ANON_KEY, which holds an INSERT grant but
does not bypass RLS — every insert was attempted and rejected, 0.3 seconds
apart from usage_events' identical failure (one shared cause, not two).
Fixed by routing through the SECURITY DEFINER RPC function
`billing_events_insert` (sql/agentbroker/003_usage_billing_security_definer_
rpc.sql) instead of a direct table insert — see `DurableMeter._persist()`'s
docstring.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from billing.meter import UsageMeter, UsageRecord, get_meter

logger = logging.getLogger("smb_broker.durable_meter")

# ---------------------------------------------------------------------------
# Metering health — mirrors billing/usage_logger.py's _stats/_pending_tasks.
# ---------------------------------------------------------------------------
_pending_tasks: set[asyncio.Task] = set()

_stats: dict = {
    "scheduled": 0,
    "succeeded": 0,
    "failed": 0,
    "last_success_ts": None,
    "last_failure_ts": None,
    "last_failure_reason": None,
}


def get_durable_meter_health() -> dict:
    """Snapshot for a health check — see usage_logger.get_usage_logger_health()."""
    return {**_stats, "pending": len(_pending_tasks)}


def _record_success() -> None:
    _stats["succeeded"] += 1
    _stats["last_success_ts"] = datetime.now(timezone.utc).isoformat()


def _record_failure(reason: str) -> None:
    logger.error("durable_meter_failed reason=%s", reason)
    _stats["failed"] += 1
    _stats["last_failure_ts"] = datetime.now(timezone.utc).isoformat()
    _stats["last_failure_reason"] = reason


def _on_persist_task_done(task: "asyncio.Task") -> None:
    _pending_tasks.discard(task)
    if task.cancelled():
        _record_failure("task_cancelled")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("durable_meter_task_failed err=%s", exc, exc_info=exc)
        _record_failure(f"unhandled_exception:{type(exc).__name__}")


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
        """Schedule durable write without blocking the caller.

        Holds a strong reference to the scheduled task in `_pending_tasks`
        until it finishes (see module docstring, AUDIT-2026-09-28) — a task
        handed to `asyncio.ensure_future` and then dropped is only weakly
        referenced by the event loop and may be collected before it runs.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task = loop.create_task(self._persist(rec))
                _stats["scheduled"] += 1
                _pending_tasks.add(task)
                task.add_done_callback(_on_persist_task_done)
            else:
                # Called from sync context (tests, startup) — run in a thread.
                import threading
                _stats["scheduled"] += 1

                def _run() -> None:
                    try:
                        asyncio.run(self._persist(rec))
                    except Exception as exc:  # noqa: BLE001
                        # asyncio.run() itself failing (as opposed to
                        # _persist's own internal try/except, which never
                        # raises) would otherwise only surface as an
                        # uncaught exception traceback in a daemon thread —
                        # easy to miss, and not reflected in _stats at all.
                        logger.error("durable_meter_thread_run_failed err=%s", exc,
                                     exc_info=exc)
                        _record_failure(f"thread_exception:{type(exc).__name__}")
                t = threading.Thread(target=_run, daemon=True)
                t.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("durable_meter_schedule_error err=%s", exc, exc_info=exc)
            _record_failure(f"schedule_exception:{type(exc).__name__}")

    @staticmethod
    async def _persist(rec: UsageRecord) -> None:
        """Write one UsageRecord to Supabase billing_events. Never raises.

        RLS-CREDENTIAL FIX (tm_requirements row 1181, 2026-09-28): this used
        to call storage/supabase_client.py's insert_row() directly against
        the `billing_events` table. That table has RLS ENABLED with ZERO
        POLICIES (confirmed live), and this container runs with ONLY
        SUPABASE_ANON_KEY -- `anon` holds a table-level INSERT grant but does
        not bypass RLS, so every insert was attempted and rejected, 0.3
        seconds apart from usage_events' identical failure -- one shared
        cause (the RLS lockdown), not two coincidental bugs. The fix mirrors
        usage_logger.py's: a narrow, parameter-scoped, SECURITY DEFINER RPC
        (`billing_events_insert`, sql/agentbroker/003_usage_billing_
        security_definer_rpc.sql) that anon may EXECUTE but that owns its
        own INSERT internally as a BYPASSRLS-owned function -- never a
        permissive anon INSERT policy, which the public anon key would let
        anyone use to forge a billing row. The RPC fixes `status` server-side
        and bounds `amount_usd`, so a forged direct call cannot claim an
        implausible dollar amount; see that file's header for the full
        threat-model note (this table is a best-effort mirror of what
        billing/credits.py's credit_reserve/credit_commit already charged,
        not the ledger itself, so a forged row cannot move real money).
        """
        try:
            from storage.supabase_client import rpc
            result = await rpc("billing_events_insert", {
                "p_record_id":    rec.record_id,
                "p_agent_id":     rec.agent_id,
                "p_tool":         rec.operation,
                "p_operation_id": rec.operation_id,
                "p_amount_usd":   float(rec.amount_usd),
                "p_basis":        rec.basis,
                "p_channel_used": rec.channel_used,
                "p_success":      rec.success,
            })
            if result:
                logger.debug(
                    "billing_event_persisted record_id=%s op=%s amount=%.6f",
                    rec.record_id, rec.operation, rec.amount_usd,
                )
                _record_success()
            else:
                # rpc() itself raises on any failure (see storage/
                # supabase_client.py) -- see usage_logger.py's identical
                # guard comment for why this branch is still handled rather
                # than assumed unreachable.
                _record_failure("rpc_returned_none")
        except Exception as exc:  # noqa: BLE001
            logger.error("billing_event_persist_failed record_id=%s err=%s", rec.record_id, exc,
                        exc_info=exc)
            _record_failure(f"exception:{type(exc).__name__}")


# Module-level singleton.
_durable_meter = DurableMeter()


def get_durable_meter() -> DurableMeter:
    """Return the module-level DurableMeter singleton."""
    return _durable_meter
