"""Sliding-window rate limits for the unauthenticated parts of the sign-in.

The OAuth pages and endpoints are the one place on this service where an anonymous stranger can make US send
an email to an address of THEIR choosing, register a record in our database, or make our server fetch a URL
of their choosing. Each of those needs a ceiling that is not the caller's goodwill.

In-memory and per-process, deliberately: this is a single-container deployment, the ceilings are far below
anything legitimate (one person signs in once), and a limiter that needed a database round-trip would be a
new way for a database blip to lock everybody out of sign-in. The durable, un-raceable limits - resend gap and
resend ceiling per sign-in - live in the database function that records the send.

ONE FLOODED LIMITER MUST NEVER REOPEN ANOTHER. Every limiter has its own table of keys with its own size
cap, and a table that overflows drops ITS OWN least recently used keys - never the table of any other limiter
and never all of its own. The first version kept one shared table and cleared it entirely when it passed
20,000 keys; the poll endpoint counted a key per request id with no proof that the id existed, so a stranger
could post 20,000 made-up ids and switch off every ceiling here, the sender-reputation ones included (found by
the 2026-10-03 adversarial review). The keys that a stranger can multiply freely are also the ones nothing
else depends on.

Never keyed by anything secret: callers pass an address hash, a request id or a digest of an email.
"""
from __future__ import annotations

import itertools
import time
from collections import OrderedDict, deque
from typing import Callable

_MAX_KEYS_PER_NAME = 20000


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 max_keys_per_name: int = _MAX_KEYS_PER_NAME) -> None:
        self._clock = clock
        self._max = max_keys_per_name
        self._tables: dict = {}            # limiter name -> OrderedDict[key] -> deque of hit times (LRU order)
        self._calls = 0

    @staticmethod
    def _split(key: str) -> tuple:
        name, _, rest = key.partition(":")
        return name, rest

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        """Record one attempt and say whether it is within `limit` per `window_s`. A refused attempt is not
        recorded, so a caller that keeps hammering does not extend its own lockout. `key` is
        "<limiter name>:<subject>"; the name picks the table."""
        now = self._clock()
        name, subject = self._split(key)
        table = self._tables.get(name)
        if table is None:
            table = self._tables[name] = OrderedDict()
        self._calls += 1
        if self._calls % 500 == 0:
            self._sweep(now)
        q = table.get(subject)
        if q is None:
            q = table[subject] = deque()
            while len(table) > self._max:
                table.popitem(last=False)          # this limiter's least recently used key, nobody else's
        else:
            table.move_to_end(subject)
        floor = now - window_s
        while q and q[0] <= floor:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Forget keys that have been quiet for a day (a bounded amount of work, done now and then)."""
        for table in self._tables.values():
            for k in [k for k, q in itertools.islice(table.items(), 2000) if not q or q[-1] < now - 86400]:
                table.pop(k, None)

    def reset(self) -> None:
        self._tables.clear()


LIMITS = RateLimiter()

# (name, limit, window seconds). One table, so the numbers are reviewable in one place.
HOUR = 3600.0
POLICY = {
    "signin_start_ip": (30, HOUR),        # GET /oauth/authorize creates a database row
    "email_ip": (8, HOUR),                # mails sent per source address
    "email_recipient": (4, HOUR),         # mails sent to one recipient (digest), however many sign-ins
    "email_global": (400, HOUR),          # mails sent by the whole service through this door
    # Registration and the token endpoint are called by assistant vendors from the SHARED addresses of their
    # back ends, once per user per hour (a refresh) or per connector set-up, so a per-address ceiling sized for
    # one person's browser would cap an entire vendor. These are abuse ceilings, not fairness ones.
    "register_ip": (120, HOUR),           # dynamic client registrations per source address
    "register_global": (2000, HOUR),
    "metadata_fetch_ip": (30, HOUR),      # REAL outbound fetches of a client's metadata URL, per source address
    "metadata_fetch_host": (60, HOUR),    # ... per target host, so we cannot be aimed at one site
    "poll_ip": (300, 60.0),               # status polls per source address per minute (a waiting page sends ~30)
    "poll_request": (120, 60.0),          # status polls per sign-in per minute
    "verify_ip": (60, HOUR),              # magic-link opens/presses per source address
    "match_attempt": (5, 900.0),          # Confirm presses without the starting browser, per mailed link
    "token_ip": (3000, HOUR),
}


def check(name: str, key: str) -> bool:
    limit, window = POLICY[name]
    return LIMITS.allow(f"{name}:{key}", limit, window)
