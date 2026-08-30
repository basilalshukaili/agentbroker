"""A screen that did not happen must never read as a clean one.

`matched: false` meant two opposite things - "we checked three lists and this
party is on none of them" and "we checked nothing" - and the sentence above it
opened "No matches on the screened lists" in both cases. Measured live:

    screen_sanctions("GRU")  -> matched: false
                                "No matches on the screened lists for 'GRU'."

GRU is listed on the EU and UK sanctions lists. The refusal (the name is
3 characters, too short for the old screenability rule) was disclosed further
down the payload, and the first sentence - the one a human skims and an LLM
summarises - said clean.

These tests are about the SHAPE of the answer, not the matching.
"""
from __future__ import annotations

import asyncio

import pytest

import core.screen_sanctions as ss


def _run(coro):
    return asyncio.run(coro)


class _Rows:
    """Stand in for the index so these tests do not need the network."""

    def __init__(self, by_list: dict):
        self.by_list = by_list

    async def select_rows_strict(self, table, filters=None, limit=None, **kw):
        filters = filters or {}
        code = filters.get("list_code")
        if "name_key" in filters:
            return [r for r in self.by_list.get(code, [])
                    if r["name_key"] == filters["name_key"]]
        return []                       # no superset hits

    async def select_rows(self, table, filters=None, limit=None, **kw):
        code = (filters or {}).get("list_code")
        return self.by_list.get(code, [])[:1]       # non-empty: index is loaded


@pytest.fixture
def _index(monkeypatch):
    rows = _Rows({
        "EU": [{"name_key": "gru", "tokens": ["gru"], "display_name": "GRU",
                "programme": "Russia", "etype": "ENTITY", "countries": ["RU"]}],
        "UK": [{"name_key": "gru", "tokens": ["gru"], "display_name": "GRU",
                "programme": "Russia", "etype": "ENTITY", "countries": ["RU"]}],
    })
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_strict", rows.select_rows_strict)
    monkeypatch.setattr(sb, "select_rows", rows.select_rows)

    async def _fresh(code):
        # DATE ONLY, exactly as _list_refreshed_at returns it in production
        # ([:10] of refreshed_at). A full ISO timestamp makes _days_since
        # raise ValueError -> age None -> the list is (correctly) reported as
        # unscreenable, so this fixture would be testing the outage path while
        # claiming to test a fresh index.
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    monkeypatch.setattr(ss, "_list_refreshed_at", _fresh)

    async def _no_ofac(name):
        return [], ["OFAC-SDN"], []

    monkeypatch.setattr(ss, "_call_ofac_sdn", _no_ofac)
    return rows


def test_a_short_listed_name_is_surfaced_not_reported_clean(_index):
    """GRU is on two of the three lists we screen. The old rule refused to
    look at all and reported no matches."""
    rc = _run(ss.handle_screen_sanctions("GRU"))
    res = rc.result

    assert res["screening_status"] == "not_screened", (
        "a name we could not fully screen is reporting a completed screen")
    cands = res.get("possible_matches_unverified") or []
    assert cands, (
        "GRU is listed on the EU and UK lists and nothing was surfaced - the "
        "caller is told, in effect, that it is clean")
    assert not rc.human_message.startswith("No matches"), (
        f"the headline still reads as clean: {rc.human_message[:80]}")
    assert "COULD NOT FULLY SCREEN" in rc.human_message


def test_a_short_name_is_never_asserted_as_a_finding(_index):
    """The other half of the bar. Surfacing is right; asserting is not - the
    rule exists because "Dave" was returned as an Al-Qaeda programme hit."""
    rc = _run(ss.handle_screen_sanctions("GRU"))
    assert rc.result["matched"] is False
    assert not rc.result["matches"]


def test_the_superset_query_stays_off_for_a_weak_name(monkeypatch, _index):
    """The exact lookup is what was re-enabled. The superset lookup - the one
    that reached "Isil (Da'esh) and Al-Qaeda" from the word "Dave" - must not
    run, or this fix reintroduces the bug it is downstream of."""
    calls = []
    inner = _index.select_rows_strict

    async def _spy(table, filters=None, limit=None, **kw):
        calls.append(dict(filters or {}))
        return await inner(table, filters, limit, **kw)

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_strict", _spy)

    # TWO TOKENS, both too short. A single-token weak name like "GRU" cannot
    # test this: the superset query is already skipped for any one-word query,
    # so the assertion would pass no matter what this fix did. "Li Na" is the
    # real shape - it is the name that was returned as an Iran-WMD finding.
    assert len(ss._normalize_name("Li Na").split()) == 2
    _run(ss.handle_screen_sanctions("Li Na"))
    supersets = [c for c in calls if any(str(v).startswith("cs.")
                                         for v in c.values())]
    assert not supersets, (
        f"a superset query ran for a weak name: {supersets}")


def test_status_distinguishes_the_four_outcomes(_index):
    """screening_status exists because `matched: false` could not."""
    assert _run(ss.handle_screen_sanctions("GRU")
                ).result["screening_status"] == "not_screened"
    assert _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd")
                ).result["screening_status"] == "clean"


def test_a_clean_screen_still_says_clean(_index):
    rc = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
    assert rc.result["screening_status"] == "clean"
    assert rc.result["matched"] is False
    assert rc.human_message.startswith("No matches on the screened lists")


def test_candidates_do_not_hide_behind_a_no_matches_headline(monkeypatch,
                                                             _index):
    """Screening "Rosneft" put three Rosneft entities in
    possible_matches_unverified under the sentence "No matches on the screened
    lists for 'Rosneft'". Both halves were true and the order was wrong."""
    _index.by_list["EU"] = [
        {"name_key": "rosneft", "tokens": ["rosneft"],
         "display_name": "ROSNEFT", "programme": "UKR", "etype": "ENTITY",
         "countries": ["RU"]}]
    rc = _run(ss.handle_screen_sanctions("Rosneft"))
    assert rc.result["screening_status"] == "candidates"
    assert not rc.human_message.startswith("No matches"), rc.human_message[:90]
    assert "No CONFIRMED match" in rc.human_message
