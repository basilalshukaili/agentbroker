"""
Map a free-text service category onto OpenStreetMap tags.

WHY A TABLE, AND WHY IT IS SMALL
================================
find_business takes a macro `vertical` (three values) plus a free-text
`capability` ("haircut", "plumbing", "pharmacy"). OpenStreetMap has no such
vocabulary: a hairdresser is `shop=hairdresser`, a plumber is
`craft=plumber`, a lawyer is `office=lawyer`. Something has to translate, and
the honest way to do it is an explicit, reviewable table - not a model guess
that can silently return the wrong kind of business.

Rules the table follows:

  * Every entry is a real OSM tag pair, so a hit is a feature somebody
    mapped as that kind of thing. We never label a business with a category
    the map does not give it.
  * A term we do not know is NOT silently dropped. It falls back to a NAME
    match (the term must appear in the feature's name, and the feature must
    carry a shop/amenity/craft/office/healthcare/leisure tag so we do not
    return benches and postboxes). The response says which basis was used
    (`category_match.basis`), so a caller can tell a tag hit from a name hit.
  * With no usable term at all we search the broad tag set for the macro
    vertical, and say so.

A selector is an AND of (key, value-regex) pairs. Values are fixed strings
from this file - caller text NEVER reaches a key or a value regex. The only
caller text that reaches a query is the name-match phrase, which is reduced
to letters, digits, spaces, hyphens, apostrophes and ampersands first (no
character that means anything in an Overpass regular expression).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# One selector = an AND of (osm_key, value_alternatives) pairs.
Selector = tuple[tuple[str, str], ...]

_BUSINESS_KEYS = ("shop", "amenity", "craft", "office", "healthcare", "leisure")

_VALUE_RE = re.compile(r"^[a-z_]+(\|[a-z_]+)*$")
_KEY_RE = re.compile(r"^[a-z_:]+$")


def _s(key: str, values: str) -> Selector:
    return ((key, values),)


# Normalised term (lowercase, single spaces) -> selectors. OR across the tuple.
CATEGORY_TABLE: dict[str, tuple[Selector, ...]] = {}


def _add(terms: str, *selectors: Selector) -> None:
    for term in terms.split(","):
        CATEGORY_TABLE[term.strip()] = tuple(selectors)


# --- personal services ------------------------------------------------------
_add("hairdresser,haircut,hair,barber,barbershop,barber shop,hair salon,salon",
     _s("shop", "hairdresser|barber"))
_add("beauty,beauty salon,nail,nails,nail salon,manicure,pedicure,lash,lashes,"
     "lash extensions,makeup,waxing",
     _s("shop", "beauty"))
_add("massage,swedish massage", _s("shop", "massage"))
_add("spa", _s("shop", "massage"), _s("amenity", "spa"), _s("leisure", "spa"))
_add("yoga,yoga class,pilates", _s("sport", "yoga|pilates"))
_add("fitness,gym,personal training,personal trainer,crossfit",
     _s("leisure", "fitness_centre"), _s("amenity", "gym"))
_add("restaurant,restaurants,dining", _s("amenity", "restaurant"))
_add("food", _s("amenity", "restaurant|fast_food|cafe"))
_add("cafe,coffee,coffee shop", _s("amenity", "cafe"))
_add("bakery", _s("shop", "bakery"))
_add("pharmacy", _s("amenity", "pharmacy"))
_add("laundry,dry cleaning", _s("shop", "laundry|dry_cleaning"))
_add("florist", _s("shop", "florist"))
_add("tailor,tailoring", _s("craft", "tailor"), _s("shop", "tailor"))

# --- home services ----------------------------------------------------------
_add("plumber,plumbing", _s("craft", "plumber"))
_add("electrician,electrical", _s("craft", "electrician"))
_add("hvac,air conditioning,heating", _s("craft", "hvac"))
_add("cleaning,house cleaning,cleaner,janitorial",
     _s("craft", "cleaning|window_cleaner"), _s("office", "cleaning"))
_add("pest control,pest,exterminator", _s("craft", "pest_control"))
_add("lawn,lawn care,landscaping,landscaper,gardener,gardening",
     _s("craft", "gardener|landscaper"))
_add("handyman,general handyman", _s("craft", "handyman"))
_add("carpenter,carpentry", _s("craft", "carpenter"))
_add("painter,painting", _s("craft", "painter"))
_add("locksmith", _s("craft", "locksmith"), _s("shop", "locksmith"))
_add("roofing,roofer,roof", _s("craft", "roofer"))
_add("mechanic,car repair,auto repair", _s("shop", "car_repair"))

# --- professional services --------------------------------------------------
_add("lawyer,attorney,legal,law,law firm,legal consultation", _s("office", "lawyer"))
_add("notary", _s("office", "notary"))
_add("tax,tax advisor,tax consultation", _s("office", "tax_advisor"))
_add("accounting,accountant,accountants,bookkeeping,bookkeeper,business accounting",
     _s("office", "accountant"))
_add("financial,financial planning,financial advisor,financial planner",
     _s("office", "financial|financial_advisor"))
_add("insurance", _s("office", "insurance"))
_add("real estate,realtor,estate agent", _s("office", "estate_agent"))
_add("architect", _s("office", "architect"))
_add("consulting,consultant", _s("office", "consulting"))
_add("dentist,dental", _s("amenity", "dentist"), _s("healthcare", "dentist"))
_add("doctor,physician,medical,clinic,medical consultation",
     _s("amenity", "doctors|clinic"), _s("healthcare", "doctor|clinic"))
_add("tutor,tutors,tutoring,math tutoring",
     _s("office", "educational_institution"), _s("amenity", "prep_school"))

# When no usable term is given, search the broad tag set for the macro vertical.
VERTICAL_DEFAULTS: dict[str, tuple[Selector, ...]] = {
    "personal_services": (
        _s("shop", "hairdresser|barber|beauty|massage"),
        _s("leisure", "fitness_centre"),
    ),
    "home_services": (
        _s("craft", "plumber|electrician|hvac|carpenter|painter|roofer|gardener|"
                    "handyman|locksmith|pest_control"),
    ),
    "professional_services": (
        _s("office", "lawyer|accountant|tax_advisor|insurance|financial|"
                     "estate_agent|architect|consulting|notary"),
        _s("amenity", "dentist|doctors|clinic"),
    ),
}

# The name-match fallback is limited to features that look like businesses.
NAME_FALLBACK_KEYS = _BUSINESS_KEYS

_MAX_NAME_PHRASE = 40


@dataclass(frozen=True)
class CategoryPlan:
    """What to look for, and on what basis. Immutable so it can key a cache."""
    selectors: tuple[Selector, ...]
    basis: str                          # term_table | term_table+name_match | name_match | vertical_default
    matched_term: Optional[str] = None  # the table key that matched, if any
    name_phrase: Optional[str] = None   # sanitised phrase the name must contain

    def describe_tags(self) -> list[str]:
        """Human/machine readable tag list for the response."""
        out: list[str] = []
        for sel in self.selectors:
            out.append(" & ".join(f"{k}={v}" for k, v in sel))
        if self.name_phrase and not self.selectors:
            out = [f"{k}=* (name contains '{self.name_phrase}')" for k in NAME_FALLBACK_KEYS]
        elif self.name_phrase:
            out = [f"{o} & name contains '{self.name_phrase}'" for o in out]
        return out

    def signature(self) -> str:
        return repr((self.selectors, self.name_phrase))


def normalise_term(text: Optional[str]) -> str:
    """lowercase, `_` and `-` as spaces, letters/digits/space only, collapsed."""
    if not text:
        return ""
    t = str(text).lower().replace("_", " ").replace("-", " ")
    t = "".join(ch if (ch.isalnum() or ch == " ") else " " for ch in t)
    return " ".join(t.split())


def sanitise_name_phrase(text: Optional[str]) -> Optional[str]:
    """Reduce caller text to characters that carry no regex meaning.

    Keeps letters and digits (any script), space, hyphen, apostrophe and
    ampersand. Everything else - including every character that is special in
    an Overpass (POSIX ERE) pattern or a QL string - becomes a space.
    """
    if not text:
        return None
    t = str(text).replace("_", " ")
    t = "".join(ch if (ch.isalnum() or ch in " -'&") else " " for ch in t)
    t = " ".join(t.split())[:_MAX_NAME_PHRASE].strip(" -'&")
    return t if len(t) >= 2 else None


def _lookup(term: str) -> tuple[Optional[str], tuple[Selector, ...]]:
    """Find the table key for `term`: exact, plural-stripped, then the longest
    key that appears as whole words inside it ("swedish massage", "roof repair")."""
    if not term:
        return None, ()
    if term in CATEGORY_TABLE:
        return term, CATEGORY_TABLE[term]
    if term.endswith("s") and term[:-1] in CATEGORY_TABLE:
        return term[:-1], CATEGORY_TABLE[term[:-1]]
    best: Optional[str] = None
    for key in CATEGORY_TABLE:
        if len(key) < 3:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", term):
            if best is None or len(key) > len(best):
                best = key
    if best is not None:
        return best, CATEGORY_TABLE[best]
    return None, ()


def resolve(vertical: str, capability: Optional[str], vertical_term: Optional[str] = None) -> CategoryPlan:
    """Turn (vertical, capability, the vertical word the caller used) into a plan.

    Order of precedence:
      1. capability found in the table            -> its tags
      2. the vertical word found in the table     -> its tags, and the capability
         (if any) must ALSO appear in the name
      3. capability text only                     -> name match on business-like features
      4. nothing usable                           -> broad tags for the macro vertical
    """
    cap_norm = normalise_term(capability)
    term_norm = normalise_term(vertical_term)

    key, selectors = _lookup(cap_norm)
    if selectors:
        return CategoryPlan(selectors, "term_table", matched_term=key)

    key, selectors = _lookup(term_norm)
    if selectors:
        phrase = sanitise_name_phrase(capability)
        if phrase:
            return CategoryPlan(selectors, "term_table+name_match",
                                matched_term=key, name_phrase=phrase)
        return CategoryPlan(selectors, "term_table", matched_term=key)

    phrase = sanitise_name_phrase(capability)
    if phrase:
        return CategoryPlan((), "name_match", name_phrase=phrase)

    v = getattr(vertical, "value", vertical)
    defaults = VERTICAL_DEFAULTS.get(str(v), ())
    return CategoryPlan(defaults, "vertical_default")


def _validate_table() -> None:
    for term, sels in CATEGORY_TABLE.items():
        assert term == normalise_term(term), term
        for sel in sels:
            for k, v in sel:
                assert _KEY_RE.match(k) and _VALUE_RE.match(v), (term, k, v)
    for sels in VERTICAL_DEFAULTS.values():
        for sel in sels:
            for k, v in sel:
                assert _KEY_RE.match(k) and _VALUE_RE.match(v), (k, v)


_validate_table()
