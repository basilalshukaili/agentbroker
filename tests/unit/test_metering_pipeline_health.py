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

All tests mock storage.supabase_client.rpc (or insert_row, where noted); no
real Supabase or network calls.

UPDATE 2026-09-28 (tm_requirements row 1181, second cause): the fix above
made the SECOND, real cause of the original silence findable — usage_events
and billing_events both have RLS enabled with zero policies, and this
container runs with only SUPABASE_ANON_KEY (anon holds an INSERT grant but
does not bypass RLS, so every insert was attempted and rejected). Both
billing/usage_logger.py and billing/durable_meter.py now call a SECURITY
DEFINER RPC function (usage_events_insert / billing_events_insert, see
sql/agentbroker/003_usage_billing_security_definer_rpc.sql) instead of a
direct table insert, so the tests below that exercise the write itself now
mock storage.supabase_client.rpc rather than insert_row.
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
    ul._window.reset()


def _reset_durable_meter_stats():
    import billing.durable_meter as dm
    dm._stats.update({
        "scheduled": 0, "succeeded": 0, "failed": 0,
        "last_success_ts": None, "last_failure_ts": None, "last_failure_reason": None,
    })
    dm._pending_tasks.clear()
    dm._window.reset()


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

        async def slow_rpc(fn, payload):
            await release.wait()
            return {"id": "fake"}

        with patch("storage.supabase_client.rpc", slow_rpc):
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
    async def test_rpc_returning_none_is_counted_as_a_failure(self):
        """rpc() returning None (defensive branch -- see log_usage_event's
        own docstring: usage_events_insert's `returning * into v_row` makes
        this practically impossible, but a None here must never be read as
        success) must increment the failed counter and record why."""
        import billing.usage_logger as ul

        async def null_rpc(fn, payload):
            return None

        with patch("storage.supabase_client.rpc", null_rpc):
            ul.fire_log_usage("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
            await _drain_pending(ul._pending_tasks)

        health = ul.get_usage_logger_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "rpc_returned_none"

    @pytest.mark.asyncio
    async def test_rpc_raising_is_counted_as_a_failure(self):
        """The REAL failure shape in production (tm_requirements row 1181):
        storage/supabase_client.py's rpc() RAISES (never returns None) when
        PostgREST rejects the call -- e.g. RLS denying an anon caller with no
        matching policy. This must be counted exactly like any other
        exception, not silently swallowed."""
        import billing.usage_logger as ul

        async def denied_rpc(fn, payload):
            raise RuntimeError(f"rpc({fn!r}) failed: HTTP 401 body=permission denied")

        with patch("storage.supabase_client.rpc", denied_rpc):
            ul.fire_log_usage("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
            await _drain_pending(ul._pending_tasks)

        health = ul.get_usage_logger_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "exception:RuntimeError"

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


class TestUsageLoggerUsesTheSecurityDefinerRpc:
    """tm_requirements row 1181: usage_events has RLS enabled with zero
    policies, and this container runs with only SUPABASE_ANON_KEY, which
    holds a table-level INSERT grant but does not bypass RLS -- a direct
    insert_row("usage_events", ...) is attempted and rejected every time.
    The fix is routing through the SECURITY DEFINER RPC function
    `usage_events_insert` (sql/agentbroker/003_usage_billing_security_
    definer_rpc.sql), which `anon` may EXECUTE. This must never regress back
    to a direct table write -- proven here by making insert_row itself raise
    if it is ever called, not merely by observing that rpc() was called."""

    def setup_method(self):
        _reset_usage_logger_stats()

    @pytest.mark.asyncio
    async def test_log_usage_event_never_calls_insert_row(self):
        import billing.usage_logger as ul

        async def forbidden_insert_row(table, row):
            raise AssertionError(
                f"log_usage_event must never call insert_row directly (table={table!r}) "
                "-- anon has no table grant on usage_events (row 1181); it must go "
                "through the usage_events_insert RPC instead."
            )

        async def fake_rpc(fn, payload):
            assert fn == "usage_events_insert"
            assert payload["p_session_kind"] in (
                "crawler", "anon_agent", "verified_agent_key", "verified_human_key",
            )
            return {"id": 1}

        with patch("storage.supabase_client.insert_row", forbidden_insert_row), \
             patch("storage.supabase_client.rpc", fake_rpc):
            await ul.log_usage_event("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)

        health = ul.get_usage_logger_health()
        assert health["succeeded"] == 1
        assert health["failed"] == 0


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

        async def slow_rpc(fn, payload):
            await release.wait()
            return {"id": "fake"}

        with patch("storage.supabase_client.rpc", slow_rpc):
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
    async def test_rpc_returning_none_is_counted(self):
        import billing.durable_meter as dm

        async def null_rpc(fn, payload):
            return None

        with patch("storage.supabase_client.rpc", null_rpc):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-2", amount_usd=0.0, basis="free")
            await _drain_pending(dm._pending_tasks)

        health = dm.get_durable_meter_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "rpc_returned_none"

    @pytest.mark.asyncio
    async def test_rpc_raising_is_counted(self):
        """The REAL failure shape in production (tm_requirements row 1181):
        rpc() RAISES when PostgREST rejects the call. Must be counted, not
        swallowed."""
        import billing.durable_meter as dm

        async def denied_rpc(fn, payload):
            raise RuntimeError(f"rpc({fn!r}) failed: HTTP 401 body=permission denied")

        with patch("storage.supabase_client.rpc", denied_rpc):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-3", amount_usd=0.0, basis="free")
            await _drain_pending(dm._pending_tasks)

        health = dm.get_durable_meter_health()
        assert health["failed"] == 1
        assert health["succeeded"] == 0
        assert health["last_failure_reason"] == "exception:RuntimeError"


class TestDurableMeterUsesTheSecurityDefinerRpc:
    """Same row-1181 fix, billing rail: DurableMeter._persist() must go
    through the `billing_events_insert` RPC, never a direct
    insert_row("billing_events", ...) -- anon has no table grant on
    billing_events either. Proven by making insert_row itself raise if it is
    ever called."""

    def setup_method(self):
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_persist_never_calls_insert_row(self):
        import billing.durable_meter as dm

        async def forbidden_insert_row(table, row):
            raise AssertionError(
                f"DurableMeter._persist must never call insert_row directly "
                f"(table={table!r}) -- anon has no table grant on billing_events "
                "(row 1181); it must go through the billing_events_insert RPC instead."
            )

        async def fake_rpc(fn, payload):
            assert fn == "billing_events_insert"
            assert 0 <= payload["p_amount_usd"] <= 5.00
            return {"id": 1}

        with patch("storage.supabase_client.insert_row", forbidden_insert_row), \
             patch("storage.supabase_client.rpc", fake_rpc):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-4", amount_usd=0.0, basis="free")
            await _drain_pending(dm._pending_tasks)

        health = dm.get_durable_meter_health()
        assert health["succeeded"] == 1
        assert health["failed"] == 0


# ---------------------------------------------------------------------------
# self_test.py wiring -- the observability half of the fix
# ---------------------------------------------------------------------------

class TestMeteringPipelineSelfTestCheck:
    """The self_test check itself must exist, must actually run as part of
    self_test (not merely be defined and orphaned -- this codebase has hit
    that exact "wired but unreachable" shape before, see mcp_server.py's
    profile-door history), and must go unhealthy exactly when a rail is
    FAILING -- not merely when there has been no traffic yet, and not merely
    because one write failed at some point since the process started.

    That last clause is the 2026-10-03 fix (tm_requirements row 358). The
    check used to fail for the rest of the process's life on the first failed
    write: two stalls of the database's shared connection pool (2026-10-02 12:12 and
    2026-10-03 07:16 UTC) lost 13 of the last 24 hours' 15,283 usage_events writes, and the
    public self_test said `healthy: false` from the first one until a restart reset the
    counter, about 30 hours later."""

    def setup_method(self):
        _reset_usage_logger_stats()
        _reset_durable_meter_stats()

    def teardown_method(self):
        _reset_usage_logger_stats()
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_healthy_when_no_failures_recorded(self):
        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is True

    @pytest.mark.asyncio
    async def test_one_failed_write_among_many_successes_does_not_flip_the_check(self):
        """THE REGRESSION. The old rule (`failed > 0`) fails this."""
        import billing.usage_logger as ul
        import billing.durable_meter as dm
        for _ in range(200):
            ul._record_success()
            dm._record_success()
        ul._record_failure("transport:ReadTimeout")
        dm._record_failure("http_504:PGRST003")

        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is True, check.error
        # the cumulative counters still remember it: nothing is hidden, the verdict just changed
        assert ul.get_usage_logger_health()["failed"] == 1
        assert ul.get_usage_logger_health()["last_failure_reason"] == "transport:ReadTimeout"

    @pytest.mark.asyncio
    async def test_the_real_incident_ratio_is_healthy(self):
        """13 failed writes in a day of 15,283 -- spread through a 10 minute window of normal
        traffic that is a few percent, and must not be called a broken pipeline."""
        import billing.usage_logger as ul
        for _ in range(180):
            ul._record_success()
        for _ in range(13):
            ul._record_failure("transport:ReadTimeout")

        from agent_interface.self_test import _check_metering_pipeline
        assert (await _check_metering_pipeline()).passed is True

    @pytest.mark.asyncio
    async def test_unhealthy_when_usage_logger_is_failing_every_write(self):
        """A dead rail -- the AUDIT-2026-09-28 / row 1181 shape: every write rejected."""
        import billing.usage_logger as ul
        for _ in range(3):
            ul._record_failure("http_401")

        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "usage_events" in check.error
        assert "http_401" in check.error

    @pytest.mark.asyncio
    async def test_unhealthy_when_durable_meter_is_failing_every_write(self):
        import billing.durable_meter as dm
        for _ in range(3):
            dm._record_failure("exception:RuntimeError")

        from agent_interface.self_test import _check_metering_pipeline
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "billing_events" in check.error

    @pytest.mark.asyncio
    async def test_two_failures_are_not_yet_a_dead_rail(self):
        import billing.usage_logger as ul
        ul._record_failure("transport:ReadTimeout")
        ul._record_failure("transport:ReadTimeout")

        from agent_interface.self_test import _check_metering_pipeline
        assert (await _check_metering_pipeline()).passed is True

    @pytest.mark.asyncio
    async def test_a_burst_of_failures_clears_itself_once_it_leaves_the_window(self):
        """No restart needed: the failures age out ten minutes after they happened."""
        import time
        import billing.usage_logger as ul
        from agent_interface.self_test import _check_metering_pipeline

        for _ in range(6):
            ul._window.record(False, now=time.monotonic() - 100)    # recent
        assert (await _check_metering_pipeline()).passed is False

        ul._window.reset()
        for _ in range(6):
            ul._window.record(False, now=time.monotonic() - 700)    # older than the window
        assert (await _check_metering_pipeline()).passed is True

    @pytest.mark.asyncio
    async def test_writes_stuck_pending_are_still_unhealthy(self):
        import billing.usage_logger as ul
        ul._pending_tasks.update(object() for _ in range(51))
        try:
            from agent_interface.self_test import _check_metering_pipeline
            check = await _check_metering_pipeline()
            assert check.passed is False
            assert "stuck pending" in check.error
        finally:
            ul._pending_tasks.clear()

    def test_metering_pipeline_check_is_registered_in_self_test(self):
        """A check that exists but is never added to _CHECKS runs in no
        report self_test ever produces -- proven by asserting membership,
        not merely that the function is importable."""
        from agent_interface.self_test import _CHECKS, _check_metering_pipeline
        assert _check_metering_pipeline in _CHECKS

    @pytest.mark.asyncio
    async def test_run_self_test_reports_unhealthy_when_a_rail_is_failing(self):
        """End-to-end through the same run_self_test() the MCP self_test
        tool calls."""
        import billing.usage_logger as ul
        for _ in range(4):
            ul._record_failure("exception:RuntimeError")

        from agent_interface.self_test import run_self_test
        report = await run_self_test()
        assert report.all_passed is False
        names = {c.name: c for c in report.checks}
        assert "metering_pipeline" in names
        assert names["metering_pipeline"].passed is False

    @pytest.mark.asyncio
    async def test_run_self_test_stays_healthy_after_one_stray_failure(self):
        import billing.usage_logger as ul
        ul._record_failure("transport:ReadTimeout")

        from agent_interface.self_test import run_self_test
        report = await run_self_test()
        names = {c.name: c for c in report.checks}
        assert names["metering_pipeline"].passed is True


# ---------------------------------------------------------------------------
# billing/pipeline_health.py -- the rolling window, with an injected clock
# ---------------------------------------------------------------------------

class TestRollingOutcomes:
    def _fresh(self):
        from billing.pipeline_health import RollingOutcomes
        return RollingOutcomes()

    def test_empty_is_healthy(self):
        assert self._fresh().assess(now=1000.0) == (True, None)

    def test_the_thresholds(self):
        cases = [
            # (failures, successes, healthy)
            (2, 0, True),      # two is not yet a dead rail
            (3, 0, False),     # three of three: dead
            (3, 4, True),      # 3 of 7 = 43%
            (3, 3, False),     # 3 of 6 = 50%
            (13, 400, True),   # the real incident, diluted by normal traffic
            (50, 40, False),   # more than half failing
            (1, 0, True),
        ]
        for failed, ok, want in cases:
            w = self._fresh()
            for _ in range(ok):
                w.record(True, now=1000.0)
            for _ in range(failed):
                w.record(False, now=1000.0)
            assert w.assess(now=1001.0)[0] is want, (failed, ok)

    def test_events_age_out_of_the_window(self):
        w = self._fresh()
        for _ in range(5):
            w.record(False, now=1000.0)
        assert w.assess(now=1001.0)[0] is False
        assert w.assess(now=1000.0 + w.window_s + 1)[0] is True
        assert w.snapshot(now=1000.0 + w.window_s + 1)["attempts"] == 0

    def test_the_reason_says_what_happened(self):
        w = self._fresh()
        for _ in range(4):
            w.record(False, now=1000.0)
        healthy, why = w.assess(now=1001.0)
        assert healthy is False
        assert "4 of the last 4 writes" in why and "10 min" in why

    def test_memory_is_bounded(self):
        from billing.pipeline_health import RollingOutcomes
        w = RollingOutcomes(max_events=50)
        for i in range(500):
            w.record(True, now=1000.0)
        assert w.snapshot(now=1000.0)["attempts"] == 50

    def test_reset_clears_everything(self):
        w = self._fresh()
        for _ in range(5):
            w.record(False, now=1000.0)
        w.reset()
        assert w.snapshot(now=1000.0)["attempts"] == 0


class TestFailureReason:
    def test_plain_exceptions_keep_the_historical_shape(self):
        from billing.pipeline_health import failure_reason
        assert failure_reason(RuntimeError("anything")) == "exception:RuntimeError"
        assert failure_reason(ValueError("x")) == "exception:ValueError"

    def test_rpc_errors_name_the_cause(self):
        import httpx
        from billing.pipeline_health import failure_reason
        from storage.supabase_client import RpcError
        assert failure_reason(RpcError("m", kind="transport", fn="f",
                                       cause=httpx.ReadTimeout(""))) == "transport:ReadTimeout"
        assert failure_reason(RpcError("m", kind="transport", fn="f")) == "transport:unknown"
        assert failure_reason(RpcError("m", kind="http", fn="f", status=504,
                                       pg_code="PGRST003")) == "http_504:PGRST003"
        assert failure_reason(RpcError("m", kind="http", fn="f", status=401)) == "http_401"
        assert failure_reason(RpcError("m", kind="decode", fn="f")) == "decode_error"


class TestUsageLoggerNamesTheCause:
    def setup_method(self):
        _reset_usage_logger_stats()

    def teardown_method(self):
        _reset_usage_logger_stats()

    @pytest.mark.asyncio
    async def test_a_timeout_is_recorded_as_a_timeout_not_as_a_runtime_error(self):
        import httpx
        import billing.usage_logger as ul
        from storage.supabase_client import RpcError

        async def stalled(fn, payload):
            raise RpcError(f"rpc({fn!r}) transport error: ReadTimeout", kind="transport",
                           fn=fn, cause=httpx.ReadTimeout(""))

        with patch("storage.supabase_client.rpc", stalled):
            await ul.log_usage_event("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)

        assert ul.get_usage_logger_health()["last_failure_reason"] == "transport:ReadTimeout"

    @pytest.mark.asyncio
    async def test_the_outcome_path_records_the_postgrest_pool_timeout_by_name(self):
        import billing.usage_logger as ul
        from storage.supabase_client import RpcError

        async def pool_timeout(fn, payload):
            raise RpcError(f"rpc({fn!r}) failed: HTTP 504 body=PGRST003", kind="http", fn=fn,
                           status=504, pg_code="PGRST003")

        with patch("storage.supabase_client.rpc", pool_timeout):
            await ul.log_usage_outcome(ul.UsageEvent(method="initialize"))

        assert ul.get_usage_logger_health()["last_failure_reason"] == "http_504:PGRST003"

    @pytest.mark.asyncio
    async def test_one_failed_write_leaves_exactly_one_usage_log_failed_line(self, caplog):
        """A count of `usage_log_failed` lines must be a count of failed writes. It used to be
        double: 26 lines in the 2026-10-03 log were 13 writes."""
        import logging
        import billing.usage_logger as ul
        from storage.supabase_client import RpcError

        async def denied(fn, payload):
            raise RpcError(f"rpc({fn!r}) failed: HTTP 401 body=x", kind="http", fn=fn, status=401)

        caplog.set_level(logging.ERROR, logger="smb_broker.usage_logger")
        with patch("storage.supabase_client.rpc", denied):
            await ul.log_usage_event("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)

        messages = [r.getMessage() for r in caplog.records]
        assert sum("usage_log_failed" in m for m in messages) == 1, messages
        assert sum("usage_log_exception" in m for m in messages) == 1, messages
        assert not any(r.exc_info for r in caplog.records), "a named cause needs no traceback"

    @pytest.mark.asyncio
    async def test_an_unnamed_exception_still_gets_its_traceback(self, caplog):
        import logging
        import billing.usage_logger as ul

        async def odd(fn, payload):
            raise ZeroDivisionError("boom")

        caplog.set_level(logging.ERROR, logger="smb_broker.usage_logger")
        with patch("storage.supabase_client.rpc", odd):
            await ul.log_usage_event("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)

        assert ul.get_usage_logger_health()["last_failure_reason"] == "exception:ZeroDivisionError"
        assert any(r.exc_info for r in caplog.records)

    @pytest.mark.asyncio
    async def test_success_and_failure_both_feed_the_window(self):
        import billing.usage_logger as ul

        async def ok(fn, payload):
            return {"id": 1}

        with patch("storage.supabase_client.rpc", ok):
            await ul.log_usage_event("tools/call", "self_test", {}, "1.2.3.4", "UA/1", None)
        health = ul.get_usage_logger_health()
        assert health["recent_attempts"] == 1 and health["recent_failed"] == 0
        assert health["healthy"] is True and health["unhealthy_reason"] is None


class TestDurableMeterNamesTheCause:
    def setup_method(self):
        _reset_durable_meter_stats()

    def teardown_method(self):
        _reset_durable_meter_stats()

    @pytest.mark.asyncio
    async def test_an_rpc_error_is_recorded_by_cause_and_feeds_the_window(self):
        import httpx
        import billing.durable_meter as dm
        from storage.supabase_client import RpcError

        async def stalled(fn, payload):
            raise RpcError(f"rpc({fn!r}) transport error: ReadTimeout", kind="transport",
                           fn=fn, cause=httpx.ReadTimeout(""))

        with patch("storage.supabase_client.rpc", stalled):
            meter = dm.DurableMeter()
            meter.record(agent_id="anonymous", operation="check_compliance",
                        operation_id="diag-op-9", amount_usd=0.0, basis="free")
            await _drain_pending(dm._pending_tasks)

        health = dm.get_durable_meter_health()
        assert health["last_failure_reason"] == "transport:ReadTimeout"
        assert health["recent_failed"] == 1
        assert health["healthy"] is True   # one failure is not a dead rail

