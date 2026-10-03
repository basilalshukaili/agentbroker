"""
test_metering_pipeline_recovery.py -- self_test must tell a write failure that has RECOVERED
from a rail that is still DOWN (tm_requirements row 358, 2026-10-04).

BACKGROUND. billing/pipeline_health.py judges a rail by the share of failed writes in the last
ten minutes (at least 3 failures AND at least half of the attempts). That tells a stall of the
database's shared connection pool (2026-10-02 12:12 and 2026-10-03 07:16 UTC, 15 and 15 seconds)
from a sustained outage when traffic is steady: a 15 second stall at one write every
seven seconds is 2 failures among about 80 successes.

It cannot tell them apart when the stall arrives in a burst after a quiet spell. A crawler wave
of 50 calls that lands inside a 15 second stall, ten quiet minutes after the last call, is 50
failures among 50 attempts: "at least half", so the check stays red. The pool is back a moment
later and every following write succeeds, yet the ratio only falls below one half after 50 more
successes or after ten minutes, whichever comes first. A transient failure was reported as an
outage, which is the complaint this branch exists to fix.

THE RULE ADDED HERE. Whatever the ratio says, a rail whose most recent MIN_FAILURES writes all
succeeded has recovered and is reported healthy: a transient failure is one that something
succeeded after. A rail that is still down, or only intermittently up, has a failure among its
most recent writes and keeps its verdict. The cumulative counters are untouched.
"""
from __future__ import annotations

import pytest

from billing.pipeline_health import MIN_FAILURES, RollingOutcomes


def _feed(window: RollingOutcomes, outcomes: str, start: float = 1000.0, step: float = 1.0) -> float:
    """Record one outcome per character ('S' success, 'F' failure); return the time after the last."""
    t = start
    for ch in outcomes:
        window.record(ch == "S", now=t)
        t += step
    return t


class TestRecoveredVersusDown:
    def test_a_burst_that_has_recovered_is_not_an_outage(self):
        """Fifty writes fail inside a stall that follows ten quiet minutes; the pool comes back and
        the next writes succeed. That is a transient failure: healthy as soon as it is proven."""
        w = RollingOutcomes()
        t = _feed(w, "F" * 50, start=700.0, step=0.3)
        assert w.assess(now=t)[0] is False, "nothing has succeeded yet: this is still an outage"

        t = _feed(w, "S" * MIN_FAILURES, start=t + 1.0)
        healthy, why = w.assess(now=t)
        assert healthy is True
        assert why is None

    def test_it_is_healthy_at_the_moment_the_last_needed_success_lands_not_ten_minutes_later(self):
        w = RollingOutcomes()
        t = _feed(w, "F" * 40, start=0.0, step=0.25)
        for k in range(MIN_FAILURES):
            w.record(True, now=t + k)
            expected = k + 1 >= MIN_FAILURES
            assert w.assess(now=t + k)[0] is expected, f"after {k + 1} success(es)"

    def test_fewer_successes_than_the_failure_threshold_are_not_recovery(self):
        w = RollingOutcomes()
        _feed(w, "F" * 20 + "S" * (MIN_FAILURES - 1))
        assert w.assess(now=1100.0)[0] is False

    def test_a_dead_rail_stays_down_however_many_writes_fail(self):
        w = RollingOutcomes()
        _feed(w, "F" * 500)
        healthy, why = w.assess(now=1600.0)
        assert healthy is False
        assert "500 of the last" in why

    def test_a_rail_that_succeeds_only_now_and_then_is_still_down(self):
        """Mostly failing with an odd success in between: the most recent writes include a failure."""
        w = RollingOutcomes()
        _feed(w, ("FFFF" + "S") * 8 + "FF")
        assert w.assess(now=1100.0)[0] is False

    def test_the_successes_must_come_after_the_failures(self):
        """Successes that came BEFORE a run of failures prove nothing about the rail now."""
        w = RollingOutcomes()
        _feed(w, "S" * 3 + "F" * 6)
        assert w.assess(now=1100.0)[0] is False

    def test_a_recovered_rail_still_reports_what_it_lost(self):
        """Nothing is hidden: the window keeps counting the failures, only the verdict changed."""
        w = RollingOutcomes()
        t = _feed(w, "F" * 9 + "S" * 3)
        snap = w.snapshot(now=t)
        assert snap["failed"] == 9
        assert snap["succeeded"] == 3
        assert w.assess(now=t) == (True, None)

    def test_the_original_incident_ratio_is_still_healthy_without_needing_the_new_rule(self):
        """13 failures among 180 successes, ending on failures: healthy by the ratio alone."""
        w = RollingOutcomes()
        t = _feed(w, "S" * 180 + "F" * 13, step=0.5)
        assert w.assess(now=t) == (True, None)


class TestSelfTestTellsRecoveredFromDown:
    def setup_method(self):
        import billing.durable_meter as dm
        import billing.usage_logger as ul
        ul._window.reset()
        dm._window.reset()

    def teardown_method(self):
        import billing.durable_meter as dm
        import billing.usage_logger as ul
        ul._window.reset()
        dm._window.reset()

    @pytest.mark.asyncio
    async def test_usage_events_recovered_burst_passes(self):
        import billing.usage_logger as ul
        from agent_interface.self_test import _check_metering_pipeline

        for _ in range(50):
            ul._record_failure("http_504:PGRST003")
        down = await _check_metering_pipeline()
        assert down.passed is False
        assert "http_504:PGRST003" in down.error

        for _ in range(MIN_FAILURES):
            ul._record_success()
        up = await _check_metering_pipeline()
        assert up.passed is True, up.error
        # the cumulative counters still remember all fifty
        assert ul.get_usage_logger_health()["failed"] >= 50

    @pytest.mark.asyncio
    async def test_billing_events_recovered_burst_passes(self):
        import billing.durable_meter as dm
        from agent_interface.self_test import _check_metering_pipeline

        for _ in range(12):
            dm._record_failure("transport:ReadTimeout")
        assert (await _check_metering_pipeline()).passed is False
        for _ in range(MIN_FAILURES):
            dm._record_success()
        assert (await _check_metering_pipeline()).passed is True

    @pytest.mark.asyncio
    async def test_a_rail_still_failing_is_reported_down_end_to_end(self):
        import billing.usage_logger as ul
        from agent_interface.self_test import run_self_test

        for _ in range(10):
            ul._record_failure("http_401")
        ul._record_success()                      # one lucky write is not recovery
        report = await run_self_test()
        check = {c.name: c for c in report.checks}["metering_pipeline"]
        assert check.passed is False
        assert report.all_passed is False

    @pytest.mark.asyncio
    async def test_the_two_rails_are_judged_separately(self):
        """A recovered usage_events rail must not mask a billing_events rail that is still down."""
        import billing.durable_meter as dm
        import billing.usage_logger as ul
        from agent_interface.self_test import _check_metering_pipeline

        for _ in range(10):
            ul._record_failure("transport:ReadTimeout")
            dm._record_failure("transport:ReadTimeout")
        for _ in range(MIN_FAILURES):
            ul._record_success()
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "billing_events" in check.error
        assert "usage_events" not in check.error
