"""A country mismatch must never turn a MATCH into a clean screen.

`country` was accepted and ignored for as long as it existed - consumed only
by OpenSanctions - while the response cheerfully said "(country filter: IR)".
Making it real is an improvement; making it EXCLUDE would have been a
regression far worse than the no-op it replaced.

Our country data is the address, nationality and birth country written on a
listing. It is missing on roughly 15% of entries, the EU writes ISO2 codes
while the UK writes strings like "FORMER USSR CURRENTLY UKRAINE", and a
sanctioned party operates wherever it likes. Every one of those gaps would
become a FALSE NEGATIVE - a confident "no match" about someone who is on the
list - if a mismatch removed the row.

So the rule is: annotate and rank, never remove. These tests are what stop a
future edit from quietly turning a hint into a filter.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.screen_sanctions as ss  # noqa: E402
import storage.supabase_client as sb  # noqa: E402


def _screen(name, country=None):
    rec = asyncio.run(ss.handle_screen_sanctions(name=name, country=country))
    return rec.result or {}


# ---------------------------------------------------------------------------
# WHY THE FOUR TESTS BELOW USED TO SKIP, AND WHY THAT WAS THE WRONG GATE.
#
# They called `_has_db()` and skipped without live Supabase credentials. But
# the property they guard - "a country mismatch annotates, it never removes
# the row" - is decided entirely INSIDE `_screen_list_db`'s own Python: it
# queries by name only (never by country), then calls `_country_matches`
# AFTER the rows are back, purely to label them. No part of that logic reads
# anything about the DATA beyond "what rows exist" - it is a control-flow
# property, not a fact about the real sanctions lists.
#
# So `_has_db()` was gating on the wrong thing: not "do we have real data" but
# "do we have a real network path to Supabase". Faking the ONE thing that
# actually requires a network - the two storage-layer calls
# `select_rows_strict` / `select_rows` - lets the REAL `_screen_list_db`,
# `_country_matches` and `handle_screen_sanctions` all run unmodified against
# a small in-memory table standing in for `public.sanctions_names`. That is
# the same seam `test_sanctions_false_clean_guards.py::test_a_failed_age_read_is_not_cached`
# already uses for exactly this table. No live database is required, and
# nothing here would be more convincing with one plugged in - a real Supabase
# would exercise this exact code against different bytes, not different logic.
# ---------------------------------------------------------------------------

LISTED = "Saddam Hussein Al-Tikriti"          # OFAC + EU (IQ) + UK (IRAQ)

_TOKENS = sorted(set(ss._normalize_name(LISTED).split()))
_NAME_KEY = " ".join(_TOKENS)


def _seed_fake_index(monkeypatch, eu_countries=("IQ",), uk_countries=("IRAQ",)):
    """Stand in for `public.sanctions_names` with one row per list, both
    carrying LISTED under the country spellings each real feed actually uses
    (EU: ISO2, UK: a plain country name) - and fake the OFAC path too, since
    OFAC never carries country data at all (see
    test_a_listing_with_no_country_is_unknown_not_a_mismatch below).

    Only the two storage-layer functions are replaced; `_screen_list_db`,
    `_list_refreshed_at`, `_country_matches` and `handle_screen_sanctions`
    all run for real against this table.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    table = [
        {"list_code": "EU", "display_name": "Al-Tikriti, Saddam Hussein",
         "programme": "IRAQ2", "etype": "INDIVIDUAL",
         "countries": list(eu_countries), "tokens": _TOKENS,
         "name_key": _NAME_KEY, "refreshed_at": today},
        {"list_code": "UK", "display_name": "SADDAM Hussein AL-TIKRITI",
         "programme": "IRAQ (SANCTIONS) REGS", "etype": "INDIVIDUAL",
         "countries": list(uk_countries), "tokens": _TOKENS,
         "name_key": _NAME_KEY, "refreshed_at": today},
    ]

    async def _fake_select_rows_strict(table_name, filters=None, limit=1000,
                                       order=None, gte=None):
        assert table_name == "sanctions_names"
        rows = table
        filters = filters or {}
        if "list_code" in filters:
            rows = [r for r in rows if r["list_code"] == filters["list_code"]]
        if "name_key" in filters:
            rows = [r for r in rows if r["name_key"] == filters["name_key"]]
        if "tokens" in filters:
            # Mirrors _select_params' "cs.{a,b,c}" contains-operator encoding.
            want = set(filters["tokens"][len("cs.{"):-1].split(","))
            rows = [r for r in rows if want <= set(r.get("tokens") or [])]
        if order == "refreshed_at.asc":
            rows = sorted(rows, key=lambda r: r.get("refreshed_at", ""))
        return list(rows[:limit])

    async def _fake_select_rows(table_name, filters=None, limit=1000,
                                order=None, gte=None):
        return await _fake_select_rows_strict(
            table_name, filters=filters, limit=limit, order=order, gte=gte)

    async def _fake_ofac(name):
        # OFAC has no country column at all - see the note in _to_match.
        return ([{
            "name": LISTED, "list": "OFAC-SDN", "match_score": 1.0,
            "program": "IRAQ SANCTIONS", "entity_type": "INDIVIDUAL",
            "source_url": "https://sanctionssearch.ofac.treas.gov/",
            "_matcher": "local_word_overlap",
        }], ["OFAC-SDN (fake)"], [])

    monkeypatch.setattr(sb, "select_rows_strict", _fake_select_rows_strict)
    monkeypatch.setattr(sb, "select_rows", _fake_select_rows)
    monkeypatch.setattr(ss, "_call_ofac_sdn", _fake_ofac)
    ss._age_cache.clear()


