"""
test_metering_pipeline_sustained.py -- a rail that rejects EVERY write must read unhealthy
however little traffic there is (tm_requirements row 358, review of e3ab83c, 2026-10-04).

BACKGROUND. billing/pipeline_health.py judges a rail by the failures that sit inside a rolling
ten-minute window: at least 3, and at least half of the attempts. Its docstring and
agent_interface/self_test.py both said a dead rail (every write rejected) is unhealthy "after
three writes, however little traffic there is". That was not true. A rail written to less often
than about once every 300 seconds never has 3 failures inside ten minutes, so a rail that has
rejected EVERY write for hours read healthy on every check. Reproduced: 40 consecutive failed
writes at gaps of 301 s, 400 s and 660 s were judged healthy on 40 of 40 assessments. The check
this branch replaced (any failure at all) caught that case; the replacement lost it.

THE RULE ADDED HERE. Besides the ratio over the window, a rail is unhealthy when its last
MIN_FAILURES writes ALL failed (nothing has succeeded since the third-to-last write), those
failures are spread over MORE than SUSTAINED_MIN_SPAN_S (two minutes: a stall of the connection
pool lasts seconds, 15 s in both observed cases, so this is not one), and the newest of them is
at most SUSTAINED_FAILURE_MAX_AGE_S (one hour) old. One success ends it at once.

A SECOND TRAP, found while fixing the first. The first version of the rule asked for the
failures to be spread over more than the WINDOW. With writes 250 s apart the three failures span
500 s, so the rule did not apply, and the window itself holds all three for only the first part
of each gap: the check read red at the moment of each write and healthy for most of the time in
between. Every caller reads it at a random moment, so a rule has to be tested BETWEEN writes.

WHAT MUST NOT CHANGE. A short burst of failures that is over, with nothing written since, still
clears ten minutes after it ends (tests/unit/test_metering_pipeline_health.py pins it): failures
packed into one stall are not "sustained", whatever the clock says afterwards.
"""
from __future__ import annotations

import threading
import time

import pytest

from billing.pipeline_health import (
    MIN_FAILURES,
    SUSTAINED_FAILURE_MAX_AGE_S,
    SUSTAINED_MIN_SPAN_S,
    WINDOW_S,
    RollingOutcomes,
)


def _write_at_gaps(w: RollingOutcomes, outcomes: str, gap: float, start: float = 0.0) -> list:
    """Record one outcome per character ('S'/'F') `gap` seconds apart; return the verdict
    (healthy, reason) read at the moment of each write."""
    verdicts = []
    t = start
    for ch in outcomes:
        w.record(ch == "S", now=t)
        verdicts.append(w.assess(now=t))
        t += gap
    return verdicts


