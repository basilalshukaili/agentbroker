"""
find_business — core operation handler.

Returns REAL businesses near a place, from two sources, each labelled:

  * OpenStreetMap (primary). Nominatim resolves `location.zip_or_city` to a
    point; Overpass returns mapped shops, crafts, offices and amenities of the
    requested kind within a radius, nearest first. Fields are only what the
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
"""
from __future__ import annotations

import copy
import logging
import time
import uuid
from typing import Optional

from core.caller_context import CALLER_KEY
from core.models import (
    FindBusinessRequest, OutcomeReceipt, OperationStatus, SMBRecord, CostRecord
)
from supply import osm_categories
from supply.osm_client import (
    CALLER_LIMITER, CallerRateLimited, OSMUnavailable, RADIUS_DEFAULT_M,
    RADIUS_MAX_M, get_client,
)
from supply.osm_places import OSM_ATTRIBUTION, OSM_COPYRIGHT_URL, OSM_LICENSE
from supply.smb_directory import get_directory
from telemetry.metrics import increment_businesses_found
from billing.pricing import receipt_usd as _receipt_usd

_log = logging.getLogger("smb_broker.find_business")

_METERS_PER_MILE = 1609.344
_MIN_RADIUS_M = 200


def _radius(request: FindBusinessRequest) -> tuple[int, bool, bool]:
    """(radius_m, caller_supplied_it, was_capped)."""
    miles = request.location.radius_miles
    if miles is None:
        return RADIUS_DEFAULT_M, False, False
    wanted = int(round(float(miles) * _METERS_PER_MILE))
    capped = wanted > RADIUS_MAX_M
    return max(_MIN_RADIUS_M, min(wanted, RADIUS_MAX_M)), True, capped


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


async def handle_find_business(
    request: FindBusinessRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
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

    client = get_client()
    guard = _guard_for(CALLER_KEY.get())
    try:
        place = await client.geocode(request.location.zip_or_city, guard=guard)
        if place is None:
            search["status"] = "location_not_found"
        else:
            search["geocoded_place"] = {
                "display_name": place.get("display_name"),
                "latitude": place["latitude"],
                "longitude": place["longitude"],
            }
            found = await client.search(plan, place["latitude"], place["longitude"],
                                        radius_m, guard=guard)
            search["candidates_considered"] = found["candidates_considered"]
            search["candidates_capped"] = found["results_capped"]
            search["cached"] = bool(found.get("cached"))
            # A COPY: the client's cache hands the same dicts to every caller, and
            # downstream code (fencing, serialisation) must never edit the cache.
            osm_records = copy.deepcopy(found["businesses"])
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

    if problem is not None:
        search["status"] = "rate_limited" if problem["kind"] == "rate_limited" else "unavailable"
        search["reason"] = problem["reason"]
        search["retry_after_s"] = problem["retry_after_s"]

    # --- 3. assemble ------------------------------------------------------
    room = max(0, request.max_results - len(network_records))
    osm_records = osm_records[:room]
    records = network_records + osm_records

    result: dict = {
        "businesses": records,
        # Real (non-sample) rows only. This used to count the [DEMO] rows too.
        "total_in_supply_network": directory.size_real(),
        "attribution": OSM_ATTRIBUTION,
        "attribution_url": OSM_COPYRIGHT_URL,
        "license": OSM_LICENSE,
        "search": search,
    }
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
    if osm_records:
        result["osm_notice"] = (
            "Records with source=openstreetmap are community-mapped listings, "
            "not entries in the AgentBroker supply network. We have not "
            "verified them; any field may be missing or out of date; their "
            "smb_id (osm:...) cannot be used with verify_business, "
            "schedule_appointment or call_business. Contact the business "
            "through the phone or website when one is listed.")

    empty = not records
    if empty or problem is not None:
        result["supply_coverage_note"] = _coverage_note(request, search, problem, empty)

    next_actions: list[str] = []
    if problem is not None:
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

    if records:
        increment_businesses_found(len(records))

    if problem is not None:
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
        message = _ok_message(len(network_records), len(osm_records), search["status"])

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


def _ok_message(n_network: int, n_osm: int, osm_status: str) -> str:
    total = n_network + n_osm
    if total == 0:
        if osm_status == "location_not_found":
            return ("No businesses returned: the location could not be resolved to a "
                    "place in OpenStreetMap. Try a city or town name, optionally with "
                    "the country.")
        return ("No matching businesses found in OpenStreetMap within the search radius, "
                "and none in our supply network. OpenStreetMap coverage varies by region, "
                "so this is not evidence that none exist. Data \u00a9 OpenStreetMap contributors.")
    parts = []
    if n_network:
        parts.append(f"{n_network} from the AgentBroker supply network (added via a booking URL)")
    if n_osm:
        parts.append(f"{n_osm} from OpenStreetMap")
    return (f"Found {total} business(es): " + " and ".join(parts) + ". "
            "OpenStreetMap listings are community-mapped, unverified by us and not "
            "bookable through schedule_appointment; details may be missing or out of "
            "date. Data \u00a9 OpenStreetMap contributors (ODbL).")


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
            "Data \u00a9 OpenStreetMap contributors.")


def _coverage_note(request: FindBusinessRequest, search: dict, problem: Optional[dict],
                   empty: bool) -> str:
    where = request.location.zip_or_city[:80]
    if problem is not None:
        return ("The OpenStreetMap half of this search did not run (see search.reason). "
                "The list above is incomplete; retry before concluding anything about "
                f"{where}.")
    if search["status"] == "location_not_found":
        return (f"OpenStreetMap could not resolve '{where}' to a place. Try a city or town "
                "name, optionally with the country (for example 'Nizwa, Oman').")
    return (f"No verified or mapped businesses matched near '{where}'. OpenStreetMap coverage "
            "varies by region: it is dense in most cities and thinner elsewhere, and small "
            "businesses are often unmapped. The category was matched on "
            f"{search['category_match']['basis'].replace('_', ' ')}; a simpler capability "
            "(e.g. 'plumber', 'dentist') or a wider radius_miles may help.")