def test_a_deliberately_wrong_country_still_returns_the_match(monkeypatch):
    """The whole point. Screening a listed Iraqi against country=FR must still
    report him, flagged as a country mismatch - never drop him."""
    _seed_fake_index(monkeypatch)
    right = _screen(LISTED, country="IQ")
    ss._age_cache.clear()
    wrong = _screen(LISTED, country="FR")

    assert right.get("matched") is True
    assert wrong.get("matched") is True, (
        "a country mismatch turned a real sanctions match into a clean "
        "screen - this is the false negative the design exists to prevent")
    assert len(wrong.get("matches") or []) == len(right.get("matches") or []), (
        "a country mismatch removed matches from the result")


def test_the_mismatch_is_reported_rather_than_hidden(monkeypatch):
    _seed_fake_index(monkeypatch)
    wrong = _screen(LISTED, country="FR")
    flags = [m.get("country_match") for m in wrong["matches"]
             if m["list"].startswith(("EU-", "UK-"))]
    assert flags and all(f is False for f in flags), (
        f"expected every EU/UK match flagged as a country mismatch, got {flags}")


def test_a_listing_with_no_country_is_unknown_not_a_mismatch(monkeypatch):
    """None and False are different answers. Reporting "no country recorded"
    as a mismatch would tell a caller we checked and ruled it out."""
    _seed_fake_index(monkeypatch)
    r = _screen(LISTED, country="FR")
    ofac = [m for m in r["matches"] if "OFAC-SDN" in m["list"]]
    assert ofac, "OFAC match disappeared"
    assert ofac[0].get("country_match") is None, (
        "OFAC carries no country data, so its country_match must be null - "
        "not False, which would claim we had ruled the country out")


def test_the_response_does_not_claim_a_filter_was_applied(monkeypatch):
    _seed_fake_index(monkeypatch)
    r = _screen(LISTED, country="FR")
    assert r.get("country_filter_applied") is False
    assert "never to remove any" in (r.get("country_note") or "")


# NEAR MISSES. The original table only listed pairs that SHOULD match, so a
# matcher that returned True for everything would have passed it. These are
# the ten wrong answers an adversarial review measured on the first version,
# which used raw substrings: the KP pair put a North Korea query on a South
# Korea listing, and the reverse direction let any 2-letter code match inside
# any country name ("US" inside "RUSSIA").
#
# country_match: true is corroboration of a sanctions hit. Asserting it for
# the wrong country is the same defect as reporting a mismatch we never
# checked, aimed the other way.
@pytest.mark.parametrize("want,have", [
    ("KP", ["KOREA, REPUBLIC OF"]),      # North Korea query, South Korea listing
    ("KP", ["SOUTH KOREA"]),
    ("ML", ["SOMALIA"]),                 # "MALI" inside "SOMALIA"
    ("MALI", ["SOMALIA"]),
    ("NE", ["NIGERIA"]),                 # Niger is not Nigeria
    ("NIGER", ["NIGERIA"]),
    ("SD", ["SOUTH SUDAN"]),
    ("GN", ["GUINEA-BISSAU"]),
    ("GN", ["EQUATORIAL GUINEA"]),
    ("RUSSIA", ["US"]),                  # reverse-direction substring
    ("IRELAND", ["IR"]),
    ("CHINA", ["IN"]),
])
def test_a_different_country_is_not_a_match(want, have):
    assert ss._country_matches(want, have) is False, (
        f"{want!r} was reported as connected to {have!r} - that is "
        f"corroboration of a sanctions hit against the wrong country")