class TestADeadRailIsFlaggedHoweverLittleTrafficThereIs:
    @pytest.mark.parametrize("gap", [301.0, 400.0, 660.0, 1800.0, 3000.0])
    def test_every_write_fails_at_any_spacing(self, gap):
        """The review's reproduction: 40 failed writes, `gap` seconds apart."""
        w = RollingOutcomes()
        verdicts = _write_at_gaps(w, "F" * 40, gap)
        healthy = [v[0] for v in verdicts]
        assert healthy[:MIN_FAILURES - 1] == [True] * (MIN_FAILURES - 1), "two failures are not yet a dead rail"
        assert healthy[MIN_FAILURES - 1:] == [False] * (40 - MIN_FAILURES + 1), (
            f"gap {gap}s: {sum(healthy)} of 40 assessments said healthy for a rail that rejected every write")

    @pytest.mark.parametrize("gap", [30.0, 60.0, 100.0, 150.0, 200.0, 250.0, 299.0, 300.0, 301.0,
                                     450.0, 600.0, 900.0, 1800.0, 3000.0])
    def test_it_is_unhealthy_BETWEEN_writes_too_not_only_at_the_moment_of_one(self, gap):
        """Every caller of self_test reads the verdict at a random moment, not at a write. With
        writes 250 s apart the window holds all three failures for only part of each gap, so a
        rule that is right at write time can still read healthy most of the time in between."""
        w = RollingOutcomes()
        t = 0.0
        greens = []
        for k in range(12):
            w.record(False, now=t)
            if k >= MIN_FAILURES - 1:
                for frac in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
                    if w.assess(now=t + frac * gap)[0]:
                        greens.append((k, frac))
            t += gap
        assert not greens, f"gap {gap}s: read healthy between writes at (write, fraction) {greens[:6]}"

    def test_the_reason_says_what_happened(self):
        w = RollingOutcomes()
        _write_at_gaps(w, "FFF", 400.0)
        healthy, why = w.assess(now=800.0)
        assert healthy is False
        assert "3 writes" in why and "failed" in why and "none has succeeded since" in why

    def test_it_stays_unhealthy_between_writes_for_an_hour(self):
        w = RollingOutcomes()
        _write_at_gaps(w, "FFF", 400.0)              # newest failure at t=800
        assert w.assess(now=800.0 + 3000.0)[0] is False
        assert w.assess(now=800.0 + 3600.0)[0] is False, "exactly one hour old is still evidence"

    def test_evidence_older_than_an_hour_is_not_evidence(self):
        """Silence for an hour after the last failure is not proof of anything: it reads healthy
        again by itself, as a failure that ended always did."""
        w = RollingOutcomes()
        _write_at_gaps(w, "FFF", 400.0)
        assert w.assess(now=800.0 + 3601.0) == (True, None)

    def test_one_success_ends_it_at_once(self):
        w = RollingOutcomes()
        verdicts = _write_at_gaps(w, "FFFS", 400.0)
        assert verdicts[2][0] is False
        assert verdicts[3] == (True, None)

    def test_the_rail_can_fail_again_after_a_success(self):
        w = RollingOutcomes()
        verdicts = _write_at_gaps(w, "FFFSFFF", 400.0)
        assert [v[0] for v in verdicts] == [True, True, False, True, True, True, False]

    def test_traffic_that_sometimes_succeeds_is_not_a_dead_rail(self):
        w = RollingOutcomes()
        verdicts = _write_at_gaps(w, "FFS" * 10, 500.0)
        assert all(v[0] for v in verdicts)

    @pytest.mark.parametrize("gap", [300.0, 1200.0, 3000.0])
    def test_traffic_that_arrives_in_short_sessions_is_flagged_between_sessions(self, gap):
        """A rail whose callers come in bursts: three writes a few seconds apart, then silence.
        Each session on its own looks like a stall, and the window holds it for ten minutes. But
        nothing has succeeded since the FIRST session, so from the second session on the rail is
        down at every poll, not only for the ten minutes after each session."""
        w = RollingOutcomes()
        reds_after_second_session = []
        for session in range(8):
            base = session * gap
            for k in range(3):
                w.record(False, now=base + k * 5.0)
            if session >= 1:
                for frac in (0.0, 0.3, 0.6, 0.9, 0.99):
                    t = base + 10.0 + frac * (gap - 10.0)
                    reds_after_second_session.append((session, frac, w.assess(now=t)[0]))
        assert not any(healthy for _s, _f, healthy in reds_after_second_session), [
            r for r in reds_after_second_session if r[2]][:5]

    def test_a_single_short_session_still_clears_after_the_window(self):
        """The first session alone is a burst that is over: it must clear like any other."""
        w = RollingOutcomes()
        for k in range(3):
            w.record(False, now=k * 5.0)
        assert w.assess(now=10.0)[0] is False
        assert w.assess(now=10.0 + WINDOW_S + 1) == (True, None)

    def test_two_failures_are_not_a_dead_rail_however_long_the_gap(self):
        w = RollingOutcomes()
        _write_at_gaps(w, "FF", 5000.0)
        assert w.assess(now=5000.0 + 100.0)[0] is True

    def test_failures_days_apart_still_count_when_nothing_succeeded_between(self):
        """A rail written to once a day that rejected its last three writes is dead."""
        w = RollingOutcomes()
        day = 86400.0
        _write_at_gaps(w, "FFF", day)
        assert w.assess(now=2 * day + 60.0)[0] is False


