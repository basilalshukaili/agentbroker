"""
find_business input: read the ways callers really write the request, and explain the ones we cannot.

WHY THIS FILE EXISTS. In the 47 hours to 2026-10-03, 21 of the 26 external find_business calls failed
before any search ran: 11 sent no arguments, 10 sent a `location` and a `vertical` that the strict
schema rejected (docs/reviews/2026-10-03-mcp-demand-evidence.md, section 4). It was the most-called
tool outsiders touched, and the failures taught the caller nothing: the answer named a field and
nothing else, as a JSON-RPC error many clients never show to the model.

Probing the live server the same day showed worse than a hard refusal:

  * `{"city": "Muscat", "vertical": "restaurant"}` - the shape the published schema itself advertises
    ("Alternative to location: a top-level city ... The receipt reports location_normalized_from") -
    was silently answered for ATLANTA. The dispatcher defaulted a missing `location` to Atlanta and
    nothing implemented `city`. A confident, wrong, successful answer is the worst outcome a search
    tool has, so the default is gone and `city`/`region` now do what the schema says.
  * `"location": "Muscat, Oman"` (a bare string, the most natural thing to type) was refused.
  * `"vertical": "restaurants"` / "cafe" / "clinic" were refused although the category table knows
    every one of them.

THE RULES THIS APPLIES
  1. Be liberal in what is READ: a place may be a string, a `location` object, or top-level
     `city`/`region`/`country`; a kind of business may be `capability`, or a real kind sent in the
     `vertical` slot, or a common synonym key.
  2. Never guess WHERE. A missing place is an error with a worked example - not a default city.
  3. Never guess silently. Everything that was interpreted is listed in `input_notes`, everything
     that was ignored in `ignored_arguments`, so a caller can see what the search actually used.
  4. A refusal must teach: what was wrong, in the caller's own terms, plus an example that
     is VALID (a test runs every example this module can emit through the real pipeline).

Pure functions, no network and no state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from supply import osm_categories

MACRO_VERTICALS = ("personal_services", "home_services", "professional_services")

DEFAULT_EXAMPLE_PLACE = "Muscat, Oman"
DEFAULT_EXAMPLE_KIND = "dentist"

# Kinds of business a caller can name, shown in error text. Every entry must resolve through the
# category table (tests/unit/test_find_business_input.py) - an example that falls back to a name
# match would teach the wrong thing.
SUGGESTED_KINDS = ("restaurant", "cafe", "pharmacy", "dentist", "doctor", "hairdresser", "gym",
                   "plumber", "electrician", "lawyer", "accountant", "mechanic")

# Other spellings of "what kind of business" that agents send in place of `capability`.
_KIND_ALIASES = ("category", "type", "business_type", "kind", "service", "keyword", "term", "what", "query")

# A place, in order of how specific it is. Used both for top-level arguments and inside `location`.
_PLACE_KEYS = ("zip_or_city", "address", "place", "near", "where", "city", "town", "zip", "zip_code",
               "postal_code", "postcode")
_INNER_ONLY_PLACE_KEYS = ("name", "query")
_REGION_KEYS = ("state", "region", "province")
_COUNTRY_KEYS = ("country",)
_COORD_KEYS = ("latitude", "longitude", "lat", "lon", "lng")
_RADIUS_KEYS = ("radius_miles", "radius_km")

_KNOWN_TOP_LEVEL = frozenset({
    "vertical", "location", "capability", "price_band", "availability_window", "max_results",
    "idempotency_key",
}) | frozenset(_KIND_ALIASES) | frozenset(_PLACE_KEYS) | frozenset(_REGION_KEYS) | frozenset(_COUNTRY_KEYS)

# Every top-level argument name this module reads (or knowingly drops). scripts/check_params_reach_dispatch.py
# credits the find_business dispatch branch with exactly this set, because the branch hands `args` whole to
# prepare() instead of reading keys one by one - and tests/unit/test_every_param_actually_arrives.py is the
# half that proves each one really arrives.
ARGUMENTS_READ = _KNOWN_TOP_LEVEL

_ECHO_PUNCTUATION = " _.,'&/-"
_KM_PER_MILE = 1.609344


def _echo(value: Any, limit: int = 80) -> str:
    """Caller text that is quoted back to the caller (and may be read by a log or another agent):
    reduced to letters, digits (any script - a place may be written in Arabic) and plain punctuation,
    and shortened, so a hostile argument name or value cannot carry markup or a line break through our
    own error text."""
    return "".join(ch for ch in str(value) if ch.isalnum() or ch in _ECHO_PUNCTUATION)[:limit].strip()


class FindBusinessInputError(Exception):
    """The request cannot be searched as sent. `error_code` is `missing_argument` or
    `invalid_argument`; `how_to_resolve` carries a worked example and the accepted forms."""

    def __init__(self, error_code: str, message: str, how_to_resolve: dict):
        super().__init__(message)
        self.error_code = error_code
        self.how_to_resolve = how_to_resolve


@dataclass
class Prepared:
    """A request ready for FindBusinessRequest(**kwargs), plus what was interpreted on the way."""
    kwargs: dict
    notes: list = field(default_factory=list)              # short sentences, shown to the caller
    ignored: list = field(default_factory=list)            # argument names that were not used
    location_normalized_from: Optional[dict] = None        # top-level parts assembled into a place


# --------------------------------------------------------------------------- vertical / kind

def interpret_vertical(raw: str) -> tuple[Optional[str], Optional[str], bool]:
    """(macro_vertical, capability_hint, recognised) for the text sent as `vertical`.

    recognised=False means the word is neither a macro vertical, a synonym we alias, nor a kind in
    the category table: the caller named something we do not map, and the right response is to
    search for it by name (and say so), not to refuse it."""
    from core.models import alias_vertical            # lazy: models imports this module lazily too

    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in MACRO_VERTICALS:
        return key, None, True
    macro, hint = alias_vertical(raw)
    if macro in MACRO_VERTICALS:
        return macro, hint, True
    fam = osm_categories.family_of(raw)
    if fam:
        return fam, None, True
    return None, None, False


# --------------------------------------------------------------------------- examples & errors

def example_arguments(place: Optional[str] = None, kind: Optional[str] = None) -> dict:
    """A request that is valid today. Built from what the caller sent when that is usable, so the
    example reads like THEIR request with the missing piece filled in."""
    # The table KEY the caller's words resolved to, never their raw text: a sentence that merely contains
    # the word "dentist" must not be echoed back as the example.
    kind = osm_categories.known_term(kind) or DEFAULT_EXAMPLE_KIND
    ex: dict = {"location": {"zip_or_city": _echo(place or DEFAULT_EXAMPLE_PLACE) or DEFAULT_EXAMPLE_PLACE},
                "capability": kind, "max_results": 5}
    fam = osm_categories.family_of(kind)
    if fam:
        ex = {"vertical": fam, **ex}
    return ex


def _accepted() -> dict:
    return {
        "location": ("a place name as a string, e.g. 'Muscat, Oman', or an object "
                     "{\"zip_or_city\": \"Muscat, Oman\", \"radius_miles\": 3}; top-level `city` "
                     "(with optional `region` and `country`) is accepted too"),
        "capability": ("the kind of business: " + ", ".join(SUGGESTED_KINDS)
                       + " and many more; any other word is matched against business names"),
        "vertical": "optional: personal_services | home_services | professional_services",
        "max_results": "1 to 20 (default 5)",
        "radius_miles": "0 to 25 (default about 3.1), inside `location`",
    }


def _how(example: dict) -> dict:
    return {"example_arguments": example, "accepted": _accepted(),
            "hint": "send the example's shape with your own place and kind of business"}


def _inline(example: dict) -> str:
    import json
    return json.dumps(example, separators=(",", ":"))


def _error(code: str, what: str, args: dict, *, place: Optional[str] = None, kind: Optional[str] = None
           ) -> FindBusinessInputError:
    example = example_arguments(place, kind)
    msg = f"find_business did not run: {what} Example that works: {_inline(example)}"
    return FindBusinessInputError(code, msg, _how(example))


# --------------------------------------------------------------------------- place

def _text(value: Any) -> Optional[str]:
    """A non-empty string for a place part, accepting a bare number (a ZIP typed as 30309)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value < 10 ** 10:
        return str(value)
    if isinstance(value, str):
        v = " ".join(value.split())
        return v or None
    return None