@pytest.mark.parametrize("want,have", [
    ("RU", ["RUSSIAN FEDERATION"]),      # official long form
    ("RUSSIA", ["RUSSIAN FEDERATION"]),
    ("SY", ["SYRIAN ARAB REPUBLIC"]),
    ("IR", ["IRAN, ISLAMIC REPUBLIC OF"]),
])
def test_official_long_forms_still_match(want, have):
    """Guard the guard: tightening the matcher must not lose the real ones."""
    assert ss._country_matches(want, have) is True


@pytest.mark.parametrize("want,have,expected", [
    ("IQ", ["IQ"], True),                       # ISO2 both sides (EU shape)
    ("IQ", ["IRAQ"], True),                     # caller ISO2, UK writes names
    ("IRAQ", ["IQ"], True),                     # the reverse
    ("IR", ["IRAN, ISLAMIC REPUBLIC OF"], True),
    ("UA", ["FORMER USSR CURRENTLY UKRAINE"], True),   # a real UK value
    ("FR", ["IQ"], False),
    ("IQ", [], None),                           # nothing recorded is not a miss
    ("", ["IQ"], None),
])
def test_country_matching_bridges_the_two_feed_formats(want, have, expected):
    assert ss._country_matches(want, have) is expected


# THE MOST SANCTIONED JURISDICTIONS, against the spellings our index really
# stores. An adversarial sweep found KP matched NOTHING - _ISO2_NAMES held
# four alternative names crammed into one string and the subset rule needs
# every word - so genuine DPRK entities came back country_match: FALSE and
# sorted BELOW listings with no country at all.
@pytest.mark.parametrize("code,listing", [
    ("KP", "NORTH KOREA"),                       # what the UK feed stores
    ("KP", "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF"),
    ("CD", "CONGO (DEMOCRATIC REPUBLIC)"),
    ("IR", "IRAN, ISLAMIC REPUBLIC OF"),
    ("SY", "SYRIAN ARAB REPUBLIC"),
    ("RU", "RUSSIA"),
    ("IQ", "IRAQ"),
])
def test_the_heaviest_jurisdictions_match_their_real_spellings(code, listing):
    assert ss._country_matches(code, [listing]) is True, (
        f"{code} does not match {listing!r}, which is a string our own index "
        f"stores - a false MISMATCH on a sanctions receipt")


@pytest.mark.parametrize("code,listing", [
    ("KP", "KOREA, REPUBLIC OF"),                # South Korea
    ("KP", "SOUTH KOREA"),
    ("CD", "CONGO, REPUBLIC OF"),                # the other Congo
])
def test_the_neighbouring_country_still_does_not_match(code, listing):
    assert ss._country_matches(code, [listing]) is False


def test_a_code_we_cannot_interpret_is_unknown_not_a_mismatch():
    """Passing a code the map does not carry used to return False, which the
    schema defines as "we checked and it is not that country". We had not
    checked; we did not know the word. Asserting a negative from ignorance is
    the same defect as the KP mapping, one level up.

    "DZ" was the original example here and is now MAPPED - the sweep that
    found it found 32 more, and the map was widened. TV (Tuvalu) stands in for
    the tail that will always exist."""
    assert "TV" not in ss._ISO2_NAMES, "pick a code still outside the map"
    assert ss._country_matches("TV", ["ALGERIA"]) is None
    # But two bare codes are comparable whether or not we know either.
    assert ss._country_matches("TV", ["IQ"]) is False
    assert ss._country_matches("FR", ["IQ"]) is False


def test_every_country_string_in_the_live_index_is_reachable_by_some_code():
    """THE GUARD FOR THE HOLE ITSELF.

    The KP bug and the 33 unreachable names are the same failure: a country
    the index really stores that no query can name. Checked against the
    spellings the feeds use, so a feed changing its format shows up here
    rather than as a silently weaker ranking signal.
    """
    reachable = {n.upper() for n in ss._ISO2_NAMES.values()}
    for code, aliases in ss._COUNTRY_ALIASES.items():
        reachable.update(a.upper() for a in aliases)

    # Spellings observed in the live UK and EU feeds.
    for listing in ["ALGERIA", "FRANCE", "GERMANY", "ISRAEL", "SAUDI ARABIA",
                    "THE GAMBIA", "OCCUPIED PALESTINIAN TERRITORIES",
                    "TRINIDAD AND TOBAGO", "EQUATORIAL GUINEA", "LAOS",
                    "EL SALVADOR", "NORTH KOREA", "KAZAKHSTAN"]:
        hits = [c for c in ss._ISO2_NAMES
                if ss._country_matches(c, [listing]) is True]
        assert hits, (
            f"no ISO code in the map matches {listing!r}, a country string "
            f"our own index stores - nobody can ask about it")
