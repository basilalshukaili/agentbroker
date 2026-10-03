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

THE RULE. A rail is unhealthy when, within the last WINDOW_S seconds, at least MIN_FAILURES
writes failed AND failures were at least FAILURE_RATIO of the attempts. So:

  * a dead rail (every write rejected, e.g. the RLS case of row 1181) is unhealthy after
    MIN_FAILURES attempts, however little traffic there is;
  * a rail that lost a burst during an outage is unhealthy WHILE the burst is in the window
    and for no longer than WINDOW_S after it, then recovers by itself -- no restart;
  * a stray failure among thousands of successes never flips it.

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


class RollingOutcomes:
    """Last-WINDOW_S-seconds record of write outcomes. Thread-safe (DurableMeter records from a
    worker thread when there is no running loop)."""

    def __init__(self, window_s: float = WINDOW_S, max_events: int = MAX_EVENTS,
                 min_failures: int = MIN_FAILURES, failure_ratio: float = FAILURE_RATIO) -> None:
        self.window_s = float(window_s)
        self.min_failures = int(min_failures)
        self.failure_ratio = float(failure_ratio)
        self._events: collections.deque = collections.deque(maxlen=int(max_events))
        self._lock = threading.Lock()

    def record(self, ok: bool, now: Optional[float] = None) -> None:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._events.append((stamp, bool(ok)))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def snapshot(self, now: Optional[float] = None) -> dict:
        stamp = time.monotonic() if now is None else now
        cutoff = stamp - self.window_s
        with self._lock:
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
            attempts = len(self._events)
            failed = sum(1 for _, ok in self._events if not ok)
        return {"attempts": attempts, "failed": failed, "succeeded": attempts - failed,
                "window_s": self.window_s}

    def assess(self, now: Optional[float] = None) -> tuple:
        """(healthy, reason). `reason` is None when healthy."""
        snap = self.snapshot(now)
        attempts, failed = snap["attempts"], snap["failed"]
        if failed >= self.min_failures and attempts and failed / attempts >= self.failure_ratio:
            return False, (f"{failed} of the last {attempts} writes in the past "
                           f"{int(self.window_s // 60)} min failed")
        return True, None


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