def _first(mapping: dict, keys: tuple) -> tuple[Optional[str], Optional[str]]:
    for k in keys:
        v = _text(mapping.get(k))
        if v:
            return k, v
    return None, None


def _compose(primary: str, region: Optional[str], country: Optional[str]) -> str:
    """'Muscat' + 'Oman' -> 'Muscat, Oman', without repeating a part the text already contains."""
    out = primary
    low = primary.casefold()
    for part in (region, country):
        if part and part.casefold() not in low:
            out += ", " + part
            low = out.casefold()
    return out[:200]


def _place_from(mapping: dict, *, inner: bool) -> tuple[Optional[str], list, Optional[dict]]:
    """(place text, notes, location_normalized_from) built from the place-like keys of `mapping`."""
    keys = _PLACE_KEYS + (_INNER_ONLY_PLACE_KEYS if inner else ())
    pk, primary = _first(mapping, keys)
    if not primary:
        return None, [], None
    _rk, region = _first(mapping, _REGION_KEYS)
    _ck, country = _first(mapping, _COUNTRY_KEYS)
    place = _compose(primary, region, country)
    used = {k: v for k, v in (("city" if pk != "zip_or_city" else "zip_or_city", primary),
                              ("region", region), ("country", country)) if v}
    notes: list = []
    normalized: Optional[dict] = None
    if pk != "zip_or_city" or region or country:
        shown = {_echo(k): _echo(v) for k, v in used.items()}
        notes.append(f"the place was assembled from {', '.join(f'`{k}`' for k in shown)} "
                     f"as location.zip_or_city = '{_echo(place, 120)}'")
        normalized = shown
    return place, notes, normalized