class TestABurstThatIsOverStillClears:
    """The sustained rule must not turn a stall into an hour-long outage."""

    def test_a_burst_with_nothing_written_after_it_clears_ten_minutes_later(self):
        w = RollingOutcomes()
        for _ in range(6):
            w.record(False, now=1000.0)
        assert w.assess(now=1000.0 + WINDOW_S - 1)[0] is False
        assert w.assess(now=1000.0 + WINDOW_S + 1) == (True, None)

    def test_three_failures_packed_into_a_stall_are_not_sustained(self):
        w = RollingOutcomes()
        for k in range(3):
            w.record(False, now=2000.0 + k * 5.0)       # 10 seconds in all
        assert w.assess(now=2010.0 + WINDOW_S + 1) == (True, None)

    def test_the_stall_length_boundary_is_pinned_on_both_sides(self):
        """Failures spread over exactly SUSTAINED_MIN_SPAN_S are still a stall; one second more is not."""
        def verdict_after_silence(span):
            w = RollingOutcomes()
            for ts in (0.0, span / 2, span):
                w.record(False, now=ts)
            return w.assess(now=span + WINDOW_S + 1)[0]

        assert verdict_after_silence(SUSTAINED_MIN_SPAN_S) is True
        assert verdict_after_silence(SUSTAINED_MIN_SPAN_S + 1.0) is False

    def test_an_outage_longer_than_a_stall_followed_by_silence_stays_red_for_an_hour(self):
        """Twenty failures, ten seconds apart (190 s), then nothing is written. Silence is not
        proof of recovery for a failure that lasted longer than any stall: red until a write
        succeeds or an hour has gone by."""
        w = RollingOutcomes()
        for k in range(20):
            w.record(False, now=5000.0 + k * 10.0)
        last = 5000.0 + 190.0
        assert w.assess(now=last + WINDOW_S + 1)[0] is False
        assert w.assess(now=last + SUSTAINED_FAILURE_MAX_AGE_S)[0] is False
        assert w.assess(now=last + SUSTAINED_FAILURE_MAX_AGE_S + 1) == (True, None)
        w.record(True, now=last + 900.0)
        assert w.assess(now=last + 900.0) == (True, None)

    def test_a_stall_of_a_minute_and_a_half_is_a_stall_two_and_a_half_minutes_is_an_outage(self):
        """The documented tolerance is two minutes (the pool stalls seen lasted 15 s each)."""
        def verdict_after_silence(span):
            w = RollingOutcomes()
            for ts in (0.0, span / 2, span):
                w.record(False, now=ts)
            return w.assess(now=span + WINDOW_S + 1)[0]

        assert verdict_after_silence(90.0) is True
        assert verdict_after_silence(150.0) is False

    def test_reset_forgets_a_sustained_run_too(self):
        w = RollingOutcomes()
        _write_at_gaps(w, "FFFF", 400.0)
        assert w.assess(now=1200.0)[0] is False
        w.reset()
        assert w.assess(now=1200.0) == (True, None)
        _write_at_gaps(w, "FF", 400.0, start=2000.0)          # two failures after a reset: not dead
        assert w.assess(now=2400.0)[0] is True

    def test_a_failure_long_after_a_burst_makes_it_sustained(self):
        """Failing at t=0 (a burst) and again 700 s later, with nothing succeeding in between,
        is a rail that has not worked for longer than the window."""
        w = RollingOutcomes()
        for _ in range(5):
            w.record(False, now=0.0)
        w.record(False, now=700.0)
        assert w.assess(now=700.0)[0] is False


class TestVerdictsStayConsistent:
    def test_three_successes_in_a_row_read_healthy_even_when_most_writes_failed(self):
        """Documented limit, pinned so it cannot become an accident: recovery is 'the last three
        writes succeeded', not 'the failure rate fell'. A rail failing at random can read healthy
        at the moments three writes in a row happen to succeed."""
        w = RollingOutcomes()
        _write_at_gaps(w, "FFFFF" + "SSS", 1.0)
        snap = w.snapshot(now=8.0)
        assert snap["failed"] == 5 and snap["attempts"] == 8
        assert w.assess(now=8.0) == (True, None)

    def test_the_reason_names_the_cap_when_the_window_holds_fewer_writes_than_ten_minutes(self):
        """MAX_EVENTS caps how many writes are remembered; the wording must not claim ten full
        minutes when the cap cut the window short."""
        w = RollingOutcomes(max_events=50)
        for k in range(200):
            w.record(False, now=1000.0 + k * 0.1)
        healthy, why = w.assess(now=1020.0)
        assert healthy is False
        assert "50 of the last 50 writes" in why
        assert "up to 10 min" in why


