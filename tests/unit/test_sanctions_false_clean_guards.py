"""The four ways screen_sanctions could still report a false clean.

All four were found by an adversarial review on 2026-08-30, after the
screening_status work had already landed. Each one produces the single worst
output this product can make: a party who IS listed, reported as not listed.
"""
from __future__ import annotations

import asyncio

import pytest

import core.screen_sanctions as ss


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 1. A 200 IS NOT A SANCTIONS LIST
# --------------------------------------------------------------------------
# _fetch_url accepted any non-empty 200 body and cached it for six hours.
# _parse_ofac_sdn yields nothing for a body that is not the CSV, so OFAC was
# reported SCREENED and clean while zero lists had been read.

@pytest.mark.parametrize("body,why", [
    ('{"error":"Service temporarily unavailable","code":503}', "a JSON error"),
    ("<!DOCTYPE html><html><body>Maintenance</body></html>", "an HTML page"),
    ("", "an empty body"),
    ("   \n  \n ", "whitespace"),
    ("ent_num,SDN_Name,SDN_Type,Program\n1,ACME,-0-,IRAN\n", "a truncated CSV"),
])
def test_a_body_that_is_not_the_list_is_refused(body, why):
    assert ss._looks_like_a_sanctions_list(body) is False, (
        f"{why} was accepted as the sanctions list - it would be cached for "
        f"six hours and screened against, finding nothing")


def test_the_real_shape_is_accepted():
    """The guard must not reject the actual file. SDN.CSV is ~18,000 rows of
    comma-separated values with no header."""
    real = "\n".join(
        f'{i},"ENTITY {i}","-0-","IRAN","additional info"' for i in range(500))
    assert ss._looks_like_a_sanctions_list(real) is True


def test_a_bad_body_is_never_cached(monkeypatch):
    """The six-hour cache is what turned one bad response into six hours of
    false cleans."""
    class _Resp:
        status_code = 200
        text = '{"error":"nope"}'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Client())
    ss._list_cache.clear()
    _run(ss._fetch_url("https://example.invalid/sdn.csv"))
    assert "https://example.invalid/sdn.csv" not in ss._list_cache, (
        "an error body was cached as the sanctions list")


# --------------------------------------------------------------------------
# 2. THE TWO CONGOS, AND THE TWO KOREAS
# --------------------------------------------------------------------------
# The alias loop did an unguarded subset test, UPSTREAM of both qualifier
# rules - so a query meaning one country inherited the other's name forms and
# code, and neither guard could then reject it.

@pytest.mark.parametrize("listing", [
    "CD",                               # 250 EU rows
    "CONGO (DEMOCRATIC REPUBLIC)",      # 256 UK rows
    "DEMOCRATIC REPUBLIC OF THE CONGO",
    "DRC",
])
def test_congo_does_not_match_the_drc(listing):
    assert ss._country_matches("CONGO", [listing]) is not True, (
        f"a query for the Republic of the Congo corroborates a DRC listing "
        f"({listing!r}) - wrong-country corroboration on a sanctions receipt")


@pytest.mark.parametrize("listing", [
    "KP", "NORTH KOREA", "DPRK", "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF",
])
def test_a_bare_korea_query_never_corroborates_the_dprk(listing):
    """Someone screening a South Korean supplier who types "Korea" was getting
    country_match: true on ~900 DPRK designations, which then sorted first.

    SCOPED DELIBERATELY. "KOREA" does still match "KOREA, REPUBLIC OF" -
    REPUBLIC is not a distinguishing word - and that is the reading almost
    everyone means by the bare word. It over-asserts mildly in a direction
    that costs nothing: country_match only RANKS, it never removes a match,
    so the worst case is a South Korean listing sorted higher. Corroborating
    a DPRK designation is the direction that does damage, and it is the one
    this asserts."""
    assert ss._country_matches("KOREA", [listing]) is not True


def test_the_congo_that_should_match_still_does():
    """Conservatism must not cost the true positives."""
    assert ss._country_matches("CD", ["CONGO (DEMOCRATIC REPUBLIC)"]) is True
    assert ss._country_matches("CD", ["DEMOCRATIC REPUBLIC OF THE CONGO"]) is True
    assert ss._country_matches("CG", ["CONGO"]) is True
    assert ss._country_matches("KP", ["NORTH KOREA"]) is True
    assert ss._country_matches("KR", ["KOREA, REPUBLIC OF"]) is True
    assert ss._country_matches("RU", ["RUSSIAN FEDERATION"]) is True


