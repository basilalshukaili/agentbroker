"""
OpenStreetMap client for find_business: Nominatim geocoding + Overpass search.

THE PUBLIC OSM SERVERS ARE A FREE SERVICE RUN BY VOLUNTEERS, and this module
exists to be a well-behaved guest. The rules below are the usage policies of
nominatim.openstreetmap.org and overpass-api.de, enforced in code rather than
remembered:

  * IDENTIFYING USER-AGENT with a contact, on every request (a default
    library User-Agent is grounds for a block).
  * NOMINATIM: at most one request per second, process-wide, absolute. We
    space request STARTS >= 1.1 s apart and refuse (rather than queue for
    ever) when the wait would exceed MAX_QUEUE_WAIT_S.
  * CACHE. Geocoding results are kept 7 days (a place does not move), misses
    1 hour, Overpass results 6 hours. Repeated and concurrent identical
    queries are answered from memory or coalesced onto one in-flight request,
    so a popular query costs the public servers one request per TTL, not one
    per caller.
  * GLOBAL CONCURRENCY CAP on outbound calls (MAX_CONCURRENT_UPSTREAM).
  * TIMEOUTS on everything, and a circuit breaker per upstream so a server
    that is down or throttling us is left alone instead of hammered.
  * BOUNDED result counts (CANDIDATE_LIMIT in supply/osm_places.py).

FAILURE IS A VALUE, NOT A FALLBACK. Every failure raises OSMUnavailable with a
machine-readable `reason`. The caller (core/find_business.py) turns that into
an honest "temporarily unavailable" result. Nothing here ever substitutes
made-up or sample data for a failed lookup.

THE CACHE IS IN-PROCESS. The service runs one worker (see the Dockerfile), so
a process-level cache and rate gate are exact. A restart empties the cache;
that is acceptable for a free upstream at our volume and is noted as a
follow-up rather than papered over with a new table.

Data is (c) OpenStreetMap contributors, ODbL 1.0. Attribution is added to
every find_business result set by the caller of this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

import httpx

from reliability.circuit_breaker import CircuitBreaker
from supply.osm_categories import CategoryPlan
from supply.osm_places import CANDIDATE_LIMIT, KEEP_NEAREST, build_query, parse_elements

_log = logging.getLogger("smb_broker.osm")

DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Identifying, with a contact. Required by the OSM usage policies.
USER_AGENT = "AgentBroker-find_business/1.0 (+https://hatchloop.dev/; contact: support@hatchloop.dev)"

NOMINATIM_MIN_INTERVAL_S = 1.1        # policy is 1 req/s; keep a margin
MAX_QUEUE_WAIT_S = 6.0                # refuse instead of queueing longer than this
MAX_CONCURRENT_UPSTREAM = 2           # global cap on in-flight OSM requests

NOMINATIM_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
OVERPASS_TIMEOUT = httpx.Timeout(24.0, connect=5.0)
OVERPASS_SERVER_TIMEOUT_S = 18
# The public Overpass server answers 502/503/504 when it is busy, and a second
# try a moment later usually works (seen live during development). One retry,
# after a pause, for a 5xx only - never for a timeout (that already cost 24 s)
# and never for a 429 (the server asked us to slow down).
OVERPASS_RETRY_DELAY_S = 2.0

GEOCODE_TTL_S = 7 * 24 * 3600
GEOCODE_MISS_TTL_S = 3600
OVERPASS_TTL_S = 6 * 3600
CACHE_MAX_ENTRIES = 256

RADIUS_DEFAULT_M = 5000
RADIUS_MAX_M = 40234                  # 25 miles

_DISABLE_ENV = "FIND_BUSINESS_OSM_DISABLED"


class OSMUnavailable(Exception):
    """An OSM lookup could not be completed. `reason` is a closed vocabulary."""

    def __init__(self, reason: str, detail: str = "", retry_after_s: Optional[int] = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.retry_after_s = retry_after_s


class CallerRateLimited(Exception):
    """The per-caller limit on fresh upstream lookups was hit (not an OSM fault)."""

    def __init__(self, retry_after_s: int):
        super().__init__(f"caller rate limited; retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class TokenBucket:
    """Per-key token bucket. Same algorithm as the server's per-IP limiter in
    main.py (_rl_consume), parameterised so this tool can be stricter than the
    server-wide 60-burst limit: a fresh lookup costs the public OSM servers
    real work, a cache hit costs nothing."""

    def __init__(self, capacity: float, refill_per_s: float, *, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 4096, evict_after_s: float = 900.0):
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._clock = clock
        self._max_keys = max_keys
        self._evict_after_s = evict_after_s
        self._buckets: dict[str, list[float]] = {}   # key -> [tokens, last_refill]
        self._ops = 0

    def _evict(self, now: float) -> None:
        cutoff = now - self._evict_after_s
        for k in [k for k, v in self._buckets.items() if v[1] < cutoff]:
            self._buckets.pop(k, None)

    def consume(self, key: str) -> Optional[int]:
        """Take a token. Returns None if allowed, else seconds until one is free."""
        now = self._clock()
        self._ops += 1
        if self._ops % 100 == 0 or len(self._buckets) > self._max_keys:
            self._evict(now)
        b = self._buckets.get(key)
        if b is None:
            self._buckets[key] = [self.capacity - 1.0, now]
            return None
        tokens = min(self.capacity, b[0] + max(0.0, now - b[1]) * self.refill_per_s)
        b[1] = now
        if tokens >= 1.0:
            b[0] = tokens - 1.0
            return None
        b[0] = tokens
        need = (1.0 - tokens) / self.refill_per_s if self.refill_per_s > 0 else 60.0
        return max(1, int(need + 0.999))


class TTLCache:
    """Bounded LRU with a per-entry expiry. Not thread-safe; the event loop is
    the only writer."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, *, clock: Callable[[], float] = time.monotonic):
        self._max = max_entries
        self._clock = clock
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def get(self, key: str) -> tuple[bool, Any]:
        item = self._data.get(key)
        if item is None:
            return False, None
        expires, value = item
        if expires <= self._clock():
            self._data.pop(key, None)
            return False, None
        self._data.move_to_end(key)
        return True, value

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self._data[key] = (self._clock() + ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data.clear()


def _location_key(text: str) -> str:
    return " ".join(str(text).casefold().split())[:200]


def _retry_after(resp: httpx.Response, default: int) -> int:
    try:
        v = int(float(resp.headers.get("retry-after", "")))
        return max(1, min(v, 3600))
    except (TypeError, ValueError):
        return default


class OSMClient:
    """Geocode with Nominatim, search with Overpass, politely."""

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        *,
        nominatim_url: Optional[str] = None,
        overpass_url: Optional[str] = None,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._nominatim_url = nominatim_url or os.getenv("OSM_NOMINATIM_URL") or DEFAULT_NOMINATIM_URL
        # OSM_OVERPASS_URL may list several public instances, comma separated;
        # they are tried in order when one fails. The default is the single
        # canonical server: adding a mirror sends callers' search areas to another
        # operator, which is a deployment decision, not a code default.
        raw_urls = overpass_url or os.getenv("OSM_OVERPASS_URL") or DEFAULT_OVERPASS_URL
        self._overpass_urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
        self._ua = user_agent
        self._clock = clock
        self._sleep = sleep
        self.cache = TTLCache(clock=clock)
        self._inflight: dict[str, "asyncio.Future[Any]"] = {}
        self._next_nominatim_at = 0.0
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._breakers: dict[str, CircuitBreaker] = {}
        # Counters, for tests and for an operator reading logs.
        self.upstream_calls = {"nominatim": 0, "overpass": 0}

    # ------------------------------------------------------------------ gates

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM)
            self._sem_loop = loop
        return self._sem

    async def _acquire_slot(self) -> asyncio.Semaphore:
        sem = self._semaphore()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=MAX_QUEUE_WAIT_S)
        except asyncio.TimeoutError:
            raise OSMUnavailable("busy", "too many OpenStreetMap lookups in flight", retry_after_s=5)
        return sem

    async def _space_nominatim(self) -> None:
        """Reserve the next Nominatim start slot (>= 1.1 s after the previous)."""
        now = self._clock()
        slot = max(now, self._next_nominatim_at)
        wait = slot - now
        if wait > MAX_QUEUE_WAIT_S:
            raise OSMUnavailable("busy", "geocoding queue is full", retry_after_s=int(wait) + 1)
        self._next_nominatim_at = slot + NOMINATIM_MIN_INTERVAL_S
        if wait > 0:
            await self._sleep(wait)

    async def _coalesce(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run `factory` once for concurrent identical requests."""
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)
        fut: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await factory()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()          # mark retrieved: no "never retrieved" noise if nobody waited
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    # ------------------------------------------------------------------- http

    def _breaker(self, key: str) -> CircuitBreaker:
        b = self._breakers.get(key)
        if b is None:
            b = CircuitBreaker(name=f"osm_{key}", failure_threshold=4, recovery_timeout_s=60.0)
            self._breakers[key] = b
        return b

    def overpass_breaker(self, index: int = 0) -> CircuitBreaker:
        """The breaker for the index-th configured Overpass endpoint (tests, ops)."""
        return self._breaker(f"overpass|{self._overpass_urls[index]}")

    async def _attempt(self, upstream: str, method: str, url: str, timeout: httpx.Timeout,
                       validate: Optional[Callable[[Any], None]], **kwargs: Any) -> Any:
        """One HTTP request. Raises OSMUnavailable; never touches a breaker."""
        sem = await self._acquire_slot()
        self.upstream_calls[upstream] += 1
        try:
            headers = {"User-Agent": self._ua, "Accept": "application/json"}
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout,
                                         headers=headers, follow_redirects=False) as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise OSMUnavailable("upstream_timeout", f"{upstream} timed out", retry_after_s=30) from exc
        except httpx.HTTPError as exc:
            raise OSMUnavailable("upstream_unreachable", f"{upstream}: {type(exc).__name__}",
                                 retry_after_s=30) from exc
        finally:
            sem.release()

        status = resp.status_code
        if status == 429:
            raise OSMUnavailable("upstream_rate_limited", f"{upstream} asked us to slow down",
                                 retry_after_s=_retry_after(resp, 60))
        if status in (403, 401):
            raise OSMUnavailable("upstream_blocked", f"{upstream} refused the request (HTTP {status})",
                                 retry_after_s=300)
        if status >= 500:
            raise OSMUnavailable("upstream_error", f"{upstream} returned HTTP {status}", retry_after_s=30)
        if status != 200:
            raise OSMUnavailable("upstream_rejected", f"{upstream} returned HTTP {status}", retry_after_s=60)
        try:
            data = resp.json()
        except ValueError as exc:
            raise OSMUnavailable("bad_response", f"{upstream} returned non-JSON", retry_after_s=30) from exc
        if validate is not None:
            validate(data)
        return data

    async def _request(self, upstream: str, method: str, url: str, timeout: httpx.Timeout,
                       guard: Optional[Callable[[], None]], *, breaker_key: Optional[str] = None,
                       retries: int = 0, validate: Optional[Callable[[Any], None]] = None,
                       **kwargs: Any) -> Any:
        """Policy wrapper: kill switch, circuit breaker, per-caller guard, spacing,
        then the request (with at most `retries` more tries on a 5xx). The breaker
        sees ONE outcome per call, however many tries it took."""
        if os.getenv(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
            raise OSMUnavailable("disabled", "OpenStreetMap lookups are switched off by the operator")
        breaker = self._breaker(breaker_key or upstream)
        if not breaker.is_available():
            raise OSMUnavailable("circuit_open", f"{upstream} is failing; not retrying yet",
                                 retry_after_s=int(breaker.recovery_timeout_s))
        if guard is not None:
            guard()                                    # may raise CallerRateLimited
        last: Optional[OSMUnavailable] = None
        for attempt in range(1 + max(0, retries)):
            if attempt:
                await self._sleep(OVERPASS_RETRY_DELAY_S)
            if upstream == "nominatim":
                await self._space_nominatim()
            try:
                data = await self._attempt(upstream, method, url, timeout, validate, **kwargs)
            except OSMUnavailable as exc:
                if exc.reason == "busy":
                    raise                              # our own queue, not the server's fault
                last = exc
                if exc.reason != "upstream_error":
                    break
                continue
            breaker.record_success()
            return data
        breaker.record_failure()
        assert last is not None
        raise last

    # ---------------------------------------------------------------- geocode

    async def geocode(self, query: str, *, guard: Optional[Callable[[], None]] = None) -> Optional[dict]:
        """Resolve free text to one place, or None if OSM does not know it.

        Returns {"latitude", "longitude", "display_name", "osm_type", "osm_id",
        "cached"}; raises OSMUnavailable on an upstream fault.
        """
        key = "geo:" + _location_key(query)
        hit, value = self.cache.get(key)
        if hit:
            return dict(value, cached=True) if value else None

        async def _fetch() -> Optional[dict]:
            # A second look after winning the coalesce race: another caller may
            # have populated the cache while we waited.
            hit2, value2 = self.cache.get(key)
            if hit2:
                return dict(value2, cached=True) if value2 else None
            data = await self._request(
                "nominatim", "GET", self._nominatim_url, NOMINATIM_TIMEOUT, guard,
                params={"q": query[:200], "format": "jsonv2", "limit": "1",
                        "accept-language": "en"},
            )
            place = _first_place(data)
            self.cache.set(key, place, GEOCODE_TTL_S if place else GEOCODE_MISS_TTL_S)
            return dict(place, cached=False) if place else None

        return await self._coalesce(key, _fetch)

    async def _overpass(self, ql: str, guard: Optional[Callable[[], None]]) -> dict:
        """POST the query to each configured endpoint in turn until one answers."""
        fired = {"done": False}

        def once() -> None:                    # a fallback attempt must not cost the caller a second token
            if guard is not None and not fired["done"]:
                fired["done"] = True
                guard()

        urls = self._overpass_urls
        last: Optional[OSMUnavailable] = None
        for url in urls:
            try:
                return await self._request(
                    "overpass", "POST", url, OVERPASS_TIMEOUT, once,
                    breaker_key=f"overpass|{url}", retries=1 if len(urls) == 1 else 0,
                    validate=_validate_overpass, data={"data": ql})
            except OSMUnavailable as exc:
                if exc.reason in ("disabled", "busy"):
                    raise
                last = exc
        assert last is not None
        raise last

    # ------------------------------------------------------------------ search

    async def search(self, plan: CategoryPlan, lat: float, lon: float, radius_m: int,
                     *, guard: Optional[Callable[[], None]] = None) -> dict:
        """Nearest-first businesses for `plan` around a point.

        Returns {"businesses": [...], "candidates_considered": int,
        "results_capped": bool, "cached": bool}; raises OSMUnavailable.
        """
        ql = build_query(plan, lat, lon, radius_m, server_timeout_s=OVERPASS_SERVER_TIMEOUT_S)
        key = "ovp:" + hashlib.sha256(ql.encode("utf-8")).hexdigest()
        hit, value = self.cache.get(key)
        if hit:
            return dict(value, cached=True)

        async def _fetch() -> dict:
            hit2, value2 = self.cache.get(key)
            if hit2:
                return dict(value2, cached=True)
            data = await self._overpass(ql, guard)
            elements = data["elements"]
            result = {
                "businesses": parse_elements(elements, plan, lat, lon, keep=KEEP_NEAREST),
                "candidates_considered": len(elements),
                "results_capped": len(elements) >= CANDIDATE_LIMIT,
            }
            self.cache.set(key, result, OVERPASS_TTL_S)
            return dict(result, cached=False)

        return await self._coalesce(key, _fetch)


def _validate_overpass(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise OSMUnavailable("bad_response", "overpass returned an unexpected shape", retry_after_s=30)
    remark = data.get("remark")
    if isinstance(remark, str) and ("runtime error" in remark.lower() or "timed out" in remark.lower()):
        # Overpass answers 200 with a remark when it gave up or ran out of
        # memory; the elements are then incomplete. Never cached, never returned.
        raise OSMUnavailable("upstream_timeout", "overpass could not finish the query", retry_after_s=30)


def _first_place(data: Any) -> Optional[dict]:
    """Pull the top Nominatim hit out of its JSON array, defensively."""
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    top = data[0]
    try:
        lat = float(top.get("lat"))
        lon = float(top.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    display = top.get("display_name")
    osm_id = top.get("osm_id")
    return {
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "display_name": " ".join(display.split())[:300] if isinstance(display, str) else None,
        "osm_type": top.get("osm_type") if top.get("osm_type") in ("node", "way", "relation") else None,
        "osm_id": osm_id if isinstance(osm_id, int) and not isinstance(osm_id, bool) else None,
    }


# ---------------------------------------------------------------------------
# Process-wide singleton. Tests replace it with set_client().
# ---------------------------------------------------------------------------

_CLIENT: Optional[OSMClient] = None


def get_client() -> OSMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OSMClient()
    return _CLIENT


def set_client(client: Optional[OSMClient]) -> Optional[OSMClient]:
    """Install `client` (or None to reset to a lazily created default).
    Returns the previous one."""
    global _CLIENT
    previous = _CLIENT
    _CLIENT = client
    return previous


# One limiter for the whole process: burst of 10 fresh upstream requests per
# caller, then one every 6 s (~10/min). A find_business cache miss costs two
# (geocode + search); cache hits cost none.
CALLER_LIMITER = TokenBucket(capacity=10, refill_per_s=1.0 / 6.0)
