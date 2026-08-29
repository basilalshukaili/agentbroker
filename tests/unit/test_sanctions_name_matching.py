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
    # Observed LIVE after the first fix deployed: the candidate reduced to a
    # single distinctive word, so scoring from its side was perfect.
    ("Bright Star Trading Company", "GLOBAL STAR"),
    ("Gulf General Trading LLC", "Pars General Trading Co"),
    # Also observed live: both names reduced to the single place-word "Gulf".
    ("Gulf General Trading LLC", "Gulf General Contracting Limited"),
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

@pytest.mark.parametrize("name", [
    "Bank Melli Iran",
    "Korea Mining Development Trading Corporation",
    "Syrian Arab Airlines",
])
def test_country_names_stay_distinctive(name):
    """Regional words like "Gulf" are noise; COUNTRY names are evidence.

    Iran, Korea, Syria and Russia appear in the names of heavily sanctioned
    entities and carry real signal. Sweeping them into the generic set along
    with "Gulf" and "Eastern" would remove evidence rather than noise - so the
    generic list stops at regional and directional words, and this pins that
    line.
    """
    assert matches(name, name)


def test_a_name_made_only_of_generic_words_still_behaves():
    """"General Trading Company" has no distinctive words at all. The scorer
    must not divide by zero, and an exact match must still be a match."""
    assert matches("General Trading Company", "General Trading Company")


def test_scoring_is_asymmetric_on_purpose():
    """The query and the candidate are NOT interchangeable, and an earlier
    version of this test asserted they were.

    The question a screener asks is "is the entity in front of me on the list",
    not "do these two strings resemble each other". Symmetric scoring is what
    produced the live false positive "Bright Star Trading Company" matching
    "GLOBAL STAR": the candidate reduced to one distinctive word, so scoring
    from the candidate's side was perfect and dragged the average over the
    line. Measuring only how much of the QUERY appears in the listed name
    removes that whole class.
    """
    forward = _word_match_score("Bright Star Trading Company", "GLOBAL STAR")
    backward = _word_match_score("GLOBAL STAR", "Bright Star Trading Company")
    assert forward < backward, "direction no longer matters - the asymmetry is gone"
    assert not matches("Bright Star Trading Company", "GLOBAL STAR")


def test_a_short_query_is_generous_and_that_is_the_intended_trade():
    """Screening one distinctive word flags every listed name containing it.

    Pinned rather than fixed, because it is a deliberate choice: for a
    sanctions tool a flagged name costs one verification and a missed one can
    be a sanctions breach. It is only safe because generic words are removed
    first - the generosity applies to distinctive tokens, never to "Trading".
    """
    assert matches("Rosneft", "OJSC Rosneft Oil Company")
    assert not matches("Trading", "ONCU Trading L.L.C.")


def test_no_overlap_scores_zero():
    assert _word_match_score("Muscat Coffee House", "Pyongyang Metal Works") == 0.0


def test_empty_input_is_not_a_match():
    assert _word_match_score("", "Kim Jong-un") == 0.0
    assert _word_match_score("Kim Jong-un", "") == 0.0
