"""Sliding-window rate limits for the unauthenticated parts of the sign-in.

The OAuth pages and endpoints are the one place on this service where an anonymous stranger can make US send
an email to an address of THEIR choosing, register a record in our database, or make our server fetch a URL
of their choosing. Each of those needs a ceiling that is not the caller's goodwill.

In-memory and per-process, deliberately: this is a single-container deployment, the ceilings are far below
anything legitimate (one person signs in once), and a limiter that needed a database round-trip would be a
new way for a database blip to lock everybody out of sign-in. The durable, un-raceable limits - resend gap and
resend ceiling per sign-in - live in the database function that records the send.

Never keyed by anything secret: callers pass an address hash, a request id or a digest of an email.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

_MAX_KEYS = 20000


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict = {}
        self._calls = 0

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        """Record one attempt and say whether it is within `limit` per `window_s`. A refused attempt is not
        recorded, so a caller that keeps hammering does not extend its own lockout."""
        now = self._clock()
        self._calls += 1
        if self._calls % 200 == 0 or len(self._hits) > _MAX_KEYS:
            self._evict(now)
        q = self._hits.get(key)
        if q is None:
            q = self._hits[key] = deque()
        floor = now - window_s
        while q and q[0] <= floor:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _evict(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] < now - 86400]:
            self._hits.pop(k, None)
        if len(self._hits) > _MAX_KEYS:
            # still too many live keys: an attack. Dropping them all fails OPEN for one window, which is
            # better than growing without bound; the database-side ceilings still hold.
            self._hits.clear()

    def reset(self) -> None:
        self._hits.clear()


LIMITS = RateLimiter()

# (name, limit, window seconds). One table, so the numbers are reviewable in one place.
HOUR = 3600.0
POLICY = {
    "signin_start_ip": (30, HOUR),        # GET /oauth/authorize creates a database row
    "email_ip": (8, HOUR),                # mails sent per source address
    "email_recipient": (4, HOUR),         # mails sent to one recipient (digest), however many sign-ins
    "email_global": (400, HOUR),          # mails sent by the whole service through this door
    "register_ip": (20, HOUR),            # dynamic client registrations per source address
    "register_global": (600, HOUR),
    "metadata_fetch_ip": (30, HOUR),      # authorize requests that make us fetch a client's metadata URL
    "metadata_fetch_host": (60, HOUR),    # ... per target host, so we cannot be aimed at one site
    "poll_request": (120, 60.0),          # status polls per sign-in per minute
    "verify_ip": (60, HOUR),              # magic-link opens/presses per source address
    "match_attempt": (5, 900.0),          # Confirm presses without the starting browser, per mailed link
    "token_ip": (300, HOUR),
}


def check(name: str, key: str) -> bool:
    limit, window = POLICY[name]
    return LIMITS.allow(f"{name}:{key}", limit, window)
