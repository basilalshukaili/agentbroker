"""
What a sanctions match may and may not be made of.

FOUND IN PRODUCTION 2026-08-29, by calling our own live endpoint as a stranger
agent would. Screening the invented company "Acme Trading LLC" returned:

    MATCH FOUND for 'Acme Trading LLC': 'ONCU Trading L.L.C.' on OFAC-SDN
    (score=0.67, program=US-IRAN)

OpenSanctions itself returns ZERO results for "Acme Trading". We manufactured
that hit. The arithmetic was {acme, trading, llc} against {oncu, trading, llc}:
two of three words overlapped, 0.67, over the 0.60 threshold. The two matching
tokens were a generic activity word and a legal form. Nothing about the
identity matched at all.

WHY THIS IS THE WORST FAILURE MODE FOR THIS PARTICULAR TOOL. A false negative
lets one bad actor through. A false positive that fires on "<anything> Trading
LLC" tells an AI agent that an ordinary business appears on a US-Iran sanctions
programme - and an agent acting on that refuses a legitimate customer, or
escalates to a human who then distrusts every result we give. At scale, that
makes the screening worse than none: it trains the caller to ignore us.

The fix also had to avoid the opposite error. Removing legal forms from the
score initially MISSED "Rosneft" against "OJSC Rosneft Oil Company" and
"Sberbank" against "Sberbank of Russia PJSC" - false negatives on two of the
most-sanctioned entities in the world, introduced by a false-positive fix. Both
directions are pinned below.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.screen_sanctions import (  # noqa: E402
    _MATCH_THRESHOLD_OFAC,
    _word_match_score,
)


def matches(query: str, candidate: str) -> bool:
    return _word_match_score(query, candidate) >= _MATCH_THRESHOLD_OFAC


# --------------------------------------------------------------------------
# MUST NOT MATCH — the false positives that started this
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query,candidate", [
    # The exact pair observed on the live endpoint.
    ("Acme Trading LLC", "ONCU Trading L.L.C."),
    ("Bright Star Trading Company", "Star Sapphire Trading Company Limited"),
    ("Gulf General Trading LLC", "Pars General Trading Co"),
    ("Al Noor Enterprises", "Al Rayan Enterprises Ltd"),
    # A place name is not an identity.
    ("Muscat Coffee House", "Muscat Trading LLC"),
    ("Coffee Bean Trading", "Muscat Coffee House"),
])
def test_generic_words_alone_cannot_make_a_match(query, candidate):
    """Legal forms and activity words may APPEAR in a name; they cannot be what
    the match is made of. Two companies sharing "Trading" and "LLC" have
    nothing in common."""
    assert not matches(query, candidate), (
        f"{query!r} matched {candidate!r} at {_word_match_score(query, candidate):.2f} "
        f"- an ordinary business would be reported as sanctioned")


# --------------------------------------------------------------------------
# MUST MATCH — the false negatives the fix nearly introduced
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query,candidate", [
    # Each of these BROKE when legal forms were first removed from scoring.
    ("Rosneft", "OJSC Rosneft Oil Company"),
    ("Sberbank", "Sberbank of Russia PJSC"),
    ("Zarubezhneft", "Zarubezhneft OAO"),
    ("Gazprombank", "Joint Stock Company Gazprombank"),
    # word order
    ("Bank Sepah", "Sepah Bank"),
    # punctuation and hyphenation
    ("Kim Jong Un", "Kim Jong-un"),
    # a partial personal name against a fuller listed one
    ("Ahmed Hassan", "Ahmed Hassan Mohammed Ali"),
    ("Wagner Group", "Wagner Group"),
    ("Islamic Revolutionary Guard Corps", "Islamic Revolutionary Guard Corps"),
])
def test_a_distinctive_name_still_matches_its_registered_form(query, candidate):
    """Russian and CIS entities are among the most heavily sanctioned in the
    world and are almost always listed with a legal form attached. Treating
    OAO/PJSC/OJSC as distinctive words diluted the score below threshold and
    MISSED them - a false-positive fix turning into a false-negative generator
    aimed exactly at the entities that matter most."""
    assert matches(query, candidate), (
        f"{query!r} did NOT match {candidate!r} "
        f"(scored {_word_match_score(query, candidate):.2f}) - a real sanctioned "
        f"entity would pass screening")


# --------------------------------------------------------------------------
# The properties behind those cases
# --------------------------------------------------------------------------

def test_a_name_made_only_of_generic_words_still_behaves():
    """"General Trading Company" has no distinctive words at all. The scorer
    must not divide by zero, and an exact match must still be a match."""
    assert matches("General Trading Company", "General Trading Company")


def test_scoring_is_symmetric():
    """Which side is the query must not change the verdict - callers pass names
    in either order and a list entry is not privileged."""
    for a, b in [("Rosneft", "OJSC Rosneft Oil Company"),
                 ("Acme Trading LLC", "ONCU Trading L.L.C.")]:
        assert abs(_word_match_score(a, b) - _word_match_score(b, a)) < 1e-9


def test_no_overlap_scores_zero():
    assert _word_match_score("Muscat Coffee House", "Pyongyang Metal Works") == 0.0


def test_empty_input_is_not_a_match():
    assert _word_match_score("", "Kim Jong-un") == 0.0
    assert _word_match_score("Kim Jong-un", "") == 0.0
