"""
Build the Overpass query and turn OpenStreetMap elements into business records.

Pure functions, no network and no state, so every rule here is unit-testable
offline. The HTTP, caching and rate limiting live in supply/osm_client.py.

WHAT A RECORD MAY CONTAIN
=========================
Only what OpenStreetMap actually holds for the feature, plus two values we
compute from coordinates we were given (`distance_m`, and the OSM url built
from the element's own type and id). A tag that is not on the feature is ABSENT
from the record - never null, never a placeholder, never a guess. In
particular we do not invent phone numbers, hours, ratings or opening status,
and we do not claim a business is open, verified or bookable.

Every string here was typed into a public wiki-style map by a stranger. It is
fenced as untrusted by core/untrusted.py before it reaches a caller.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from supply.osm_categories import CategoryPlan, NAME_FALLBACK_KEYS

OSM_ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
OSM_LICENSE = "Open Database License (ODbL) 1.0"

# Longest string we keep per field. The untrusted fence truncates at its own
# limit too; clamping here keeps a hostile 5 MB tag out of the cache.
_MAX_NAME = 200
_MAX_PHONE = 60
_MAX_WEBSITE = 300
_MAX_HOURS = 300
_MAX_ADDR_PART = 120

# Overpass `out ... N` limit: how many raw candidates we ask for.
CANDIDATE_LIMIT = 150
# How many we keep (nearest first) after sorting, per query, in the cache.
KEEP_NEAREST = 60

# The tag families we report as `category`, in priority order.
_CATEGORY_KEYS = ("shop", "amenity", "craft", "office", "healthcare", "leisure")


def _regex_selector(key: str, values: str) -> str:
    # `values` is a fixed alternation from supply/osm_categories.py
    # ([a-z_|] only, validated at import), never caller text.
    return f'["{key}"~"^({values})$"]'


def build_query(plan: CategoryPlan, lat: float, lon: float, radius_m: int,
                *, server_timeout_s: int = 18, limit: int = CANDIDATE_LIMIT) -> str:
    """Overpass QL for `plan` within `radius_m` of (lat, lon).

    lat/lon/radius are coerced to numbers here, so a caller string cannot reach
    the query through them either.
    """
    la = float(lat)
    lo = float(lon)
    r = int(radius_m)
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        raise ValueError("coordinates out of range")
    if r < 1:
        raise ValueError("radius must be positive")
    around = f"(around:{r},{la:.6f},{lo:.6f})"

    name_filter = ""
    if plan.name_phrase:
        # sanitise_name_phrase() already removed every regex/QL metacharacter.
        name_filter = f'["name"~"{plan.name_phrase}",i]'

    lines: list[str] = []
    if plan.selectors:
        for sel in plan.selectors:
            conds = "".join(_regex_selector(k, v) for k, v in sel)
            lines.append(f'  nwr{conds}["name"]{name_filter}{around};')
    else:
        # name-match fallback: any business-like feature whose name contains the phrase
        for key in NAME_FALLBACK_KEYS:
            lines.append(f'  nwr["{key}"]{name_filter}{around};')
    body = "\n".join(lines)
    return (
        f"[out:json][timeout:{int(server_timeout_s)}];\n"
        f"(\n{body}\n);\n"
        f"out center tags {int(limit)};"
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _clean(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = " ".join(value.split())
    if not v:
        return None
    return v[:limit]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _address_parts(tags: dict) -> dict:
    mapping = (
        ("housenumber", "addr:housenumber"),
        ("street", "addr:street"),
        ("suburb", "addr:suburb"),
        ("city", "addr:city"),
        ("state", "addr:state"),
        ("postcode", "addr:postcode"),
        ("country", "addr:country"),
    )
    parts: dict = {}
    for out_key, tag in mapping:
        v = _clean(tags.get(tag), _MAX_ADDR_PART)
        if v:
            parts[out_key] = v
    return parts


def _address_line(parts: dict) -> Optional[str]:
    """One display line built ONLY from parts that exist. None when there is
    not enough to be an address (a bare house number is not one)."""
    if not (parts.get("street") or parts.get("city") or parts.get("postcode")):
        return None
    street = " ".join(x for x in (parts.get("housenumber"), parts.get("street")) if x)
    region = " ".join(x for x in (parts.get("state"), parts.get("postcode")) if x)
    pieces = [p for p in (street, parts.get("suburb"), parts.get("city"), region,
                          parts.get("country")) if p]
    return ", ".join(pieces) or None


_TOKEN_RE = re.compile(r"^[a-z0-9_;]{1,60}$")


def _token(value: Any) -> Optional[str]:
    """A tag value only if it has the shape of an OSM enum value: lowercase
    letters, digits, underscore, semicolon, no spaces. Such a string cannot
    carry a sentence, so `category` needs no untrusted fence; anything else
    ("SYSTEM: ignore ...") is dropped rather than passed on."""
    return value if isinstance(value, str) and _TOKEN_RE.match(value) else None


def _category(tags: dict, plan: CategoryPlan) -> Optional[str]:
    """`key=value` for the tag that made this feature match, else the first
    business-like tag it carries. Absent when it carries none - or when the
    value is not a plain enum-shaped token."""
    for sel in plan.selectors:
        for k, _v in sel:
            val = _token(tags.get(k))
            if val:
                return f"{k}={val}"
    for k in _CATEGORY_KEYS:
        val = _token(tags.get(k))
        if val:
            return f"{k}={val}"
    return None


def parse_elements(elements: Any, plan: CategoryPlan, center_lat: float, center_lon: float,
                   *, keep: int = KEEP_NEAREST) -> list[dict]:
    """Overpass `elements` -> business dicts, nearest first, de-duplicated.

    A feature with no name, no usable coordinates or an unknown element type is
    skipped: it is not a business we can present.
    """
    if not isinstance(elements, list):
        return []
    out: list[dict] = []
    seen: set = set()
    for el in elements:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        eid = el.get("id")
        if etype not in ("node", "way", "relation") or isinstance(eid, bool) or not isinstance(eid, int):
            continue
        tags = el.get("tags")
        if not isinstance(tags, dict):
            continue
        name = _clean(tags.get("name"), _MAX_NAME)
        if not name:
            continue
        if etype == "node":
            lat, lon = _num(el.get("lat")), _num(el.get("lon"))
        else:
            c = el.get("center")
            lat = _num(c.get("lat")) if isinstance(c, dict) else None
            lon = _num(c.get("lon")) if isinstance(c, dict) else None
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        # The same shop is often mapped twice (a point AND its building outline).
        dedupe = (name.casefold(), round(lat, 4), round(lon, 4))
        if dedupe in seen:
            continue
        seen.add(dedupe)

        rec: dict = {
            "smb_id": f"osm:{etype}/{eid}",
            "name": name,
            "source": "openstreetmap",
        }
        cat = _category(tags, plan)
        if cat:
            rec["category"] = cat
        parts = _address_parts(tags)
        if parts:
            rec["address_parts"] = parts
        line = _address_line(parts)
        if line:
            rec["address"] = line
        phone = _clean(tags.get("phone") or tags.get("contact:phone"), _MAX_PHONE)
        if phone:
            rec["phone"] = phone
        website = _clean(tags.get("website") or tags.get("contact:website") or tags.get("url"),
                         _MAX_WEBSITE)
        if website:
            rec["website"] = website
        hours = _clean(tags.get("opening_hours"), _MAX_HOURS)
        if hours:
            rec["opening_hours"] = hours
        rec["latitude"] = round(lat, 6)
        rec["longitude"] = round(lon, 6)
        rec["distance_m"] = int(round(haversine_m(center_lat, center_lon, lat, lon)))
        rec["osm"] = {
            "type": etype,
            "id": eid,
            "url": f"https://www.openstreetmap.org/{etype}/{eid}",
        }
        out.append(rec)
    out.sort(key=lambda r: (r["distance_m"], r["name"].casefold(), r["smb_id"]))
    return out[:keep]
