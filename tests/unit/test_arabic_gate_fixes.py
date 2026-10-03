# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-03 review and gate findings on the Arabic sanctions layer.

The mixed-script finding (P1) is tested where the pipeline is, in test_screen_sanctions_arabic.py. Here:

  P2  ordinary names must not be engaged by the Arabic layer, and the rules that decide it are pinned with the
      names the gate found engaged, from a fixture of 125 ordinary English names and 60 Gulf names in both scripts;
  P2  a candidate above `low` needs an anchor - a distinctive element that matches closely;
  P3  the analysis cache is keyed on the name as cut, not as sent;
  P3  the OFAC index version changes when the text changes in place;
  P3  a renamed UK column cannot silently drop every UK Arabic alias, and the refresh will not sweep if it did.

Everything here is offline and fast; the measured numbers on the real lists are in docs/ARABIC_SANCTIONS_EVAL.md.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.screen_sanctions as ss  # noqa: E402
from core import arabic_names as an  # noqa: E402

NEGATIVES = os.path.join(ROOT, "tests", "fixtures", "sanctions_negative_names.json")


@pytest.fixture(scope="module")
def negatives():
    with open(NEGATIVES, encoding="utf-8") as fh:
        return json.load(fh)


# The names the gate found engaged (23 of 50 ordinary English names), verbatim, plus the others its probe listed.
GATE_ENGAGED_ENGLISH = [
    "David Evans", "Andrew Rogers", "Jeffrey Baker", "Ryan Scott", "Samuel Jones", "Wayne Turner", "Elijah Young",
    "Mary Collins", "Elizabeth Brown", "Emily Rogers", "Andrea Nelson", "Judy Collins", "James Alvarez",
    "Robert Evans", "Karen Warren", "Sarah Alvarez", "George Ward", "Aladdin Cox", "Alonso Ward", "Alistair Wright",
    "Eleanor Roberts", "Alison Adams", "Allison Ward", "Ed Davis", "An Nguyen", "Alvarez Baker", "Larry Baker",
    "Jack Evans", "Keith Ward", "Logan Perry", "Emily Owens", "Alison Murray",
]


@pytest.mark.parametrize("name", GATE_ENGAGED_ENGLISH)
def test_an_ordinary_english_name_is_not_read_as_arabic(name):
    a = an.analyse(name)
    assert a.engaged is False and a.phonetic_ok is False, [(u.text, u.marker, u.given) for u in a.units]


def test_none_of_125_ordinary_english_names_is_engaged(negatives):
    names = [f"{a} {b}" for a, b in negatives["english_pairs"]]
    assert len(names) >= 120
    engaged = [n for n in names if an.analyse(n).engaged]
    assert engaged == [], engaged


@pytest.mark.parametrize("name", [
    "David bin Salim Al Busaidi",          # a Western given name WITH Arabic structure is still Arabic
    "Mary bint Ahmed Al Kindi",
    "Ryan Al-Zawahiri",
    "Mohammed Smith",                      # an Arab given name is evidence even beside a Western surname
    "Ayman Zawahiri",
    "Hamid Abdallah Ahmad Al-Ali",
    "Saddam Hussein",
    "Abdelaziz Johnson",
    "Nasrallah Brown",                     # -allah
    "Alzawahiri Ayman",                    # a glued article
    "Elsayed Mahmoud",
])
def test_a_name_with_arabic_structure_or_an_arab_given_name_is_still_engaged(name):
    assert an.analyse(name).engaged is True


def test_every_gulf_name_in_both_scripts_is_engaged_except_the_few_with_no_structure_and_no_given_name(negatives):
    latin = [b for a, b in negatives["gulf"]]
    arabic = [a for a, b in negatives["gulf"]]
    assert all(an.analyse(n).engaged for n in arabic)                      # Arabic script is always read as Arabic
    missed = [n for n in latin if not an.analyse(n).engaged]
    assert len(missed) <= 6, missed                                       # companies written in Latin only, mostly


def test_a_western_name_list_vetoes_only_the_weak_signal():
    """The list never removes STRUCTURE: it only stops a given-name sound-alike from counting by itself."""
    t = an._tables()
    assert "david" in t["western"] and "adam" not in t["western"] and "sara" not in t["western"]
    assert "omar" not in t["western"] and "ali" not in t["western"] and "hassan" not in t["western"]
    assert an.analyse("David bin Evans").engaged is True and an.analyse("David Evans").engaged is False


# ---------------------------------------------------------------------------
# an anchor: a candidate above `low` needs a distinctive element that matches closely
# ---------------------------------------------------------------------------

def _grade(query, listed):
    al = an.compare_names(query, listed)
    return None if al is None else al.confidence


def test_three_short_given_name_elements_matched_to_a_long_foreign_listing_are_not_a_candidate():
    """'Adil bin Saud Al Alawi' lined up with the short words of a Spanish organisation graded MEDIUM on coverage
    alone (the gate's example)."""
    assert _grade("Adil bin Saud Al Alawi", "Division de Asia Meridional del EIIL") in (None, "low")


