"""
Rolling health for the two fire-and-forget write rails (usage_events, billing_events).

WHY THIS EXISTS (2026-10-03, tm_requirements row 358). agent_interface/self_test.py's
`metering_pipeline` check used to report unhealthy when a rail had EVER recorded a failed
write since the process started. That is the right alarm for a rail that has silently died
(AUDIT-2026-09-28: a week of lost rows, no signal), and the wrong one for a rail that lost a
handful of writes during a short outage of the database in front of it. On 2026-10-02 12:12
UTC and again on 2026-10-03 07:16 UTC the shared PostgREST connection pool (10 connections,
used by every service on the spine) starved for 10 to 25 seconds and answered HTTP 504 PGRST003
to everything, usage_events writes included: 13 of the last 24 hours' 15,283 writes failed
(0.085%). `self_test` -- which every directory crawler and the Door Reliability Run call --
was reporting `healthy: false` (observed 18:29 and 18:38 UTC on 2026-10-03) and stayed that way
until the container was restarted at 18:39 and the counter happened to reset, because nothing
else ever resets it. The public health surface was telling every caller that a pipeline
carrying 99.9% of its writes was broken.

THE RULE. A rail is unhealthy when EITHER of two things is true.

  1. The window. Within the last WINDOW_S seconds at least MIN_FAILURES writes failed and
     failures were at least FAILURE_RATIO of the attempts -- unless the rail has already
     recovered: when its most recent MIN_FAILURES writes all succeeded the verdict is healthy.
     A transient failure is one that something succeeded after, and the ratio alone cannot tell
     the two apart after a burst that follows a quiet spell (50 calls fail inside a 15 second
     stall: 50 of 50 attempts, red until 50 more writes succeed or ten minutes pass, long after
     the pool is back). The test is the last three writes, not the rate, so a rail that is mostly
     failing reads healthy at any moment its last three writes happen to succeed.

  2. A sustained run. The window cannot see a rail that rejects every write but is written to
     rarely: one write every 400 seconds puts two failures in a ten-minute window, one every
     250 seconds fills it for only part of each gap, and callers that arrive in short sessions
     (three writes, then twenty quiet minutes) fill it only while a session is fresh. So the
     rail also keeps its current RUN of consecutive failures, everything since the last
     success. If the run is at least MIN_FAILURES long, was spread over MORE than
     SUSTAINED_MIN_SPAN_S (two minutes: a stall of the connection pool lasts seconds, 15 s in
     both observed cases, so this is not one), and its newest failure is at most
     SUSTAINED_FAILURE_MAX_AGE_S old, the rail is unhealthy whatever the window says. One
     success ends the run at once.

What that means in practice:

  * a dead rail (every write rejected, e.g. the RLS case of row 1181) is unhealthy after
    MIN_FAILURES writes at ANY spacing, at any moment between writes too, and stays unhealthy
    until a write succeeds or, if nothing is written, until its newest failure is
    SUSTAINED_FAILURE_MAX_AGE_S (an hour) old. A rail written to less than once an hour is
    therefore judged only for the hour after each write;
  * a rail that lost a short burst during an outage (failures spread over no more than
    SUSTAINED_MIN_SPAN_S) is unhealthy WHILE the burst is in the window and for no longer than
    WINDOW_S after it if nothing is written afterwards, then recovers by itself -- no restart;
    once writes succeed again it is healthy as soon as three in a row have. A failure that
    lasted longer than a stall and was followed by no write at all is not known to be over, so it
    stays unhealthy until a write succeeds or SUSTAINED_FAILURE_MAX_AGE_S has passed;
  * a stray failure among thousands of successes never flips it.

WHAT THIS DOES NOT CATCH, stated so nobody relies on it:

  * A rail that fails at a steady rate below FAILURE_RATIO (one tool or payload shape
    permanently rejected, say 30% of writes) reads healthy. The old check flagged it; this one
    cannot, because the share of failures never reaches the threshold and successes keep
    ending the run. The cumulative counters and `recent_failed`/`recent_attempts` still show it,
    but self_test does not read them.
  * Failures inside the window are judged by the ratio alone unless they form a sustained run:
    three failures in a row that sit among enough successes from the same ten minutes to keep
    the share under FAILURE_RATIO read healthy, which is what keeps a 15 second stall at steady
    traffic from turning the check red.

Cumulative counters (`failed`, `succeeded`, `last_failure_reason`) are untouched and still
exposed by get_usage_logger_health() / get_durable_meter_health(); this only changes WHICH
question the health check asks of them. A write that never completes is a separate alarm
(`pending` in self_test) and is unchanged.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Optional

WINDOW_S = 600.0
MAX_EVENTS = 1000
MIN_FAILURES = 3
FAILURE_RATIO = 0.5
SUSTAINED_MIN_SPAN_S = 120.0
SUSTAINED_FAILURE_MAX_AGE_S = 3600.0


def _ago(seconds: float) -> str:
    """'45 s' or '7 min' -- a length of time for a reason string."""
    seconds = max(0.0, seconds)
    return f"{int(seconds)} s" if seconds < 120 else f"{int(seconds // 60)} min"


class RollingOutcomes:
    """Last-WINDOW_S-seconds record of write outcomes, plus the current run of consecutive
    failures. Thread-safe (DurableMeter records from a worker thread when there is no running
    loop)."""

    def __init__(self, window_s: float = WINDOW_S, max_events: int = MAX_EVENTS,
                 min_failures: int = MIN_FAILURES, failure_ratio: float = FAILURE_RATIO,
                 sustained_min_span_s: float = SUSTAINED_MIN_SPAN_S,
                 sustained_max_age_s: float = SUSTAINED_FAILURE_MAX_AGE_S) -> None:
        self.window_s = float(window_s)
        self.min_failures = int(min_failures)
        self.failure_ratio = float(failure_ratio)
        self.sustained_min_span_s = float(sustained_min_span_s)
        self.sustained_max_age_s = float(sustained_max_age_s)
        self._events: collections.deque = collections.deque(maxlen=int(max_events))
        self._lock = threading.Lock()
        self._clear_run()

    def _clear_run(self) -> None:
        # The run is kept apart from the window on purpose: it must outlive the events that
        # carry it aging out of the window (that is the whole point), and its length is not
        # bounded by MAX_EVENTS. Callers hold the lock, or are the constructor.
        self._run_len = 0
        self._run_first: Optional[float] = None
        self._run_last: Optional[float] = None

    def record(self, ok: bool, now: Optional[float] = None) -> None:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._events.append((stamp, bool(ok)))
            if ok:
                self._clear_run()
            else:
                if self._run_len == 0:
                    self._run_first = stamp
                self._run_len += 1
                self._run_last = stamp

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._clear_run()

    def evaluate(self, now: Optional[float] = None) -> dict:
        """The verdict AND the counts it is quoting, from ONE look at the window.

        {"healthy", "reason" (None when healthy), "attempts", "failed", "succeeded", "window_s"}.
        Everything is read while the lock is held once, so a write that lands during the call
        cannot make the verdict contradict the counts that sit beside it."""
        stamp = time.monotonic() if now is None else now
        window_start = stamp - self.window_s
        n = self.min_failures
        with self._lock:
            while self._events and self._events[0][0] < window_start:
                self._events.popleft()
            attempts = len(self._events)
            failed = sum(1 for _, ok in self._events if not ok)
            recovered = 0 < n <= attempts and all(self._events[i][1] for i in range(-n, 0))
            run_len, run_first, run_last = self._run_len, self._run_first, self._run_last

        healthy, reason = True, None
        if failed >= n and attempts and failed / attempts >= self.failure_ratio:
            if not recovered:
                healthy = False
                reason = (f"{failed} of the last {attempts} writes "
                          f"(up to {int(self.window_s // 60)} min back) failed")
        elif run_len >= max(n, 1) and run_first is not None and run_last is not None:
            span = run_last - run_first
            age = stamp - run_last
            if span > self.sustained_min_span_s and age <= self.sustained_max_age_s:
                healthy = False
                reason = (f"{run_len} writes in a row have failed over {_ago(span)} and none has "
                          f"succeeded since (newest failure {_ago(age)} ago)")
        return {"healthy": healthy, "reason": reason, "attempts": attempts, "failed": failed,
                "succeeded": attempts - failed, "window_s": self.window_s}

    def snapshot(self, now: Optional[float] = None) -> dict:
        """The counts inside the window: {"attempts", "failed", "succeeded", "window_s"}."""
        v = self.evaluate(now)
        return {k: v[k] for k in ("attempts", "failed", "succeeded", "window_s")}

    def assess(self, now: Optional[float] = None) -> tuple:
        """(healthy, reason). `reason` is None when healthy. See the module docstring."""
        v = self.evaluate(now)
        return v["healthy"], v["reason"]


def failure_reason(exc: BaseException) -> str:
    """A short, countable reason for a failed write, naming the real cause.

    storage.supabase_client.rpc() raises RpcError carrying `kind`/`status`/`cause_name`/`pg_code`.
    Anything else keeps the historical `exception:<ClassName>` shape, which existing dashboards
    and tests read. The point is that an httpx timeout has an EMPTY message, so the old
    `rpc('x') transport error: ` told nobody whether the cause was a timeout, a refused
    connection or a TLS failure."""
    kind = getattr(exc, "kind", None)
    if kind == "transport":
        return f"transport:{getattr(exc, 'cause_name', None) or 'unknown'}"
    if kind == "http":
        status = getattr(exc, "status", None)
        code = getattr(exc, "pg_code", None)
        return f"http_{status}" + (f":{code}" if code else "")
    if kind == "decode":
        return "decode_error"
    return f"exception:{type(exc).__name__}"