class _CountingLock:
    """Stands in for RollingOutcomes._lock and counts how often it is taken."""

    def __init__(self) -> None:
        self.acquisitions = 0
        self._real = threading.Lock()

    def __enter__(self):
        self.acquisitions += 1
        self._real.acquire()
        return self

    def __exit__(self, *exc):
        self._real.release()
        return False


class TestOneConsistentRead:
    """The verdict and the counts it quotes must come from ONE look at the window. With the
    lock taken once for the counts and again for the recovery check, a write that landed between
    the two produced a verdict that contradicted the state it reported."""

    @staticmethod
    def _failing_window() -> RollingOutcomes:
        w = RollingOutcomes()
        for k in range(8):
            w.record(False, now=1000.0 + k)
        return w

    def test_assess_looks_at_the_window_once(self):
        w = self._failing_window()
        w._lock = _CountingLock()
        assert w.assess(now=1010.0)[0] is False
        assert w._lock.acquisitions == 1

    def test_snapshot_looks_at_the_window_once(self):
        w = self._failing_window()
        w._lock = _CountingLock()
        w.snapshot(now=1010.0)
        assert w._lock.acquisitions == 1

    def test_usage_logger_health_reads_the_window_once(self, monkeypatch):
        import billing.usage_logger as ul
        w = self._failing_window()
        w._lock = _CountingLock()
        monkeypatch.setattr(ul, "_window", w)
        # real time: events at 1000.. are long gone from a real monotonic clock, so use the
        # window's own clock by recording fresh failures
        for _ in range(4):
            w.record(False)
        w._lock.acquisitions = 0
        ul.get_usage_logger_health()
        assert w._lock.acquisitions == 1

    def test_durable_meter_health_reads_the_window_once(self, monkeypatch):
        import billing.durable_meter as dm
        w = self._failing_window()
        w._lock = _CountingLock()
        monkeypatch.setattr(dm, "_window", w)
        for _ in range(4):
            w.record(False)
        w._lock.acquisitions = 0
        dm.get_durable_meter_health()
        assert w._lock.acquisitions == 1

    def test_the_counts_a_getter_reports_agree_with_its_verdict(self, monkeypatch):
        import billing.usage_logger as ul
        w = RollingOutcomes()
        monkeypatch.setattr(ul, "_window", w)
        for _ in range(7):
            w.record(False)
        health = ul.get_usage_logger_health()
        assert health["healthy"] is False
        assert health["recent_failed"] == 7 and health["recent_attempts"] == 7
        assert "7 of the last 7 writes" in health["unhealthy_reason"]


class TestSelfTestEndToEnd:
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
    async def test_a_usage_events_rail_rejecting_every_write_at_low_traffic_is_reported(self):
        import billing.usage_logger as ul
        from agent_interface.self_test import _check_metering_pipeline

        now = time.monotonic()
        for ago in (1500.0, 800.0, 100.0):               # one write every ~12 minutes, all rejected
            ul._window.record(False, now=now - ago)
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "usage_events" in check.error

        ul._record_success()                             # a single success clears it
        assert (await _check_metering_pipeline()).passed is True

    @pytest.mark.asyncio
    async def test_a_billing_events_rail_rejecting_every_write_at_low_traffic_is_reported(self):
        import billing.durable_meter as dm
        from agent_interface.self_test import _check_metering_pipeline

        now = time.monotonic()
        for ago in (3000.0, 1900.0, 700.0):
            dm._window.record(False, now=now - ago)
        check = await _check_metering_pipeline()
        assert check.passed is False
        assert "billing_events" in check.error