def _read_location(args: dict, prepared: Prepared) -> Optional[str]:
    loc = args.get("location")
    place: Optional[str] = None
    radius_miles: Optional[float] = None

    if isinstance(loc, dict):
        if any(k in loc for k in _COORD_KEYS):
            raise _error("invalid_argument",
                         "`location` carried coordinates (latitude/longitude), which are not supported - "
                         "the search needs a place NAME.", args)
        place, notes, normalized = _place_from(loc, inner=True)
        prepared.notes += notes
        prepared.location_normalized_from = normalized
        if "radius_miles" in loc and loc["radius_miles"] is not None:
            radius_miles = _number(loc["radius_miles"], "location.radius_miles", args, minimum=0.0)
            if loc.get("radius_km") is not None:
                prepared.ignored.append("location.radius_km")
                prepared.notes.append("both location.radius_miles and location.radius_km were sent; "
                                      "radius_miles was used and radius_km was not")
        elif "radius_km" in loc and loc["radius_km"] is not None:
            km = _number(loc["radius_km"], "location.radius_km", args, minimum=0.0)
            radius_miles = round(km / _KM_PER_MILE, 3)
            prepared.notes.append(f"location.radius_km {_echo(loc['radius_km'])} was converted to "
                                  f"radius_miles {radius_miles}")
        known_inside = set(_PLACE_KEYS) | set(_INNER_ONLY_PLACE_KEYS) | set(_REGION_KEYS) | set(_COUNTRY_KEYS) \
            | set(_RADIUS_KEYS)
        prepared.ignored += [f"location.{_echo(k, 40)}" for k in loc if k not in known_inside]
    elif isinstance(loc, str) or (isinstance(loc, int) and not isinstance(loc, bool)):
        place = _text(loc)
        if place:
            prepared.notes.append("`location` was a plain string; it was read as location.zip_or_city")
            if isinstance(loc, int) and loc < 10000:
                prepared.notes.append(f"`location` was the number {loc}; a JSON number cannot keep a leading "
                                      "zero, so a ZIP such as 02139 must be sent as a string (\"02139\")")
    elif loc is not None:
        raise _error("invalid_argument",
                     f"`location` must be a place name (string) or an object with `zip_or_city`, "
                     f"got {type(loc).__name__}.", args)

    if not place:
        # Top-level parts, e.g. {"city": "Muscat", "country": "Oman"} - advertised by the schema. Tried
        # whenever `location` gave no place (empty, or an object that only carried a radius), so the refusal
        # below can never say "no city was sent" about a request that sent one.
        place, notes, normalized = _place_from(args, inner=False)
        prepared.notes += notes
        prepared.location_normalized_from = normalized
    else:
        # A region or country beside a `location` that names neither is the caller's own disambiguation
        # (Birmingham + US); geocoding the bare name would throw it away. A city-like top-level key is a
        # second, competing place and stays ignored. Parts the location object carries itself win.
        used_top: list = []
        inner_has_parts = isinstance(loc, dict) and any(k in loc for k in _REGION_KEYS + _COUNTRY_KEYS)
        if not inner_has_parts:
            rk, region = _first(args, _REGION_KEYS)
            ck, country = _first(args, _COUNTRY_KEYS)
            composed = _compose(place, region, country)
            if composed != place:
                prepared.notes.append(
                    f"the top-level {' and '.join(f'`{_echo(k, 40)}`' for k in (rk, ck) if k)} "
                    f"{'was' if (bool(rk) + bool(ck)) == 1 else 'were'} added to `location`: '{_echo(composed, 120)}'")
                place = composed
                used_top = [k for k in (rk, ck) if k]
        extra = [k for k in (_PLACE_KEYS + _REGION_KEYS + _COUNTRY_KEYS)
                 if k in args and k != "zip_or_city" and k not in used_top]
        if extra:
            prepared.ignored += [_echo(k, 40) for k in extra]
            prepared.notes.append("`location` was used; the top-level place argument(s) "
                                  + ", ".join(f"`{_echo(k, 40)}`" for k in extra) + " were not")

    if not place:
        only_broad = any(_text(args.get(k)) for k in _REGION_KEYS + _COUNTRY_KEYS)
        what = ("`location` is missing: a search needs a town or city, not a country or region alone."
                if only_broad else "`location` is missing, and no `city` was sent either. "
                "(There is no default city: guessing one would answer for the wrong place.)")
        raise _error("missing_argument", what, args, kind=_kind_hint(args))
    prepared.kwargs["location"] = {"zip_or_city": place}
    if radius_miles is not None:
        prepared.kwargs["location"]["radius_miles"] = radius_miles
    return place


