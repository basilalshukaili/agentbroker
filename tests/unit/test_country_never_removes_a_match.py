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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.screen_sanctions as ss  # noqa: E402


def _screen(name, country=None):
    rec = asyncio.run(ss.handle_screen_sanctions(name=name, country=country))
    return rec.result or {}


def _has_db() -> bool:
    from storage.supabase_client import _get_config
    u, k = _get_config()
    return bool(u and k)


LISTED = "Saddam Hussein Al-Tikriti"          # OFAC + EU (IQ) + UK (IRAQ)


def test_a_deliberately_wrong_country_still_returns_the_match():
    """The whole point. Screening a listed Iraqi against country=FR must still
    report him, flagged as a country mismatch - never drop him."""
    if not _has_db():
        pytest.skip("no database config in this environment")

    right = _screen(LISTED, country="IQ")
    wrong = _screen(LISTED, country="FR")

    assert right.get("matched") is True
    assert wrong.get("matched") is True, (
        "a country mismatch turned a real sanctions match into a clean "
        "screen - this is the false negative the design exists to prevent")
    assert len(wrong.get("matches") or []) == len(right.get("matches") or []), (
        "a country mismatch removed matches from the result")


def test_the_mismatch_is_reported_rather_than_hidden():
    if not _has_db():
        pytest.skip("no database config in this environment")
    wrong = _screen(LISTED, country="FR")
    flags = [m.get("country_match") for m in wrong["matches"]
             if m["list"].startswith(("EU-", "UK-"))]
    assert flags and all(f is False for f in flags), (
        f"expected every EU/UK match flagged as a country mismatch, got {flags}")


def test_a_listing_with_no_country_is_unknown_not_a_mismatch():
    """None and False are different answers. Reporting "no country recorded"
    as a mismatch would tell a caller we checked and ruled it out."""
    if not _has_db():
        pytest.skip("no database config in this environment")
    r = _screen(LISTED, country="FR")
    ofac = [m for m in r["matches"] if "OFAC-SDN" in m["list"]]
    assert ofac, "OFAC match disappeared"
    assert ofac[0].get("country_match") is None, (
        "OFAC carries no country data, so its country_match must be null - "
        "not False, which would claim we had ruled the country out")


def test_the_response_does_not_claim_a_filter_was_applied():
    if not _has_db():
        pytest.skip("no database config in this environment")
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
