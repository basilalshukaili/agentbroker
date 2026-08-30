"""map_trade_restriction must not say "No party matches found" when it found
candidates, or when a party could not be screened.

screen_sanctions was fixed on 2026-08-30 so that an unscreenable name surfaces
exact whole-name candidates instead of reading as clean. This receipt sits one
layer ABOVE it and did not carry that through:

    map_trade_restriction(product=..., destination_country="RU",
                          parties=["GRU"])
      -> "ADVISORY: ... No party matches found."

GRU is on the EU and UK sanctions lists, and this tool's own payload held both
as unverified candidates. The gap was warned about further down the message;
the fact that two lists carry that exact name was reported nowhere in it.

A fix that stops at the layer where the bug was found is half a fix.
"""
from __future__ import annotations

import asyncio

import pytest

import core.map_trade_restriction as MT


def _run(coro):
    return asyncio.run(coro)


def _fake_screen(monkeypatch, **payload):
    """Stand in for screen_sanctions with a chosen result shape."""
    class _R:
        reason_code = payload.pop("_reason", "no_match")
        result = payload

    async def _screen(name, *a, **kw):
        return _R()

    import core.screen_sanctions as ss
    monkeypatch.setattr(ss, "handle_screen_sanctions", _screen)


def test_candidates_are_named_in_the_sentence(monkeypatch):
    _fake_screen(
        monkeypatch,
        matched=False,
        screening_status="not_screened",
        matches=[],
        sources_queried=["EU", "UK"],
        sources_unavailable=["EU (reduced screen: too short)"],
        possible_matches_unverified=[{"name": "GRU"}, {"name": "GRU"}],
        _reason="not_screened",
    )
    r = _run(MT.handle_map_trade_restriction(
        product="industrial pumps", origin_country="DE",
        destination_country="RU", parties=["GRU"]))

    msg = r.human_message
    assert "No party matches found." not in msg, (
        "the receipt reports no matches while holding two sanctions "
        "candidates for the same party")
    assert "unverified name candidate" in msg
    assert "GRU (2)" in msg, "the candidate count is not surfaced"
    assert r.result["parties_fully_screened"] is False


def test_a_genuinely_clean_party_still_reads_clean(monkeypatch):
    """The fix must not turn every clean screen into a scary one."""
    _fake_screen(
        monkeypatch,
        matched=False,
        screening_status="clean",
        matches=[],
        sources_queried=["OFAC", "EU", "UK"],
        sources_unavailable=[],
        possible_matches_unverified=[],
    )
    r = _run(MT.handle_map_trade_restriction(
        product="industrial pumps", origin_country="DE",
        destination_country="RU", parties=["Definitely Nobody Ltd"]))
    assert "No party matches found on the screens that ran" in r.human_message
    assert r.result["parties_fully_screened"] is True


def test_the_screening_status_is_carried_through(monkeypatch):
    """`matched: false` is false both for "screened and clean" and for
    "nothing screened". Callers should not have to re-derive which."""
    _fake_screen(
        monkeypatch,
        matched=False,
        screening_status="not_screened",
        matches=[],
        sources_queried=[],
        sources_unavailable=["EU (unreachable)"],
        possible_matches_unverified=[],
    )
    r = _run(MT.handle_map_trade_restriction(
        product="pumps", destination_country="RU", parties=["Someone"]))
    party = r.result["parties_screened"][0]
    assert party["screening_status"] == "not_screened"


def test_a_screening_error_is_not_shaped_like_a_clean_screen(monkeypatch):
    """The except-branch used to return a party dict identical to a cleared
    one except for an `error` key nothing downstream had to read."""
    async def _boom(name, *a, **kw):
        raise RuntimeError("upstream exploded")

    import core.screen_sanctions as ss
    monkeypatch.setattr(ss, "handle_screen_sanctions", _boom)

    r = _run(MT.handle_map_trade_restriction(
        product="pumps", destination_country="RU", parties=["Someone"]))
    party = r.result["parties_screened"][0]
    assert party["screening_status"] == "not_screened"
    assert party["screening_complete"] is False
    assert r.result["parties_fully_screened"] is False
    assert "WARNING" in r.human_message
