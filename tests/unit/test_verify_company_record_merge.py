"""verify_company_record must not invent a company, and must not call one
unregistered without asking the registry that would know.

Both defects below were live and both produced receipts that look completely
normal - which is what makes them the two worst outputs this tool can produce.
"""
from __future__ import annotations

import asyncio

import pytest

from core import verify_company_record as V


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# #3 - THE MERGE STAPLED TWO DIFFERENT COMPANIES TOGETHER
# --------------------------------------------------------------------------
# GLEIF is keyed on the LEI when one is given; SEC EDGAR is always keyed on
# `name`. Nothing compared the two results. Supplying one company's name and
# another's LEI returned a single entity carrying TESLA's legal name and
# APPLE's ticker and CIK.

_TESLA_GLEIF = {
    "id": "54930043XZGB27CTOV49",
    "attributes": {
        "lei": "54930043XZGB27CTOV49",
        "entity": {
            "legalName": {"name": "TESLA, INC."},
            "status": "ACTIVE",
            "jurisdiction": "US-DE",
            "legalAddress": {"city": "Austin", "country": "US"},
        },
        "registration": {"status": "ISSUED", "managingLou": {"name": "Bloomberg"}},
    },
}

_APPLE_EDGAR = {
    "legal_name": "Apple Inc.",
    "ticker": "AAPL",
    "cik": "320193",
    "registry_authority": "SEC EDGAR (US public companies)",
    "jurisdiction": "US",
    "status": "active",
}


@pytest.fixture
def _two_companies(monkeypatch):
    async def _gleif_by_lei(lei):
        return _TESLA_GLEIF

    async def _edgar(name):
        return dict(_APPLE_EDGAR)

    monkeypatch.setattr(V, "_gleif_by_lei", _gleif_by_lei)
    monkeypatch.setattr(V, "_edgar_search", _edgar)


def test_a_sec_hit_for_a_different_company_is_not_attached(_two_companies):
    r = _run(V.handle_verify_company_record(
        name="Apple Inc", lei="54930043XZGB27CTOV49"))
    res = r.result

    assert res["legal_name"] == "TESLA, INC."
    assert res["sec_cik"] is None, (
        "Apple's CIK is attached to Tesla's registry record - this receipt "
        "describes a company that does not exist")
    assert res["ticker"] is None
    assert res["sources"] == ["GLEIF"]


def test_the_mismatch_is_disclosed_rather_than_dropped(_two_companies):
    """Refusing the merge silently would hide the most useful fact on the
    call: the name and the LEI the caller supplied disagree."""
    r = _run(V.handle_verify_company_record(
        name="Apple Inc", lei="54930043XZGB27CTOV49"))
    conflict = r.result.get("unmerged_sec_match")
    assert conflict, "the SEC mismatch is not reported anywhere"
    assert conflict["sec_ticker"] == "AAPL"
    assert "DIFFERENT company" in r.human_message


def test_the_same_company_still_merges(monkeypatch):
    """The guard must not cost us the enrichment it is protecting. GLEIF
    writes "APPLE INC." and SEC writes "Apple Inc." - punctuation, case and
    corporate suffixes differ between the registries on nearly every name."""
    apple_gleif = {
        "id": "HWUPKR0MPOU8FGXBT394",
        "attributes": {
            "lei": "HWUPKR0MPOU8FGXBT394",
            "entity": {
                "legalName": {"name": "APPLE INC."},
                "status": "ACTIVE",
                "jurisdiction": "US-CA",
                "legalAddress": {"city": "Cupertino", "country": "US"},
            },
            "registration": {"status": "ISSUED"},
        },
    }

    async def _gleif_by_name(name, country):
        return [apple_gleif]

    async def _edgar(name):
        return dict(_APPLE_EDGAR)

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    monkeypatch.setattr(V, "_edgar_search", _edgar)

    r = _run(V.handle_verify_company_record(name="Apple Inc"))
    assert r.result["sec_cik"] == "320193"
    assert r.result["ticker"] == "AAPL"
    assert r.result["sources"] == ["GLEIF", "SEC EDGAR"]
    assert "unmerged_sec_match" not in r.result


@pytest.mark.parametrize("a,b,same", [
    ("APPLE INC.", "Apple Inc.", True),
    ("Tesla, Inc.", "TESLA INC", True),
    ("Vodafone Group Public Limited Company", "Vodafone Group Plc", True),
    ("Public Storage", "Storage Inc", False),   # the word alone is not a suffix
    ("Siemens Aktiengesellschaft", "Siemens AG", True),
    ("Microsoft Corporation", "Microsoft Corp", True),
    # A suffix may be spelled differently; it may not be MISSING. Bare
    # "Apple" names no legal entity, and test_edgar_name_matching.py
    # forbids resolving it to one.
    ("Apple Inc.", "Apple", False),
    ("Apple", "Apple Inc.", False),
    ("APPLE INC.", "Apple Hospitality REIT, Inc.", False),
    ("Apple Inc.", "Pineapple Financial Inc.", False),
    ("TESLA, INC.", "Apple Inc.", False),
    ("", "Apple Inc.", False),
    (None, None, False),
])
def test_same_entity_bar(a, b, same):
    assert V._same_entity(a, b) is same


# --------------------------------------------------------------------------
# #4 - "NOT REGISTERED" ON THE STRENGTH OF A US TICKER FILE
# --------------------------------------------------------------------------

