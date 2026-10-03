"""
Keep the popular find_business lookups warm, so the first caller after a restart does not pay for them.

WHY. A cold lookup costs a Nominatim request and an Overpass query against two volunteer-run public
servers; measured live on 2026-10-03 it took 16-25 s. The cache makes every repeat instant, but it is
in-process, so each deploy empties it, and a lookup that was never made is never warm. The tool's
five-second budget (core/find_business.CALL_BUDGET_S) turns a slow cold lookup into a "still
searching, repeat shortly" answer - which is honest but is still a worse first impression than an
answer. This module makes the likeliest first lookups already be in the cache.

HOW IT STAYS A WELL-BEHAVED GUEST (supply/osm_client.py's rules still apply, because the pre-warm
goes through the same client):
  * one lookup at a time, with a pause between lookups (PAUSE_S) - a few per minute at most;
  * a short list (places x kinds below), refreshed every REFRESH_EVERY_S, which is under the 6-hour
    cache lifetime so an entry is replaced before it expires;
  * it stops the whole run the moment the upstream says stop (a 429, a 403, an open circuit breaker,
    the operator's kill switch) and after a few failures in a row - it never retries into a problem;
  * it starts START_DELAY_S after boot, never during it (a start-up warm-up of the sanctions lists
    once ran the container out of memory; see main.py).

WHICH LOOKUPS. These are NOT measured popularity: arguments are recorded as names only, so no log says
which place x kind pairs callers use. They are the places and kinds this product's own documentation
and tests use, plus the cities TechMate works in. When `result_count` and the arguments are logged,
replace them with the pairs callers actually ask for.

The warm lookups are made exactly the way a real call makes them (the same `_osm_phase`), so their
cache keys are the real ones; a test proves a later real call is answered from cache with no upstream
request.

ON BY DEFAULT ONLY IN PRODUCTION. The container's environment file is built from the variable names
the code reads, so a new opt-in flag would simply never be set there; the default is therefore the
behaviour we want, with FIND_BUSINESS_PREWARM=0 as the kill switch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional, Sequence

import config
from core import find_business as FB
from supply import osm_categories
from supply.osm_client import OSMUnavailable, RADIUS_DEFAULT_M, get_client

_log = logging.getLogger("smb_broker.find_business_prewarm")

PLACES: tuple = (
    "Muscat, Oman",
    "Nizwa, Oman",
    "Salalah, Oman",
    "Dubai, United Arab Emirates",
    "London, United Kingdom",
    "Atlanta, Georgia",
)
KINDS: tuple = ("restaurant", "dentist", "plumber", "hairdresser")

START_DELAY_S = 120.0
PAUSE_S = 20.0
REFRESH_EVERY_S = 5 * 3600.0
MAX_CONSECUTIVE_FAILURES = 4

# Reasons that mean "the upstream (or the operator) said stop": end the run, do not try the next pair.
STOP_REASONS = frozenset({"upstream_rate_limited", "upstream_blocked", "circuit_open", "disabled"})


def pairs() -> list:
    return [(p, k) for p in PLACES for k in KINDS]


def enabled() -> bool:
    # The default is the word "auto", NOT an empty string, on purpose: scripts/check_deploy_env.py (in the
    # HatchLoop tree) derives the variables a deploy must supply from the code, and counts an
    # empty-string default as "required and unset". An optional switch spelled that way would block the
    # next deploy over a variable nobody needs to set.
    raw = os.getenv("FIND_BUSINESS_PREWARM", "auto").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return config.ENVIRONMENT == "production"


async def run_once(*, client=None, lookups: Optional[Sequence] = None, pause_s: float = PAUSE_S,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, refresh: bool = True) -> dict:
    """Warm every (place, kind) once. Returns counts; never raises for an upstream problem."""
    client = client or get_client()
    todo = list(lookups if lookups is not None else pairs())
    stats = {"attempted": 0, "warmed": 0, "failed": 0, "aborted": None}
    failures_in_a_row = 0
    for i, (place, kind) in enumerate(todo):
        if i and pause_s > 0:
            await sleep(pause_s)
        stats["attempted"] += 1
        plan = osm_categories.resolve(None, kind, None)
        state: dict = {}
        try:
            # narrow_until_s=inf: nobody is waiting, so a dense area is narrowed fully and the narrowed
            # answer is cached too. refresh: replace the entry now rather than wait for it to expire.
            await FB._osm_phase(client, plan, place, RADIUS_DEFAULT_M, None, time.monotonic(), state,
                                narrow_until_s=float("inf"), refresh=refresh)
        except OSMUnavailable as exc:
            stats["failed"] += 1
            failures_in_a_row += 1
            if exc.reason in STOP_REASONS:
                stats["aborted"] = exc.reason
                _log.warning("find_business_prewarm stopped: %s", exc.reason)
                break
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                stats["aborted"] = "consecutive_failures"
                _log.warning("find_business_prewarm stopped after %d failures in a row", failures_in_a_row)
                break
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                    # noqa: BLE001 - a warm-up must not take anything down
            stats["failed"] += 1
            failures_in_a_row += 1
            _log.warning("find_business_prewarm error %s", type(exc).__name__)
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                stats["aborted"] = "consecutive_failures"
                break
            continue
        failures_in_a_row = 0
        if state.get("found") is not None:
            stats["warmed"] += 1
    return stats


async def prewarm_loop(*, client=None, lookups: Optional[Sequence] = None,
                       start_delay_s: float = START_DELAY_S, interval_s: float = REFRESH_EVERY_S,
                       pause_s: float = PAUSE_S,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Warm, wait, warm again - until cancelled. One bad pass never ends the loop."""
    await sleep(start_delay_s)
    while True:
        try:
            stats = await run_once(client=client, lookups=lookups, pause_s=pause_s, sleep=sleep)
            _log.warning("find_business_prewarm pass %s", stats)       # WARNING: INFO never reaches `docker logs` here
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                    # noqa: BLE001
            _log.warning("find_business_prewarm pass failed %s", type(exc).__name__)
        await sleep(interval_s)


def start() -> "Optional[asyncio.Task]":
    """Start the loop on the running event loop, or return None when pre-warm is off."""
    if not enabled():
        return None
    return asyncio.get_running_loop().create_task(prewarm_loop())
