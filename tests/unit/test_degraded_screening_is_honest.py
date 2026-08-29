"""
What the screener may assert when its calibrated source is dark.

THE INCIDENT, 2026-08-29. An adversarial review called the live endpoint and got:

    "Mohammed Ali"         -> MATCH, score 1.00, programme US-TERR
    "Maria Garcia"         -> MATCH, score 1.00, programme US-NARCO
    "Star Trading LLC"     -> MATCH, score 1.00, programme US-DRC
    "Delta Services Group" -> MATCH, score 1.00, programme US-RUSHAR

Ordinary names, at MAXIMUM confidence, on terrorism and narcotics programmes.

Four rounds of fixes that afternoon each closed one specific false positive -
the apostrophe, "Acme Trading", "Gulf", the two-letter query - and not one of
them touched the cause. Our local matcher compares WORD SETS, so any short name
whose distinctive words appear anywhere inside one of ~17,000 long listed names
scores perfectly. Patching individual strings was never going to end.

THE ROOT CAUSE was operational, not algorithmic: our OpenSanctions key had
exceeded its MONTHLY limit (HTTP 429), so the calibrated matcher - which uses
token frequency across the whole corpus and can therefore tell "Ali Mohammed"
from "Zarubezhneft" - never answered. Every one of those results came from the
local OFAC-CSV fallback alone, which has no such data and cannot acquire it.

THE RULE THESE TESTS ENFORCE: when the calibrated source did not answer, the
fallback may SURFACE candidates but may not ASSERT findings. The only
relationship a word-set matcher can claim on its own is an identical token set.
Under-claiming costs the caller one lookup. Over-claiming tells somebody that
Maria Garcia is a narcotics trafficker.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.screen_sanctions import handle_screen_sanctions  # noqa: E402


def screen(name: str) -> dict:
    r = asyncio.run(handle_screen_sanctions(name=name))
    return (r.result if hasattr(r, "result") else r.get("result")) or {}


# --------------------------------------------------------------------------
# MUST NOT BE ASSERTED — the live false positives
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Maria Garcia",
    "Star Trading LLC",
    "Delta Services Group",
    "Atlas Trading Company",
    "Phoenix Trading LLC",
    "Horizon Group",
    "Ocean Shipping Company",
])
def test_an_ordinary_name_is_not_reported_as_a_sanctions_finding(name):
    """Each of these returned matched=true at score 1.00 on the live endpoint.

    A subset overlap - the query's words appearing inside a longer listed name -
    is the shape that produces both "Rosneft" inside "ROSNEFT TRADING S.A." and
    "Star Trading LLC" inside "Star Dragon Corporation Limited". Without
    frequency data the two are indistinguishable, so neither may be asserted.
    """
    d = screen(name)
    assert d.get("matched") is False, (
        f"{name!r} is reported as a sanctions match - an ordinary business or "
        f"person would be refused service on our say-so")


def test_candidates_are_surfaced_rather_than_hidden():
    """Suppressing them would be the opposite failure: a screener that quietly
    drops near-matches is worse than one that over-reports, because the caller
    cannot tell it happened. They move to a field that is clearly not a finding.
    """
    d = screen("Star Trading LLC")
    assert d.get("possible_matches_unverified"), (
        "near-matches were dropped entirely instead of surfaced for review")
    assert "not sanctions findings" in (d.get("degraded") or "").lower() or \
           "NOT sanctions findings" in (d.get("degraded") or "")


def test_the_human_message_says_the_screening_was_incomplete():
    """A caller reads the sentence, not the fields. "No match" from a degraded
    screen means something much weaker than "no match" from a complete one."""
    d = screen("Star Trading LLC")
    # the receipt's own narrative must carry the warning
    assert d.get("degraded"), "nothing marks this result as degraded"


# --------------------------------------------------------------------------
# MUST STILL BE ASSERTED — identical token sets
# --------------------------------------------------------------------------

def test_an_identical_name_in_a_different_order_still_matches():
    """Sanctions lists write names in every order - Kim Jong Un is listed as
    "Jong Un Kim". Exact string equality would miss real entities, which is why
    the confident bar is the token SET rather than the string."""
    d = screen("Kim Jong Un")
    assert d.get("matched") is True, (
        "a head of state on multiple sanctions lists was not matched")


def test_a_common_name_collision_is_still_surfaced_as_a_match():
    """"Mohammed Ali" against a listed "Ali Mohammed" IS a genuine collision and
    a screener must show it. What changed is not that we stopped surfacing it -
    it is that we stopped dressing SUBSET overlaps as findings too. Every result
    carries the verify-before-acting disclaimer."""
    # WAS "Mohammed Ali". That name is on OpenSanctions' 40-list aggregate but
    # NOT on OFAC SDN, and we now source OFAC from Treasury directly (their
    # dataset is CC-BY-NonCommercial and we are commercial). Real coverage
    # loss, honestly reflected: this uses a listed entity whose name we
    # deliberately match in any word order.
    d = screen("Jong Un Kim")
    assert d.get("matched") is True
    assert d.get("disclaimer"), "a name collision was asserted with no disclaimer"


# --------------------------------------------------------------------------
# The operational fact behind all of it
# --------------------------------------------------------------------------

def test_the_result_names_which_sources_actually_answered():
    """The whole incident traces to the calibrated source being silently
    unavailable while the tool kept answering confidently. Which sources ran
    must always be legible in the receipt."""
    d = screen("Rosneft")
    assert d.get("sources_queried") or d.get("lists_screened")
    assert "sources_unavailable" in d or d.get("matched") is not None
