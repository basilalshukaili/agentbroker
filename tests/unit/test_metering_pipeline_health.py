"""
test_metering_pipeline_health.py -- prove that a metering write that stops
happening no longer goes unnoticed.

ROOT CAUSE (AUDIT-2026-09-28): billing/usage_logger.py's fire_log_usage() and
billing/durable_meter.py's DurableMeter._schedule_persist() both scheduled
their background Supabase write with asyncio.ensure_future(...) and kept NO
reference to the returned Task anywhere. CPython's own docs warn that a task
nothing else references "may get garbage collected at any time, even before
it's done" (the event loop only holds it weakly) -- and the one failure path
that did exist logged at logger.debug, which the production container's
LOG_LEVEL=INFO drops entirely, so a dead metering rail reported nothing.

Measured LIVE against the production server during this investigation
(2026-09-28): a fresh tools/call (check_compliance -- a free, side-effect-free
tool) returned a clean 200 with a full receipt, yet produced NO usage_events
row and NO billing_events row. A direct insert_row() call against the same
real Supabase table with a realistic tools/call-shaped row succeeded
instantly, ruling out an RLS/schema/constraint cause. Both billing_events and
usage_events last wrote within a third of a second of each other at
2026-09-21 23:21:04 UTC and have been silent for every tools/call since,
across at least one redeploy -- one shared cause across both rails, not two
coincidental ones. The precise trigger under real production concurrency was
not pinned down further (see the investigation report), but the un-referenced
fire-and-forget task is a real, demonstrated defect in both rails regardless,
and is what this file proves fixed.

FIX:
  1. Both fire_log_usage() and DurableMeter._schedule_persist() now hold a
     strong reference to the scheduled Task in a module-level set
     (_pending_tasks), released only via task.add_done_callback() once the
     write has actually finished.
  2. Every outcome -- an exception, insert_row() returning None (a Supabase
     rejection that never raises), or the task being cancelled -- is logged
     at ERROR (not DEBUG) and counted in a module-level _stats dict, exposed
     via get_usage_logger_health() / get_durable_meter_health().
  3. agent_interface/self_test.py's new _check_metering_pipeline reads both
     health snapshots, so a health check -- not a downstream row count days
     later -- catches a dead metering rail.

All tests mock storage.supabase_client.insert_row; no real Supabase or
network calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


def _reset_usage_logger_stats():
    import billing.usage_logger as ul
    ul._stats.update({
        "scheduled": 0, "succeeded": 0, "failed": 0,
        "last_success_ts": None, "last_failure_ts": None, "last_failure_reason": None,
    })
    ul._pending_tasks.clear()


def _reset_durable_meter_stats():
    import billing.durable_meter as dm
    dm._stats.update({
        "scheduled": 0, "succeeded": 0, "failed": 0,
        "last_success_ts": None, "last_failure_ts": None, "last_failure_reason": None,
    })
    dm._pending_tasks.clear()


async def _drain_pending(pending_set, max_turns=20):
    """Give the loop enough turns for scheduled tasks to actually finish."""
    for _ in range(max_turns):
        if not pending_set:
            return
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# usage_logger.py
# ---------------------------------------------------------------------------

class TestUsageLoggerHoldsATaskReference:
    """The GC hazard: a fire-and-forget task nothing references may be
    collected before it runs (CPython docs). fire_log_usage must keep the
    task alive itself rather than rely on the event loop's own weak
    bookkeeping -- proven deterministically by checking the reference is
    held (not by trying to provoke an actual, timing-dependent GC)."""

    def setup_method(self):
        _reset_usage_logger_stats()

    @pytest.mark.asyncio
    async def test_scheduled_task_is_held_in_pending_set_until_done(self):
        import billing.usage_logger as ul

        release = asyncio.Event()

        async def slow_insert(table, row):
            await release.wait()
            return {"id": "fake"}

        with patch("storage.supabase_client.insert_row", slow_insert):
            ul.fire_log_usage("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
            # The write has not completed yet -- it must be held HERE, not
            # merely handed to the loop and forgotten about.
            assert len(ul._pending_tasks) == 1
            assert ul.get_usage_logger_health()["pending"] == 1

            release.set()
            await _drain_pending(ul._pending_tasks)

        assert ul._pending_tasks == set(), "task was never released after completing"
        health = ul.get_usage_logger_health()
        assert health["succeeded"] == 1
        assert health["failed"] == 0


class TestUsageLoggerFailureIsLoud:
    def setup_method(self):
        _reset_usage_logger_stats()

    @pytest.mark.asyncio
    async def test_insert_row_returning_none_is_counted_as_a_failure(self):
        """insert_row() returning None (its documented failure contract --
        see storage/supabase_client.py's own docstring) used to be silently
        indistinguishable from success. It must now increment the failed
        counter and record why."""
        import billing.usage_logger as ul

        async def failing_insert(table, row):
            return None  # exactly what insert_row returns on an HTTP failure

        with patch("storage.supabase_client.insert_row", failing_insert):
            ul.fire_log_usage("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
            await _drain_pending(ul._pending_tasks)

        health = ul.get_usage_logger_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "insert_row_returned_none"

    @pytest.mark.asyncio
    async def test_unhandled_exception_in_the_task_is_counted_and_logged(self):
        """log_usage_event() itself never raises (its own try/except
        guarantees that) -- but if something upstream of it does, the
        done-callback must still catch and count it rather than let it
        vanish as asyncio's own easy-to-miss "exception was never
        retrieved" warning."""
        import billing.usage_logger as ul

        async def boom(*a, **kw):
            raise RuntimeError("simulated escape from log_usage_event's own guard")

        with patch.object(ul, "log_usage_event", boom):
            ul.fire_log_usage("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
            await _drain_pending(ul._pending_tasks)

        health = ul.get_usage_logger_health()
        assert health["failed"] == 1
        assert health["last_failure_reason"].startswith("unhandled_exception:RuntimeError")


# ---------------------------------------------------------------------------
# durable_meter.py -- the separate billing rail, same defect shape
# ---------------------------------------------------------------------------

class TestDurableMeterHoldsATaskReference:
    def setup_method(self):
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_scheduled_persist_task_is_held_until_done(self):
        import billing.durable_meter as dm

        release = asyncio.Event()

        async def slow_insert(table, row):
            await release.wait()
            return {"id": "fake"}

        with patch("storage.supabase_client.insert_row", slow_insert):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-1", amount_usd=0.0, basis="free")
            assert len(dm._pending_tasks) == 1
            assert dm.get_durable_meter_health()["pending"] == 1

            release.set()
            await _drain_pending(dm._pending_tasks)

        assert dm._pending_tasks == set()
        health = dm.get_durable_meter_health()
        assert health["succeeded"] == 1
        assert health["failed"] == 0


class TestDurableMeterFailureIsLoud:
    def setup_method(self):
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_insert_row_returning_none_is_counted(self):
        import billing.durable_meter as dm

        async def failing_insert(table, row):
            return None

        with patch("storage.supabase_client.insert_row", failing_insert):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-2", amount_usd=0.0, basis="free")
            await _drain_pending(dm._pending_tasks)

        health = dm.get_durable_meter_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "insert_row_returned_none"


# ---------------------------------------------------------------------------
# self_test.py wiring -- the observability half of the fix
# ---------------------------------------------------------------------------

class TestMeteringPipelineSelfTestCheck:
    """The self_test check itself must exist, must actually run as part of
    self_test (not merely be defined and orphaned -- this codebase has hit
    that exact "wired but unreachable" shape before, see mcp_server.py's
    profile-door history), and must go unhealthy exactly when a rail has
    recorded a failure, not merely when there has been no traffic yet."""

    def setup_method(self):
        _reset_usage_logger_stats()
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_healthy_when_no_failures_recorded(self):
        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is True

    @pytest.mark.asyncio
    async def test_unhealthy_when_usage_logger_has_a_failure(self):
        import billing.usage_logger as ul
        ul._stats["failed"] = 1
        ul._stats["last_failure_reason"] = "exception:RuntimeError"

        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "usage_events" in check.error

    @pytest.mark.asyncio
    async def test_unhealthy_when_durable_meter_has_a_failure(self):
        import billing.durable_meter as dm
        dm._stats["failed"] = 1
        dm._stats["last_failure_reason"] = "exception:RuntimeError"

        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "billing_events" in check.error

    def test_metering_pipeline_check_is_registered_in_self_test(self):
        """A check that exists but is never added to _CHECKS runs in no
        report self_test ever produces -- proven by asserting membership,
        not merely that the function is importable."""
        from agent_interface.self_test import _CHECKS, _check_metering_pipeline
        assert _check_metering_pipeline in _CHECKS

    @pytest.mark.asyncio
    async def test_run_self_test_reports_unhealthy_when_a_rail_failed(self):
        """End-to-end through the same run_self_test() the MCP self_test
        tool calls."""
        import billing.usage_logger as ul
        ul._stats["failed"] = 1
        ul._stats["last_failure_reason"] = "exception:RuntimeError"

        from agent_interface.self_test import run_self_test
        report = await run_self_test()
        assert report.all_passed is False
        names = {c.name: c for c in report.checks}
        assert "metering_pipeline" in names
        assert names["metering_pipeline"].passed is False
