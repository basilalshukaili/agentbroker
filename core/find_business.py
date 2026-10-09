"""
find_business — core operation handler.

Returns REAL businesses near a place, from two sources, each labelled:

  * OpenStreetMap (primary). Nominatim resolves `location.zip_or_city` to a
    point; Overpass returns mapped shops, crafts, offices and amenities of the
    requested kind within a radius, sorted by distance. Fields are only what the
    map holds (name, address parts, phone, website, opening_hours, lat/lon,
    OSM id and url). Missing means absent. Every result set carries the ODbL
    attribution "(c) OpenStreetMap contributors". See supply/osm_client.py for
    the usage-policy safeguards (User-Agent, 1 req/s, cache, timeouts,
    concurrency cap, per-caller limit, circuit breaker).

  * The AgentBroker supply network: businesses added through
    import_booking_url, i.e. ones we can actually book. Sample rows
    (is_demo=true, names prefixed [DEMO]) are NOT returned here any more -
    they remain in the directory only for the other tools' sandbox flows.

IF OPENSTREETMAP CANNOT BE REACHED the answer says so ("temporarily
unavailable", retriable) and returns only what the supply network itself
holds. It never substitutes sample rows, and it never reports an empty list
as though it meant "no such businesses exist".

THE WHOLE CALL HAS A BUDGET (CALL_BUDGET_S, 5 s). Measured live on 2026-10-03,
a successful lookup took 16-25 s because the public Overpass server is slow and
busy. At the budget the call returns what is ready - the resolved place, any
supply-network rows, or a search that has already come back - and says it is
`search_in_progress` (status partial, not an error). The lookup is NOT
cancelled: it carries on in the background, fills the cache, and an identical
repeat call is answered from it. Callers are told to repeat, and when.

"NEAREST" IS ONLY TRUE WHEN THE SEARCH WAS COMPLETE. Overpass cannot order by
distance: it returns at most CANDIDATE_LIMIT features in its own order, and we
sort that sample. In a dense city at the default 5 km radius the sample is
capped, and sorting it yields "nearest among those examined", not "nearest".
Measured live (cafes near central London): the 20 rows returned skipped cafes
69 m, 83 m and 96 m away. So when the first answer is capped we search again
in a smaller circle (NARROW_FACTOR of the radius, at most MAX_NARROW_STEPS
times) until the answer is complete, and report the radius actually used. If it
is still capped after that, the response and the message say so plainly.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
import uuid
from typing import Optional

from core.caller_context import CALLER_KEY
from core.models import (
    FindBusinessRequest, OutcomeReceipt, OperationStatus, SMBRecord, CostRecord
)
from core.untrusted import fence as _fence
from supply import osm_categories
from supply.osm_client import (
    CALLER_LIMITER, CallerRateLimited, OSMUnavailable, RADIUS_DEFAULT_M,
    RADIUS_MAX_M, get_client,
)
from supply.osm_places import CANDIDATE_LIMIT, OSM_ATTRIBUTION, OSM_COPYRIGHT_URL, OSM_LICENSE
from supply.smb_directory import get_directory
from telemetry.metrics import increment_businesses_found
from billing.pricing import receipt_usd as _receipt_usd

_log = logging.getLogger("smb_broker.find_business")

_METERS_PER_MILE = 1609.344
_MIN_RADIUS_M = 200


def _budget_from_env() -> float:
    """FIND_BUSINESS_BUDGET_S, clamped to 1-25 s; 5 when unset or unreadable.

    The default is the literal "5", not an empty string, on purpose: scripts/check_deploy_env.py (in the
    HatchLoop tree) derives the variables a deploy must supply from the code and counts an empty-string
    default as "required and unset" - which would have blocked the next deploy over a tuning knob."""
    try:
        v = float(os.getenv("FIND_BUSINESS_BUDGET_S", "5"))
    except ValueError:
        v = 5.0
    return max(1.0, min(25.0, v))


# A capped answer is retried in a circle this fraction of the size. 0.4 (area
# x0.16) is chosen so a circle that was just over the cap still holds roughly
# 24 of the 150 candidates, i.e. at least the 20 rows we can return.
NARROW_FACTOR = 0.4
MAX_NARROW_STEPS = 2
# Do not START another lookup to narrow once this much time has gone: an
# answer that says "not guaranteed nearest" beats one that arrives too late.
NARROW_ONLY_BEFORE_S = 2.0

# THE BUDGET FOR THE WHOLE CALL (geocode + search + narrowing): what a caller waits at most.
#
# It was 25 s, and measured live on 2026-10-03 a successful lookup took 16-25 s (p50 16.2 s over 8 ok
# calls, five of them 16-25 s) because the public Overpass server is slow and busy, and narrowing a
# dense area added up to two more sequential queries. A caller that waits 25 s for a free lookup has
# usually gone. 5 s is what an agent will tolerate in a tool call.
#
# THE LOOKUP IS NOT ABANDONED AT THE BUDGET. It used to be cancelled (wait_for), which meant a query
# that needs 7 s could never finish: every attempt was killed at the limit, so the cache never warmed
# and the same slow lookup was started again, and killed again, for every caller. Now the call returns
# what is ready at the budget and the lookup carries on in the background (bounded, see
# MAX_BACKGROUND_PHASES), fills the cache, and an identical repeat is answered from it instantly.
CALL_BUDGET_S = _budget_from_env()
# How many lookups may be running past their caller's budget at once. Each already holds a slot in the
# OSM client's own concurrency cap and spacing; this stops a burst of distinct slow queries from
# queueing an unbounded number of tasks behind it. Over the cap, a call falls back to waiting inline and
# is cancelled at its budget, exactly as before.
MAX_BACKGROUND_PHASES = 12
# What a pending answer tells the caller to wait before repeating the call.
PENDING_RETRY_AFTER_S = 10

_BACKGROUND: "set[asyncio.Task]" = set()

# The documented ZIP form of this tool is US-only (its examples were 02139 and
# 30309). Nominatim is global: "02139" alone resolves to a district of Kyiv.
_US_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")


def _country_hint(text: str) -> Optional[str]:
    """ISO country to restrict the geocoder to, or None. Only a bare 5-digit
    or ZIP+4 string is restricted (to the US); anything with a word in it, a
    comma or a country is left to the geocoder as written."""
    return "us" if _US_ZIP.match((text or "").strip()) else None


def _radius(request: FindBusinessRequest) -> tuple[int, bool, bool]:
    """(radius_m, caller_supplied_it, was_capped)."""
    miles = request.location.radius_miles
    if miles is None:
        return RADIUS_DEFAULT_M, False, False
    wanted = int(round(float(miles) * _METERS_PER_MILE))
    capped = wanted > RADIUS_MAX_M
    return max(_MIN_RADIUS_M, min(wanted, RADIUS_MAX_M)), True, capped


def _km(metres: int) -> str:
    return f"{metres} m" if metres < 1000 else f"{metres / 1000:.1f} km"


def _guard_for(caller: Optional[str]):
    """A callable run immediately before each FRESH upstream request (never on
    a cache hit), or None when there is no HTTP caller to limit."""
    if not caller:
        return None

    def _guard() -> None:
        wait = CALLER_LIMITER.consume(caller)
        if wait is not None:
            raise CallerRateLimited(wait)

    return _guard


async def _osm_phase(client, plan, text: str, radius_m: int, guard, t0: float, state: dict, *,
                     narrow_until_s: Optional[float] = None, refresh: bool = False) -> None:
    """Geocode, search, and narrow a capped search. Progress is written into
    `state` as it happens, so that if the overall deadline cuts this short the
    caller still has whatever was already obtained.

    `narrow_until_s` is how long after `t0` a narrowing lookup may still START (default
    NARROW_ONLY_BEFORE_S, read at call time). `refresh` re-fetches instead of reading the
    cache. Only the scheduled pre-warm passes either: it has no caller waiting, so it narrows
    fully and replaces entries before they expire."""
    narrow_limit = NARROW_ONLY_BEFORE_S if narrow_until_s is None else narrow_until_s
    extra = {"refresh": True} if refresh else {}   # an older/test client without the kwarg keeps working
    country = _country_hint(text)
    state["country_codes"] = country
    place = await client.geocode(text, guard=guard, country_codes=country)
    state["place"] = place
    if place is None:
        return
    lat, lon = place["latitude"], place["longitude"]

    found = await client.search(plan, lat, lon, radius_m, guard=guard, **extra)
    state["found"] = found
    state["radius_m"] = radius_m

    steps = 0
    while found["results_capped"] and steps < MAX_NARROW_STEPS:
        narrower_m = max(_MIN_RADIUS_M, int(state["radius_m"] * NARROW_FACTOR))
        if narrower_m >= state["radius_m"]:
            break
        if time.monotonic() - t0 > narrow_limit:
            state["narrow_stopped"] = "time_budget"
            break
        try:
            narrower = await client.search(plan, lat, lon, narrower_m, guard=guard, **extra)
        except CallerRateLimited:
            state["narrow_stopped"] = "caller_rate_limited"
            break
        except OSMUnavailable as exc:
            state["narrow_stopped"] = exc.reason
            break
        found = narrower
        state["found"] = found
        state["radius_m"] = narrower_m
        steps += 1


def _reap(task: "asyncio.Task") -> None:
    """Done-callback for a lookup left running past its caller's budget: forget it, and retrieve any
    exception so asyncio does not log 'Task exception was never retrieved' for an outage we already
    reported (or will report to the next caller) in the normal way."""
    _BACKGROUND.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, (OSMUnavailable, CallerRateLimited)):
        _log.warning("find_business background_lookup_failed %s", type(exc).__name__)


async def _run_within_budget(coro, remaining_s: float) -> bool:
    """Run the OpenStreetMap phase for at most `remaining_s`.

    True: it finished inside the budget (its own exception, if it raised one, propagates from here, so
    the caller's except-clauses see exactly what they saw when this was a plain await).
    False: the budget ran out first. The phase is LEFT RUNNING in the background so that what it fetches
    lands in the cache; whatever it had produced so far is in the caller's `state`.

    Over MAX_BACKGROUND_PHASES the phase is awaited inline and cancelled at the budget instead - the
    pre-2026-10-03 behaviour - so a burst of slow lookups cannot grow without limit."""
    remaining = max(0.0, remaining_s)
    if len(_BACKGROUND) >= MAX_BACKGROUND_PHASES:
        try:
            await asyncio.wait_for(coro, timeout=remaining)
            return True
        except asyncio.TimeoutError:
            return False
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_reap)
    done, _pending = await asyncio.wait({task}, timeout=remaining)
    if task in done:
        task.result()
        return True
    return False


async def handle_find_business(
    request: FindBusinessRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
    *,
    include_osm: bool = True,
    input_notes: Optional[dict] = None,
    budget_s: Optional[float] = None,
) -> OutcomeReceipt:
    """`include_osm=False` is for our own code (the /demo route) that only
    wants the supply-network half: it must not send a made-up location to the
    public OpenStreetMap servers.

    `input_notes` is what core/find_business_input.prepare() interpreted or ignored
    ({"notes": [...], "ignored": [...], "location_normalized_from": {...}}); it is
    echoed in the result so a caller can see what the search actually used.

    `budget_s` can only SHORTEN the call budget (never widen it past CALL_BUDGET_S). It exists for our own
    health check (agent_interface/self_test.py), which must answer quickly however slow the public map
    servers are; a caller's request never sets it."""
    t0 = time.monotonic()
    directory = get_directory()

    # --- 1. the supply network: real, bookable rows only ----------------
    # include_demo=False is applied BEFORE the max_results cut, so five sample
    # rows with more channels cannot crowd a real one out of the answer.
    smbs = directory.search(
        vertical=request.vertical,
        zip_or_city=request.location.zip_or_city,
        capability=request.capability,
        max_usd=request.price_band.max_usd if request.price_band else None,
        max_results=request.max_results,
        include_demo=False,
    )
    smbs = [s for s in smbs if not getattr(s, "is_demo", False)]
    network_records = []
    for smb in smbs:
        rec = SMBRecord(
            smb_id=smb.smb_id,
            name=smb.name,
            vertical=smb.vertical,
            address=f"{smb.address}, {smb.city}, {smb.state} {smb.zip_code}",
            capabilities=smb.capabilities,
            channels_available=smb.channels_available,
            price_range=smb.price_range,
            verified_at=smb.verified_at,
            rank_score=round(len(smb.channels_available) / 3, 2),
            is_demo=False,
        ).model_dump()
        rec["source"] = "supply_network"
        network_records.append(rec)

    # --- 2. OpenStreetMap -------------------------------------------------
    plan = osm_categories.resolve(
        request.vertical, request.capability, getattr(request, "vertical_term", None))
    radius_m, radius_sent, radius_capped = _radius(request)
    search: dict = {
        "provider": "openstreetmap",
        "status": "ok",
        "category_match": {
            "basis": plan.basis,
            "osm_tags": plan.describe_tags(),
        },
        "radius_m": radius_m,
        "radius_miles_applied": radius_sent,
    }
    if plan.matched_term:
        search["category_match"]["matched_term"] = plan.matched_term
    if plan.name_phrase:
        search["category_match"]["name_contains"] = plan.name_phrase
    if radius_capped:
        search["radius_capped_at_miles"] = 25

    osm_records: list[dict] = []
    problem: Optional[dict] = None
    place: Optional[dict] = None
    state: dict = {}

    budget_s = CALL_BUDGET_S if budget_s is None else min(CALL_BUDGET_S, max(0.1, float(budget_s)))
    if not include_osm:
        search["status"] = "not_requested"
    else:
        client = get_client()
        guard = _guard_for(CALLER_KEY.get())
        try:
            finished = await _run_within_budget(
                _osm_phase(client, plan, request.location.zip_or_city, radius_m, guard, t0, state),
                budget_s - (time.monotonic() - t0))
            if not finished:
                if state.get("found") is None:
                    # The budget ran out before any search came back. This is NOT a failure and NOT
                    # "no businesses": the lookup is still running and will fill the cache.
                    problem = {"kind": "pending", "reason": "search_in_progress",
                               "retry_after_s": PENDING_RETRY_AFTER_S}
                    _log.info("find_business budget_exhausted place_resolved=%s", state.get("place") is not None)
                else:
                    state["narrow_stopped"] = "deadline_exceeded"
        except CallerRateLimited as exc:
            problem = {"kind": "rate_limited", "reason": "caller_rate_limited",
                       "retry_after_s": exc.retry_after_s}
        except OSMUnavailable as exc:
            problem = {"kind": "unavailable", "reason": exc.reason,
                       "retry_after_s": exc.retry_after_s or 30}
            _log.warning("find_business osm_unavailable reason=%s", exc.reason)
        except Exception as exc:  # noqa: BLE001 - a bug here must not read as "no businesses"
            problem = {"kind": "unavailable", "reason": "internal_error", "retry_after_s": 30}
            _log.exception("find_business osm_internal_error %s", type(exc).__name__)

        place = state.get("place")
        found = state.get("found")
        if problem is not None and problem["kind"] == "pending" and place is not None:
            # Say WHERE the pending search is looking, so a caller can tell a right place from a wrong one
            # before it waits.
            search["geocoded_place"] = {
                "display_name": place.get("display_name"),
                "latitude": place["latitude"],
                "longitude": place["longitude"],
            }
        if problem is None:
            if place is None:
                search["status"] = "location_not_found"
            else:
                search["geocoded_place"] = {
                    "display_name": place.get("display_name"),
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                }
                if state.get("country_codes"):
                    search["country_restriction"] = state["country_codes"]
        if problem is None and found is not None:
            effective_m = state["radius_m"]
            search["radius_requested_m"] = radius_m
            search["radius_m"] = effective_m
            search["radius_narrowed"] = effective_m < radius_m
            search["candidates_considered"] = found["candidates_considered"]
            search["candidates_capped"] = found["results_capped"]
            search["cached"] = bool(found.get("cached"))
            # "exact" = every match inside radius_m was examined, so the rows are
            # truly the nearest ones. Otherwise they are the nearest of a sample.
            search["distance_order"] = "nearest_of_examined" if found["results_capped"] else "exact"
            if state.get("narrow_stopped"):
                search["narrowing_stopped_because"] = state["narrow_stopped"]
            # A COPY: the client's cache hands the same dicts to every caller, and
            # downstream code (fencing, serialisation) must never edit the cache.
            osm_records = copy.deepcopy(found["businesses"])

    if problem is not None:
        search["status"] = {"rate_limited": "rate_limited", "pending": "pending"}.get(
            problem["kind"], "unavailable")
        search["reason"] = problem["reason"]
        search["retry_after_s"] = problem["retry_after_s"]
        if problem["kind"] == "pending":
            # Honest about WHAT is pending and what the caller can do. Nothing here says no business
            # exists, and nothing says OpenStreetMap is down: neither is known.
            search["continues_in_background"] = True
            search["repeat_this_call"] = (
                "The lookup keeps running on our side. Repeat this exact call after retry_after_s "
                "seconds: it is then usually answered from cache, instantly.")
    if include_osm:
        search["budget_s"] = budget_s
        cut_short = state.get("narrow_stopped") == "deadline_exceeded"
        search["within_budget"] = (problem is None or problem["kind"] != "pending") and not cut_short
        if cut_short and problem is None:
            # The budget ran out WHILE narrowing: the rows are the nearest of a larger sample, the lookup
            # keeps running, and a repeat is then usually answered from cache with the narrower result.
            search["continues_in_background"] = True
            search["repeat_this_call"] = (
                "The search was still narrowing when the time budget ran out, so these are the nearest of a "
                "larger sample. Repeat this exact call after retry_after_s seconds for the narrower answer, "
                "usually from cache.")
            search["retry_after_s"] = PENDING_RETRY_AFTER_S

    # --- 3. assemble ------------------------------------------------------
    room = max(0, request.max_results - len(network_records))
    osm_records = osm_records[:room]
    records = network_records + osm_records

    result: dict = {
        "businesses": records,
        # How many rows are in "businesses". Its own field so an empty answer is countable by a
        # reader (and a log) without parsing the list: the 2026-10-03 demand review could not measure
        # the empty-result rate because nothing recorded it.
        "result_count": len(records),
        # Real (non-sample) rows only. This used to count the [DEMO] rows too.
        "total_in_supply_network": directory.size_real(),
        "attribution": OSM_ATTRIBUTION,
        "attribution_url": OSM_COPYRIGHT_URL,
        "license": OSM_LICENSE,
        "search": search,
    }
    if input_notes:
        # What the request reader interpreted and ignored (core/find_business_input.py). A search
        # that quietly used something other than what was typed is the failure this exists to expose.
        if input_notes.get("notes"):
            result["input_notes"] = list(input_notes["notes"])[:8]
        if input_notes.get("ignored"):
            result["ignored_arguments"] = list(input_notes["ignored"])[:10]
        if input_notes.get("location_normalized_from"):
            result["location_normalized_from"] = dict(input_notes["location_normalized_from"])
    # A FILTER THAT DOES NOT FILTER MUST SAY SO.
    #
    # availability_window is advertised as an object with start_iso/end_iso and
    # is read by nothing: an identical five-result set came back for "no
    # window" and for a one-minute window in 1999. Until today it never even
    # reached the handler, so it was inert twice over. Forwarding it made it
    # VALIDATE - a malformed value now returns a clean -32602 - which reads to
    # an agent as support.
    #
    # Same treatment as screen_sanctions' country and entity_type notes:
    # accepted, not applied, and disclosed in the response rather than left
    # for the caller to discover by comparing result sets.
    if getattr(request, "availability_window", None) is not None:
        result["availability_window_applied"] = False
        result["availability_window_note"] = (
            "availability_window was accepted but did NOT narrow these "
            "results - we do not hold live calendars for the supply network, "
            "so we cannot filter on free/busy without calling each business. "
            "Use schedule_appointment with requested_time to book a specific "
            "slot; it checks real availability and refuses rather than "
            "booking a different time.")
    if request.price_band is not None and request.price_band.max_usd is not None:
        result["price_band_note"] = (
            "price_band.max_usd was applied to supply-network rows only. "
            "OpenStreetMap carries no prices, so OpenStreetMap results are "
            "NOT filtered by price.")
    area = _area_sentence(search)
    if osm_records:
        result["osm_notice"] = (
            "Records with source=openstreetmap are community-mapped listings, "
            "not entries in the AgentBroker supply network. We have not "
            "verified them; any field may be missing or out of date; their "
            "smb_id (osm:...) cannot be used with verify_business, "
            "schedule_appointment or call_business. Contact the business "
            "through the phone or website when one is listed."
            + (" " + area if area else ""))

    empty = not records
    if empty or problem is not None:
        result["supply_coverage_note"] = _coverage_note(request, search, problem, empty)

    next_actions: list[str] = []
    if problem is not None and problem["kind"] == "pending":
        next_actions.append(
            f"Repeat this exact call in about {problem['retry_after_s']} seconds: the lookup is still "
            "running and the repeat is answered from cache")
    elif problem is not None:
        next_actions.append(f"Retry in about {problem['retry_after_s']} seconds")
    elif empty:
        next_actions += [
            "Try a simpler capability such as 'plumber', 'dentist', 'restaurant' or 'lawyer'",
            "Name a larger nearby town, or widen location.radius_miles (maximum 25)",
            "Or call import_booking_url if the user gave you a specific booking URL",
        ]
    if osm_records:
        next_actions.append(
            "Contact OpenStreetMap results through the listed phone or website; "
            "they cannot be booked with schedule_appointment")
    if search.get("candidates_capped"):
        next_actions.append(
            "More matches exist than were examined: set a smaller location.radius_miles "
            "or a more specific capability for a closer look")

    if records:
        increment_businesses_found(len(records))

    if problem is not None and problem["kind"] == "pending":
        # Not a failure: nothing went wrong, the answer is not ready yet. PARTIAL keeps `isError` false
        # on the MCP result, so a client does not treat "still working" as a broken tool.
        status = OperationStatus.PARTIAL
        reason_code = "search_in_progress"
        message = _pending_message(problem, len(records), place, budget_s)
        retriable = True
    elif problem is not None:
        status = OperationStatus.PARTIAL if records else OperationStatus.FAILURE
        reason_code = ("rate_limited" if problem["kind"] == "rate_limited"
                       else "osm_temporarily_unavailable")
        message = _problem_message(problem, len(records))
        retriable = True
    else:
        status = OperationStatus.SUCCESS
        retriable = False
        # "no_results" also covers an unresolvable location: it is a declared
        # failure mode in the manifest, and search.status says which it was.
        reason_code = "businesses_found" if records else "no_results"
        message = _ok_message(len(network_records), len(osm_records), search,
                              place, state.get("country_codes"), area)

    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=status,
        reason_code=reason_code,
        human_message=message,
        result=result,
        cost=CostRecord(amount=_receipt_usd("find_business"), currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        retriable=retriable,
        trace_id=trace_id,
        next_actions=next_actions,
    )


def _area_sentence(search: dict) -> str:
    """What the caller must know about HOW MUCH of the area was really looked
    at. Empty when the search was complete over the area asked for."""
    if search.get("status") != "ok" or "candidates_capped" not in search:
        return ""
    parts: list[str] = []
    if search.get("radius_narrowed"):
        parts.append(
            f"The requested {_km(search['radius_requested_m'])} area holds more than "
            f"{CANDIDATE_LIMIT} matches, so the search was narrowed to {_km(search['radius_m'])} "
            "around the place")
        if not search["candidates_capped"]:
            parts[-1] += (f"; these are all matches within {_km(search['radius_m'])}, nearest first, "
                          "and more exist farther out")
    if search["candidates_capped"]:
        parts.append(
            f"More matches exist than the {CANDIDATE_LIMIT} candidates examined: rows are sorted by "
            "distance but are the nearest among those examined, not guaranteed nearest overall. "
            "A smaller radius_miles gives a closer look")
        if search.get("narrowing_stopped_because"):
            parts[-1] += f" (narrowing stopped: {search['narrowing_stopped_because']})"
    return ". ".join(parts) + "." if parts else ""


def _place_sentence(place: Optional[dict], country_codes: Optional[str]) -> str:
    """Say WHERE the search ran. The place name is text from a public map, so it
    goes through the same fence as every other third-party string."""
    if not place or not place.get("display_name"):
        return ""
    s = f" Searched near: {_fence(place['display_name'])}."
    if country_codes == "us":
        s += (" A bare 5-digit ZIP is read as a US ZIP code; for a postal code "
              "elsewhere, add the country.")
    return s


def _ok_message(n_network: int, n_osm: int, search: dict, place: Optional[dict],
                country_codes: Optional[str], area: str) -> str:
    total = n_network + n_osm
    osm_status = search["status"]
    if total == 0:
        if osm_status == "not_requested":
            return ("No matching businesses in our supply network (OpenStreetMap was not "
                    "searched for this call).")
        if osm_status == "location_not_found":
            extra = (" A bare 5-digit ZIP is read as a US ZIP code; for a postal code "
                     "elsewhere, add the country." if country_codes == "us" else "")
            return ("No businesses returned: the location could not be resolved to a "
                    "place in OpenStreetMap. Try a city or town name, optionally with "
                    "the country." + extra)
        return ("No matching businesses found in OpenStreetMap within the search radius, "
                "and none in our supply network. OpenStreetMap coverage varies by region, "
                "so this is not evidence that none exist." + _place_sentence(place, country_codes)
                + (" " + area if area else "") + " Data © OpenStreetMap contributors.")
    parts = []
    if n_network:
        parts.append(f"{n_network} from the AgentBroker supply network (added via a booking URL)")
    if n_osm:
        parts.append(f"{n_osm} from OpenStreetMap")
    return (f"Found {total} business(es): " + " and ".join(parts) + "."
            + _place_sentence(place, country_codes)
            + (" " + area if area else "")
            + " OpenStreetMap listings are community-mapped, unverified by us and not "
            "bookable through schedule_appointment; details may be missing or out of "
            "date. Data © OpenStreetMap contributors (ODbL).")


def _pending_message(problem: dict, n_records: int, place: Optional[dict], budget_s: float) -> str:
    where = (f" Resolved the place as {_fence(place['display_name'])}." if place and place.get("display_name")
             else " The place has not been resolved yet.")
    have = (f" Returning {n_records} supply-network row(s) found so far." if n_records
            else " No businesses are returned yet.")
    return (f"STILL SEARCHING OpenStreetMap after {budget_s:g} s (the public map servers are slow for this "
            f"place right now).{where}{have} This is NOT evidence that no matching businesses exist, and "
            "OpenStreetMap is not known to be down. The lookup keeps running on our side: repeat this exact "
            f"call in about {problem['retry_after_s']} seconds and it is usually answered from cache. "
            "Data © OpenStreetMap contributors.")


def _problem_message(problem: dict, n_records: int) -> str:
    if problem["kind"] == "rate_limited":
        head = ("OpenStreetMap lookup NOT performed: you have made too many fresh "
                "lookups in a short time (repeat queries are answered from cache and "
                "do not count).")
    else:
        head = ("OpenStreetMap is TEMPORARILY UNAVAILABLE for this lookup "
                f"(reason: {problem['reason']}).")
    tail = (f" Returning {n_records} supply-network row(s) only." if n_records
            else " No businesses are returned.")
    return (head + tail + " This is NOT evidence that no matching businesses exist. "
            f"Retry in about {problem['retry_after_s']} seconds. "
            "Data © OpenStreetMap contributors.")


def _coverage_note(request: FindBusinessRequest, search: dict, problem: Optional[dict],
                   empty: bool) -> str:
    where = request.location.zip_or_city[:80]
    if problem is not None and problem["kind"] == "pending":
        return ("The OpenStreetMap half of this search had not finished when the time budget ran out "
                "(see search.status = pending). The list above is incomplete; repeat the call shortly "
                f"before concluding anything about {where}.")
    if problem is not None:
        return ("The OpenStreetMap half of this search did not run (see search.reason). "
                "The list above is incomplete; retry before concluding anything about "
                f"{where}.")
    if search["status"] == "not_requested":
        return "Only the AgentBroker supply network was searched for this call."
    if search["status"] == "location_not_found":
        return (f"OpenStreetMap could not resolve '{where}' to a place. Try a city or town "
                "name, optionally with the country (for example 'Nizwa, Oman').")
    return (f"No verified or mapped businesses matched near '{where}'. OpenStreetMap coverage "
            "varies by region: it is dense in most cities and thinner elsewhere, and small "
            "businesses are often unmapped. The category was matched on "
            f"{search['category_match']['basis'].replace('_', ' ')}; a simpler capability "
            "(e.g. 'plumber', 'dentist') or a wider radius_miles may help.")