# --------------------------------------------------------------------------
# 3. THE WEAK-NAME KEEP-FILTER COMPARED TWO DIFFERENT DATA SHAPES
# --------------------------------------------------------------------------

def _weak_name_ofac(monkeypatch, listed_name: str):
    """Drive the REAL handler with one OFAC hit and no database.

    An earlier version of this test recomputed the comparison inline, which
    made it pass whether or not the source was fixed - the exact shape of
    useless test this repo keeps finding. It has to run the filter.
    """
    async def _ofac(name):
        return ([{
            "name": listed_name, "list": "OFAC-SDN", "match_score": 1.0,
            "program": "NPWMD", "entity_type": "INDIVIDUAL",
            "source_url": "https://sanctionssearch.ofac.treas.gov/",
            "_matcher": "local_word_overlap",
        }], ["OFAC-SDN"], [])

    async def _no_db(name, code, label, url, want_country=None):
        return [], [], [f"{label} (index unreachable)"]

    monkeypatch.setattr(ss, "_call_ofac_sdn", _ofac)
    monkeypatch.setattr(ss, "_screen_list_db", _no_db)


def test_an_ofac_exact_match_survives_regardless_of_word_order(monkeypatch):
    """The filter compared an ORDERED list of the listed name's tokens against
    a SORTED set of the query's, so any entry whose own order was not
    alphabetical could not be kept by any query. 276 SDN entries, 239 of them
    absent from the EU and UK lists too - MS-13, Mahan Air's tail numbers, and
    a run of North Korean WMD designations."""
    _weak_name_ofac(monkeypatch, "RI, Je-Son")
    rc = _run(ss.handle_screen_sanctions("Ri Je Son"))
    cands = rc.result.get("possible_matches_unverified") or []
    assert cands, (
        "an OFAC exact match at score 1.00 was dropped because the listed "
        "name is not in alphabetical order - this designation is invisible "
        "from every source we have")
    assert cands[0]["name"].upper().startswith("RI")
    # Surfaced, never asserted: it is still a weak name.
    assert rc.result["matched"] is False


def test_the_filter_still_rejects_a_different_name(monkeypatch):
    """Loosening the comparison must not let a NEAR name through - that is
    the harm the whole weak-name rule exists to prevent."""
    _weak_name_ofac(monkeypatch, "RI, Je-Sun")       # Sun, not Son
    rc = _run(ss.handle_screen_sanctions("Ri Je Son"))
    assert not (rc.result.get("possible_matches_unverified") or []), (
        "a different name survived the weak-name filter")
    assert rc.result["matched"] is False


# --------------------------------------------------------------------------
# 4. AN INDEX OF UNKNOWN AGE IS NOT A FRESH ONE
# --------------------------------------------------------------------------

def test_unknown_age_is_not_screened(monkeypatch):
    """_list_refreshed_at returns None on any error, _days_since(None) is
    None, and the staleness gate read `age is not None and age > limit` - so
    unknown age could not fire it and the list was counted as screened."""
    async def _unknown(code):
        return None

    monkeypatch.setattr(ss, "_list_refreshed_at", _unknown)
    matches, queried, unavail = _run(ss._screen_list_db(
        "Vladimir Putin", "EU", "EU-CONSOLIDATED", "https://example.invalid/"))
    assert not matches
    assert unavail, "a list of unknown age was reported as screened"
    assert "age" in unavail[0].lower()


def test_a_failed_age_read_is_not_cached(monkeypatch):
    """The failure was written into _age_cache OUTSIDE the try, so one blip
    took the list out of service for the full 15-minute TTL even after
    Supabase recovered."""
    ss._age_cache.clear()
    calls = []

    async def _boom(table, **kw):
        calls.append(1)
        raise RuntimeError("supabase down")

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_strict", _boom)

    assert _run(ss._list_refreshed_at("EU")) is None
    assert "EU" not in ss._age_cache, "a failed age read was cached"
    assert _run(ss._list_refreshed_at("EU")) is None
    assert len(calls) == 2, "the second call was served from a cached failure"