def _number(value: Any, name: str, args: dict, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _error("invalid_argument", f"`{name}` must be a number, got {type(value).__name__}.", args)
    try:
        v = float(value)
    except ValueError:
        raise _error("invalid_argument", f"`{name}` must be a number, got '{_echo(value)}'.", args) from None
    if v != v or v in (float("inf"), float("-inf")) or v < minimum:
        raise _error("invalid_argument", f"`{name}` must be a number of at least {minimum:g}.", args)
    return v


# --------------------------------------------------------------------------- kind of business

def _kind_hint(args: dict) -> Optional[str]:
    """Whatever the caller already said about the kind of business, for a better example."""
    for k in ("capability",) + _KIND_ALIASES:
        v = _text(args.get(k))
        if v:
            return v
    v = _text(args.get("vertical"))
    return v if v and osm_categories.known_term(v) else None


def _place_hint(args: dict) -> Optional[str]:
    loc = args.get("location")
    if isinstance(loc, str):
        return _text(loc)
    if isinstance(loc, dict):
        return _first(loc, _PLACE_KEYS + _INNER_ONLY_PLACE_KEYS)[1]
    return _first(args, _PLACE_KEYS)[1]


def _read_kind(args: dict, prepared: Prepared, place: str) -> None:
    capability = args.get("capability")
    if capability is not None and not isinstance(capability, str):
        raise _error("invalid_argument", f"`capability` must be a string (the kind of business), "
                     f"got {type(capability).__name__}.", args, place=place)
    capability = _text(capability)

    if not capability:
        for alias in _KIND_ALIASES:
            v = _text(args.get(alias)) if not isinstance(args.get(alias), (dict, list)) else None
            if v:
                capability = v
                prepared.notes.append(f"`{alias}` was read as `capability` = '{_echo(v)}'")
                break

    vertical_raw = args.get("vertical")
    if vertical_raw is not None and not isinstance(vertical_raw, str):
        raise _error("invalid_argument", f"`vertical` must be a string, got {type(vertical_raw).__name__}.",
                     args, place=place)
    vertical_text = _text(vertical_raw)
    if vertical_text:
        macro, _hint, recognised = interpret_vertical(vertical_text)
        is_macro = vertical_text.strip().lower().replace("-", "_").replace(" ", "_") in MACRO_VERTICALS
        shown = _echo(vertical_text)
        if macro and not is_macro:
            prepared.notes.append(f"`vertical` '{shown}' is a kind of business, not one of the three "
                                  f"verticals; it was read as {macro}")
        if not recognised:
            # A word we do not map to OpenStreetMap tags. Search for it BY NAME and say that is what
            # happened - or, if a capability was also given, say the capability chose the category.
            if capability:
                prepared.notes.append(f"`vertical` '{shown}' is not one of the three verticals and not a "
                                      "kind we map; `capability` chose the category")
            else:
                capability = _echo(vertical_text, 60)
                prepared.notes.append(f"`vertical` '{shown}' is not one of the three verticals and not a "
                                      "kind we map to OpenStreetMap tags; it is matched against business "
                                      "names instead")
        elif is_macro and not capability:
            # Allowed - it searches the family's common kinds - but it is the weakest question.
            prepared.notes.append(f"only the broad family '{macro}' was given; naming the kind of business "
                                  "in `capability` (e.g. 'dentist') returns much better matches")
        # The raw TEXT must reach FindBusinessRequest: it reads `vertical_term` from it.
        prepared.kwargs["vertical"] = vertical_text
    # A vertical the caller did NOT send is never inferred into the request: the supply network is
    # filtered on what was said, not on a guess (the example builder uses a family; the request does not).

    if not capability and not vertical_text:
        raise _error("missing_argument",
                     "no kind of business was named: `capability` (or `vertical`) is missing.",
                     args, place=place)
    if capability:
        prepared.kwargs["capability"] = capability


# --------------------------------------------------------------------------- entry point

def prepare(arguments: Any) -> Prepared:
    """Read a find_business `arguments` object. Raises FindBusinessInputError when it cannot be
    searched as sent; otherwise returns kwargs for FindBusinessRequest and the notes."""
    args = arguments if isinstance(arguments, dict) else {}
    prepared = Prepared(kwargs={})

    place = _read_location(args, prepared)
    _read_kind(args, prepared, place)

    # max_results: clamp and say so, rather than refuse a tidy request for 50 rows.
    mr = args.get("max_results")
    if mr is not None:
        if isinstance(mr, bool) or not isinstance(mr, (int, float, str)):
            raise _error("invalid_argument", f"`max_results` must be a whole number from 1 to 20, got "
                         f"{type(mr).__name__}.", args, place=place, kind=_kind_hint(args))
        try:
            as_float = float(mr)
            # float() accepts "inf", "1e999" and "nan"; int() of the first two raises OverflowError, which
            # nothing used to catch (the caller saw JSON-RPC -32603 "Internal error"), and none of the three
            # is a number of rows.
            if as_float != as_float or as_float in (float("inf"), float("-inf")):
                raise ValueError("not a finite number")
            n = int(as_float)
        except (ValueError, OverflowError):
            raise _error("invalid_argument", f"`max_results` must be a whole number from 1 to 20, got "
                         f"'{_echo(mr)}'.", args, place=place, kind=_kind_hint(args)) from None
        if as_float != n and abs(as_float) < 1e15:
            prepared.notes.append(f"max_results {_echo(mr)} is not a whole number; {n} was used")
        clamped = max(1, min(20, n))
        if clamped != n:
            prepared.notes.append(f"max_results {n} is outside 1-20; {clamped} was used")
        prepared.kwargs["max_results"] = clamped

    for passthrough in ("price_band", "availability_window"):
        if args.get(passthrough) is not None:
            prepared.kwargs[passthrough] = args[passthrough]

    prepared.ignored += [_echo(k, 40) for k in args if k not in _KNOWN_TOP_LEVEL]
    prepared.ignored = list(dict.fromkeys(prepared.ignored))[:10]
    return prepared


def explain_validation_error(exc: Exception, arguments: Any) -> FindBusinessInputError:
    """A pydantic ValidationError that survived `prepare` (a malformed price_band, say), as the same
    guided refusal: which field, what it expected, and a working example."""
    args = arguments if isinstance(arguments, dict) else {}
    fields: list[str] = []
    try:
        for err in exc.errors():                                           # type: ignore[attr-defined]
            loc = ".".join(str(p) for p in err.get("loc", ()))
            fields.append(f"{loc or '<request>'} ({_echo(err.get('msg', 'invalid'), 100)})")
    except Exception:                                                      # noqa: BLE001
        pass
    detail = "; ".join(fields[:4]) or "the request did not validate"
    place = _place_hint(args)
    err = _error("invalid_argument", f"{detail}.", args, place=place, kind=_kind_hint(args))
    err.how_to_resolve["invalid_fields"] = fields[:6]
    return err
