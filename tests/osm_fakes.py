"""
An offline OpenStreetMap for tests.

find_business talks to two public servers (Nominatim, Overpass). Tests must
never touch them - not because it would break, but because it would be rude,
slow and non-deterministic. This module provides:

  * FakeClock        - a clock that sleeping advances, so the client's 1 req/s
                       spacing and TTLs are exercised instantly and exactly.
  * make_transport   - an httpx.MockTransport that behaves like a tiny OSM:
                       Nominatim resolves a few place names; Overpass evaluates
                       the tag selectors, name filter and `around` radius of the
                       query it is sent against a fixed dataset. Because it
                       reads the real query text, a wrong query returns wrong
                       (or no) rows and the test fails - the fake cannot agree
                       with a broken query by accident.
  * make_client      - an OSMClient wired to the above.
  * DATASET          - the features it knows, in Atlanta.

Nothing here is imported by production code.
"""
from __future__ import annotations

import inspect
import re
from typing import Callable, Optional

import httpx

from supply.osm_client import OSMClient
from supply.osm_places import haversine_m

ATLANTA = (33.7490, -84.3880)
MUSCAT = (23.5880, 58.3829)

PLACES = {
    "atlanta": {"lat": "33.7490", "lon": "-84.3880",
                "display_name": "Atlanta, Fulton County, Georgia, United States",
                "osm_type": "relation", "osm_id": 119557},
    "30309": {"lat": "33.7995", "lon": "-84.3870",
              "display_name": "30309, Atlanta, Fulton County, Georgia, United States",
              "osm_type": "relation", "osm_id": 5555},
    "boston": {"lat": "42.3554", "lon": "-71.0605",
               "display_name": "Boston, Suffolk County, Massachusetts, United States",
               "osm_type": "relation", "osm_id": 2315704},
    "austin": {"lat": "30.2672", "lon": "-97.7431",
               "display_name": "Austin, Travis County, Texas, United States",
               "osm_type": "relation", "osm_id": 113314},
    "muscat": {"lat": "23.5880", "lon": "58.3829",
               "display_name": "Muscat, Oman", "osm_type": "relation", "osm_id": 3000},
}


def node(id_, name, lat, lon, **tags):
    t = dict(tags)
    if name is not None:
        t["name"] = name
    return {"type": "node", "id": id_, "lat": lat, "lon": lon, "tags": t}


def way(id_, name, lat, lon, **tags):
    t = dict(tags)
    if name is not None:
        t["name"] = name
    return {"type": "way", "id": id_, "center": {"lat": lat, "lon": lon}, "tags": t}


DATASET = [
    node(1001, "Peachtree Cuts", 33.7495, -84.3885, shop="hairdresser",
         phone="+1 404 555 0101", website="https://peachtreecuts.example.org",
         opening_hours="Mo-Sa 09:00-18:00", **{
             "addr:housenumber": "12", "addr:street": "Peachtree St",
             "addr:city": "Atlanta", "addr:state": "GA", "addr:postcode": "30303"}),
    node(1002, "Midtown Barber", 33.7800, -84.3830, shop="barber"),
    node(1003, "Southside Plumbing Co", 33.7300, -84.4000, craft="plumber",
         **{"contact:phone": "+1 404 555 0303"}),
    way(2001, "Hartsfield Law Group", 33.7550, -84.3900, office="lawyer",
        **{"addr:street": "Marietta St", "addr:city": "Atlanta"}),
    node(1004, "Downtown Pharmacy", 33.7500, -84.3890, amenity="pharmacy"),
    node(1005, "Oak Table", 33.7520, -84.3860, amenity="restaurant", cuisine="american"),
    node(1006, None, 33.7491, -84.3881, shop="hairdresser"),        # unnamed: never a result
    node(1007, "Glow Nails & Beauty", 33.7510, -84.3870, shop="beauty"),
    # same shop mapped twice (point + building outline): must collapse to one row
    node(1008, "Twin Cuts", 33.7493, -84.3879, shop="hairdresser"),
    way(2002, "Twin Cuts", 33.7493, -84.3879, shop="hairdresser"),
    # far away: only reachable with a big radius
    node(1009, "Far Cuts", 33.9500, -84.3880, shop="hairdresser"),
    node(3001, "Al Noor Barber", 23.5890, 58.3830, shop="barber"),
]


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds


_COND_RE = re.compile(r'\["([^"]+)"(?:~"([^"]*)"(,i)?)?\]')
_AROUND_RE = re.compile(r'\(around:(\d+),(-?[\d.]+),(-?[\d.]+)\)')


def _matches_line(el: dict, line: str) -> bool:
    tags = el.get("tags", {})
    for key, regex, ci in _COND_RE.findall(line):
        if key not in tags:
            return False
        if regex:
            flags = re.IGNORECASE if ci else 0
            if not re.search(regex, tags[key], flags):
                return False
    m = _AROUND_RE.search(line)
    if m:
        radius, lat, lon = int(m.group(1)), float(m.group(2)), float(m.group(3))
        if el["type"] == "node":
            elat, elon = el["lat"], el["lon"]
        else:
            elat, elon = el["center"]["lat"], el["center"]["lon"]
        if haversine_m(lat, lon, elat, elon) > radius:
            return False
    return True


def overpass_answer(query: str, dataset: list[dict]) -> dict:
    """Evaluate the union body of an Overpass query against `dataset`."""
    limit_m = re.search(r"out center tags (\d+);", query)
    limit = int(limit_m.group(1)) if limit_m else 100
    lines = [ln.strip() for ln in query.splitlines() if ln.strip().startswith("nwr")]
    out, seen = [], set()
    for el in dataset:
        if any(_matches_line(el, ln) for ln in lines):
            k = (el["type"], el["id"])
            if k not in seen:
                seen.add(k)
                out.append(el)
    return {"version": 0.6, "generator": "fake", "elements": out[:limit]}


def make_transport(
    *,
    dataset: Optional[list[dict]] = None,
    places: Optional[dict] = None,
    log: Optional[list] = None,
    nominatim_override: Optional[Callable[[httpx.Request], httpx.Response]] = None,
    overpass_override: Optional[Callable[[httpx.Request], httpx.Response]] = None,
) -> httpx.MockTransport:
    data = DATASET if dataset is None else dataset
    known = PLACES if places is None else places

    async def _maybe(result):
        return await result if inspect.isawaitable(result) else result

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if log is not None:
            log.append(request)
        if "nominatim" in host:
            if nominatim_override:
                return await _maybe(nominatim_override(request))
            q = request.url.params.get("q", "").casefold()
            for name, rec in known.items():
                if name in q:
                    return httpx.Response(200, json=[rec])
            return httpx.Response(200, json=[])
        if "overpass" in host:
            if overpass_override:
                return await _maybe(overpass_override(request))
            form = dict(httpx.QueryParams(request.content.decode("utf-8")))
            return httpx.Response(200, json=overpass_answer(form.get("data", ""), data))
        return httpx.Response(404, json={"error": "unexpected host " + host})

    return httpx.MockTransport(handler)


def make_client(**kwargs) -> tuple[OSMClient, FakeClock, list]:
    """(client, clock, request_log) wired to the fake OSM."""
    clock = FakeClock()
    log: list = []
    transport = make_transport(log=log, **kwargs)
    client = OSMClient(transport=transport, clock=clock, sleep=clock.sleep)
    return client, clock, log