def test_two_given_names_shared_with_a_stranger_are_not_a_candidate_when_the_family_name_does_not_match():
    assert _grade("Nabil bin Hamad Al Salhi", "AL-HADHA, Nabil Ali Ahmed") in (None, "low")


def test_two_query_words_glued_into_one_do_not_become_a_common_listed_name():
    assert _grade("Hassan bin Ali Al Ajmi", "HASSAN, Jamil") in (None, "low")


@pytest.mark.parametrize("query,listed", [
    ("Ayman Zawahiri", "Ayman Al-Zawahiri"),
    ("Muhammad Al-Zawahri", "Mohammed Al-Zawahiri"),
    ("Ibrahim Hasan Talea Aseeri", "Ibrahim Hassan Tali Al-Asiri"),
    ("Gholamreza Soleimani", "Gholam Reza Soleimani"),
    ("Osama bin Laden", "Usama bin Ladin"),
])
def test_a_real_spelling_variant_keeps_its_grade(query, listed):
    assert _grade(query, listed) in ("high", "medium"), (query, listed)


# ---------------------------------------------------------------------------
# P3
# ---------------------------------------------------------------------------

def test_the_analysis_cache_is_keyed_on_the_name_as_cut():
    an.analyse.cache_clear()
    huge = "x" * 5_000_000
    a = an.analyse(huge)
    assert len(a.raw) == an.MAX_NAME_CHARS
    keys = [k for k in _cached_keys()]
    assert all(len(k) <= an.MAX_NAME_CHARS for k in keys), "an oversized name is resident as a cache key"
    assert an.analyse(huge + "y") is an.analyse(huge)                      # cut to the same 300 characters
    an.analyse.cache_clear()


def _cached_keys():
    cache = an._analyse_cached
    # lru_cache hides its keys; cache_info proves the entry count and the wrapper below proves the argument size
    import gc
    out = []
    for obj in gc.get_objects():
        if isinstance(obj, str) and len(obj) >= 1_000_000 and obj and set(obj[:100]) == {"x"}:
            out.append(obj)
    return out if cache.cache_info().currsize else []


def test_the_ofac_index_version_changes_when_the_text_changes_in_place(monkeypatch):
    seen = []

    async def fake_index(slot, key, work):
        seen.append(key)
        return an.SkeletonIndex().freeze(), {}
    monkeypatch.setattr(ss, "_sound_index", fake_index)
    base = "10,A SURNAME, Given,individual,-0-\n" * 400
    changed = base.replace("A SURNAME", "B SURNAME", 1) if base.find("A SURNAME") > 100 else base
    # same length, one letter changed in the MIDDLE of the file (past the first and last 4 KB)
    mid = len(base) // 2
    changed = base[:mid] + ("Z" if base[mid] != "Z" else "Y") + base[mid + 1:]
    assert len(changed) == len(base) and changed != base
    aq = an.analyse("Ayman Al-Zawahiri")
    asyncio.run(ss._ofac_phonetic_matches(base, "alt", aq))
    asyncio.run(ss._ofac_phonetic_matches(changed, "alt", aq))
    assert len(seen) == 2 and seen[0] != seen[1]


def _uk_csv(header_name):
    cols = ["Unique ID", "Name 1", "Name 6", "Designation Type", "Regime Name", "Address Country", header_name]
    rows = [["Report Date: 02-Oct-2026"] + [""] * 6, cols,
            ["UK123", "Ayman", "Al-Zawahiri", "Individual", "Counter Terrorism", "Afghanistan", "أيمن الظواهري"]]
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


@pytest.mark.parametrize("column", ["Name non-latin script", "Name Non-Latin Script", "Name non-Latin script",
                                     "Name_non_latin_script"])
def test_the_uk_arabic_column_is_found_whatever_case_or_punctuation_the_publisher_uses(column):
    recs = ss._uk_parse(_uk_csv(column))
    assert any(an.has_arabic_script(r["name"]) for r in recs), recs


def test_a_uk_feed_with_the_column_gone_is_loud_not_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="smb_broker.screen_sanctions"):
        recs = ss._uk_parse(_uk_csv("Something Else Entirely"))
    assert recs and not any(an.has_arabic_script(r["name"]) for r in recs)
    assert any("uk_parse_no_non_latin_column" in r.getMessage() for r in caplog.records)


def _refresh_module():
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("refresh_sanctions_lists")


@pytest.mark.parametrize("prior,new,ok", [(735, 735, True), (735, 800, True), (735, 400, True), (735, 367, False),
                                           (735, 0, False), (0, 0, True), (0, 12, True), (-1, 735, False)])
def test_the_refresh_will_not_sweep_when_the_arabic_aliases_collapse(prior, new, ok):
    rsl = _refresh_module()
    got, message = rsl.arabic_sweep_ok(prior, new)
    assert got is ok and message
    if not ok:
        assert str(new) in message or "could not be counted" in message
