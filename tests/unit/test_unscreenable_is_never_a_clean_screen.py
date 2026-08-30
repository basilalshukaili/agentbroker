"""Two failures found by an adversarial review of the live service.

Both were returned to real callers, on the deployed product, and both are the
same shape: a name the matcher CANNOT handle producing a confident answer.

1. FALSE ACCUSATION. "Dave" came back as matched=true against a list entry
   literally named Dave, carrying the programme "Isil (Da'esh) and Al-Qaeda".
   So did "Said" (TERR), "Universal" (RUSSIA-EO14024), "East" and "OOO".
   The rows are genuine; one word is simply not enough to say WHICH Dave.

2. FALSE CLEAN SCREEN. "PUTIN Vladimir Vladimirovich" written in Cyrillic
   returned matched=false, reason_code=no_match, and claimed all three lists
   had been screened - because _normalize_name strips to [a-z0-9 ], leaving no
   tokens, and a no-token query returned "no matches, nothing unavailable".
   Total confidence, zero coverage, on exactly the populations these lists are
   full of.

The rule both tests defend: a name we cannot screen must be reported as NOT
SCREENED. Never as clean, never as a finding.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.screen_sanctions as ss  # noqa: E402


def _screen(name):
    rec = asyncio.run(ss.handle_screen_sanctions(name=name))
    return rec, (rec.result or {})


def _has_db():
    from storage.supabase_client import _get_config
    u, k = _get_config()
    return bool(u and k)


@pytest.mark.parametrize("name", ["Dave", "Said", "Universal", "East", "OOO"])
def test_a_single_or_generic_word_is_never_a_finding(name):
    if not _has_db():
        pytest.skip("no database config in this environment")
    _, r = _screen(name)
    assert r.get("matched") is False, (
        f"{name!r} was reported as a sanctions MATCH. One word - or a word "
        f"that is only a legal form - identifies nobody, and a programme name "
        f"attached to it is a false accusation about a real person.")


@pytest.mark.parametrize("name", [
    "ПУТИН Владимир",   # Cyrillic
    "سعيد",                                                           # Arabic
    "中国核工业",                                                     # Chinese
])
def test_a_non_latin_name_is_reported_as_not_screened(name):
    """The dangerous direction. We cannot match these at all today, so the
    only honest answer is to say so - not to report a clean screen."""
    if not _has_db():
        pytest.skip("no database config in this environment")
    rec, r = _screen(name)
    assert r.get("matched") is False
    assert not (r.get("lists_screened") or []) or all(
        "unavailable" in x.lower() for x in r["lists_screened"]), (
        "a name we cannot match was reported as SCREENED against real lists")
    unavail = r.get("sources_unavailable") or []
    assert unavail, (
        "a name with no matchable characters produced NO unavailability "
        "notice - the caller cannot tell this from a genuine clean screen")
    assert len(unavail) >= 3, (
        f"only {len(unavail)} source(s) reported as unscreened; all three "
        f"(OFAC, EU, UK) failed to screen this name and all three must say so")
    assert rec.reason_code == "partial_screening", (
        "reason_code must not be 'no_match' when nothing was actually "
        "screened - that is the field an agent branches on")


def test_a_real_multiword_name_still_matches():
    """Guard the guard: if the rules above also suppressed genuine findings,
    every test here would pass for the wrong reason."""
    if not _has_db():
        pytest.skip("no database config in this environment")
    _, r = _screen("Saddam Hussein Al-Tikriti")
    assert r.get("matched") is True
    lists = {m["list"].split(" ")[0] for m in r["matches"]}
    assert "OFAC-SDN" in lists
    assert any(x.startswith("EU-") for x in lists), lists
    assert any(x.startswith("UK-") for x in lists), lists