def test_gleif_down_never_reads_as_not_found(monkeypatch):
    """No country given means is_us defaults True, so the old count wanted
    BOTH registries dark. GLEIF down + EDGAR up (and empty) therefore produced
    the full not_found sentence - "may not be a legal entity registered with
    these free registries" - about a company whose only crime was not being
    listed on a US exchange."""
    async def _gleif_by_name(name, country):
        raise V.RegistryUnavailable("GLEIF unreachable: timeout")

    async def _edgar(name):
        return None                     # EDGAR answered; it has no such ticker

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    monkeypatch.setattr(V, "_edgar_search", _edgar)

    r = _run(V.handle_verify_company_record(name="Siemens Energy AG"))
    assert r.reason_code == "partial_lookup", (
        "GLEIF was unreachable and we reported not_found - the only global "
        "registry we consult never answered")
    assert r.result["status"] == "unavailable"
    assert r.retriable is True
    assert "NOT evidence" in r.human_message


def test_edgar_down_alone_still_answers_not_found_but_says_so(monkeypatch):
    """The reverse must NOT become unavailable: GLEIF is global and it
    answered. Losing EDGAR costs us US ticker enrichment, not the search."""
    async def _gleif_by_name(name, country):
        return []

    async def _edgar(name):
        raise V.RegistryUnavailable("SEC EDGAR returned HTTP 503")

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    monkeypatch.setattr(V, "_edgar_search", _edgar)

    r = _run(V.handle_verify_company_record(name="Nonexistent Holdings Ltd"))
    assert r.reason_code == "not_found"
    assert "SEC EDGAR" in r.result["sources_unavailable"]
    assert "partial search" in r.human_message


def test_both_down_is_unavailable(monkeypatch):
    async def _gleif_by_name(name, country):
        raise V.RegistryUnavailable("GLEIF unreachable")

    async def _edgar(name):
        raise V.RegistryUnavailable("SEC EDGAR unreachable")

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    monkeypatch.setattr(V, "_edgar_search", _edgar)

    r = _run(V.handle_verify_company_record(name="Apple Inc"))
    assert r.reason_code == "partial_lookup"
    assert set(r.result["sources_unavailable"]) == {"GLEIF", "SEC EDGAR"}


def test_non_us_country_with_gleif_down_is_unavailable(monkeypatch):
    """EDGAR is never even queried here, so sources_unavailable has one entry
    and the old count happened to get this case right. Kept so the fix cannot
    regress the case it already handled."""
    async def _gleif_by_name(name, country):
        raise V.RegistryUnavailable("GLEIF unreachable")

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    r = _run(V.handle_verify_company_record(name="Bosch GmbH", country="DE"))
    assert r.reason_code == "partial_lookup"


# --------------------------------------------------------------------------
# EDGAR LOOKUP: ABBREVIATIONS, AND REFUSING TO GUESS BETWEEN FILERS
# --------------------------------------------------------------------------

class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        return _FakeResp(self._payload)


def _edgar_with(entries, monkeypatch):
    import httpx
    payload = {str(i): e for i, e in enumerate(entries)}
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **kw: _FakeClient(payload))


def test_edgar_matches_across_a_corporate_abbreviation(monkeypatch):
    """SEC stores "Microsoft Corp"; GLEIF stores "MICROSOFT CORPORATION". The
    punctuation-insensitive match handled the Apple full stop and still missed
    this, so verify_company_record returned sec_cik: null for Microsoft while
    listing sec.gov as queried."""
    _edgar_with([{"title": "Microsoft Corp", "ticker": "MSFT",
                  "cik_str": 789019}], monkeypatch)
    hit = _run(V._edgar_search("Microsoft Corporation"))
    assert hit and hit["cik"] == "789019"


def test_edgar_still_refuses_a_near_name(monkeypatch):
    _edgar_with([{"title": "Apple Hospitality REIT, Inc.", "ticker": "APLE",
                  "cik_str": 1418121},
                 {"title": "Pineapple Financial Inc.", "ticker": "PAPL",
                  "cik_str": 1938046}], monkeypatch)
    assert _run(V._edgar_search("Apple Inc")) is None


def test_two_filers_alike_after_suffixes_are_reported_not_guessed(monkeypatch):
    """Picking one would attach a real CIK to the wrong company. Returning a
    bare None would say "SEC had nothing", which is a different and untrue
    answer - so the candidates come back instead."""
    _edgar_with([{"title": "Acme Corp", "ticker": "ACM", "cik_str": 111},
                 {"title": "Acme Inc.", "ticker": "ACMI", "cik_str": 222}],
                monkeypatch)
    hit = _run(V._edgar_search("Acme Limited"))
    assert hit and hit.get("ambiguous")
    assert {c["cik"] for c in hit["ambiguous"]} == {"111", "222"}
    assert "cik" not in hit


def test_the_ambiguity_reaches_the_receipt(monkeypatch):
    apple_gleif = {
        "id": "X", "attributes": {"lei": "X", "entity": {
            "legalName": {"name": "ACME LIMITED"}, "status": "ACTIVE",
            "jurisdiction": "US-DE", "legalAddress": {"country": "US"}},
            "registration": {"status": "ISSUED"}}}

    async def _gleif_by_name(name, country):
        return [apple_gleif]

    monkeypatch.setattr(V, "_gleif_by_name", _gleif_by_name)
    _edgar_with([{"title": "Acme Corp", "ticker": "ACM", "cik_str": 111},
                 {"title": "Acme Inc.", "ticker": "ACMI", "cik_str": 222}],
                monkeypatch)

    r = _run(V.handle_verify_company_record(name="Acme Limited"))
    assert r.result["sec_cik"] is None
    cand = r.result["unmerged_sec_match"]["sec_candidates"]
    assert len(cand) == 2
    assert "2 filers" in r.human_message
