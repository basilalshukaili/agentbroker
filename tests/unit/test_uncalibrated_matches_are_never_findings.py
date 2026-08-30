"""
Our own name matcher must never produce a sanctions FINDING.

THE DISTINCTION THIS FILE DEFENDS. Two matchers feed `screen_sanctions`:

  * OpenSanctions' API, which is CALIBRATED - it knows "Ali Mohammed" is an
    extremely common name and "Zarubezhneft" is not, so the same similarity
    score means different things for each.
  * `_word_match_score` in core/screen_sanctions.py, which is word overlap with
    no frequency data whatsoever.

The second one scores, measured:

    1.00  'Maria Garcia'     vs 'GARCIA MARIA Isabel'
    1.00  'John Smith'       vs 'SMITH JOHN Robert'
    1.00  'Star Trading LLC' vs 'Star Dragon Corporation'   <- a different company
    0.50  'Mohamed Ali'      vs 'Muhammad Ali'              <- a real variant, MISSED

Both directions are wrong at once, which is the worst combination in a
strict-liability domain: it would tell an ordinary person they are on a US
narcotics programme at score 1.00, and miss a genuine transliteration.

WHY THIS FILE EXISTS RATHER THAN A COMMENT. The strict filter that prevents
this used to run only when `degraded` was true - and `degraded` was true on
every call only because `authoritative_ran` was ALWAYS FALSE, comparing the
literal "OpenSanctions" against lowercase URLs like
"https://api.opensanctions.org/...". The tool behaved well for a reason nobody
intended.

That made it a booby trap. The obvious one-line fix to the casing - which looks
like tidying - would have switched the filter off whenever OpenSanctions
answered, and started publishing raw word-overlap as findings. A safety that
depends on a bug is not a safety, and the person who "fixes" the bug gets
blamed for a defect they could not have seen.

So the filter is now tied to WHERE A MATCH CAME FROM, not to a flag, and the
last test here fails if anyone re-couples them.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import screen_sanctions as ss  # noqa: E402


def screen(name: str):
    receipt = asyncio.run(ss.handle_screen_sanctions(name=name))
    return receipt, (receipt.result or {})


# Ordinary names that collide with listed entries only by word overlap. None of
# these people are sanctioned; all of them share tokens with someone who is.
INNOCENT = [
    "Maria Garcia",
    "Star Trading LLC",
    "John Smith Consulting",
]


@pytest.mark.parametrize("name", INNOCENT)
def test_word_overlap_alone_is_never_reported_as_a_match(name):
    receipt, result = screen(name)
    assert result.get("matched") is not True, (
        f"{name!r} was reported as MATCHED on word overlap. Our matcher has no "
        f"name-frequency data, so a shared token is a coincidence, not "
        f"evidence. Matches: {[m.get('name') for m in result.get('matches') or []]}")
    assert receipt.reason_code != "clear", (
        "a screen that could not consult the calibrated source must not read "
        "as a clearance")


def test_a_real_listed_entity_is_still_found():
    """THE OTHER DIRECTION. A filter that returns nothing is not safe, it is
    useless - and useless is its own compliance failure."""
    receipt, result = screen("Kim Jong Un")
    assert result.get("matched") is True, (
        "a genuinely listed entity must still be found - suppressing every "
        "match is not caution, it is a broken screen")
    names = " ".join(m.get("name", "") for m in result.get("matches") or [])
    # Treasury writes names uppercase ("KIM, Jong Un"), so compare case-insensitively.
    assert "kim" in names.lower()


def test_matches_carry_their_provenance():
    """Downstream cannot apply different rules to calibrated and uncalibrated
    matches unless the match says which it is."""
    _, result = screen("Kim Jong Un")
    matches = result.get("matches") or []
    assert matches, "expected at least one match to inspect"
    for m in matches:
        assert m.get("_matcher") in ("opensanctions_calibrated",
                                     "local_word_overlap"), (
            f"match {m.get('name')!r} carries no _matcher tag, so nothing "
            f"downstream can tell evidence from coincidence")


def test_the_strict_filter_does_not_depend_on_the_degraded_flag():
    """THE BOOBY TRAP, PINNED.

    The filter used to run only under `degraded`, which was true only because
    of a case-sensitivity bug. Correcting that bug would have disarmed it.

    Here we force the non-degraded path - pretending the calibrated source
    answered - and assert that our own matcher's coincidences are STILL not
    reported as findings. If someone re-couples the filter to the flag, this
    goes red.
    """
    real = ss._word_match_score

    # Make our uncalibrated matcher maximally trigger-happy, so that anything
    # relying on its score alone will certainly produce a "finding".
    ss._word_match_score = lambda a, b: 1.0
    try:
        _, result = screen("Maria Garcia")
    finally:
        ss._word_match_score = real

    for m in result.get("matches") or []:
        assert m.get("_matcher") != "local_word_overlap", (
            "an uncalibrated word-overlap hit was promoted to a MATCH. The "
            "strict filter must apply to our own matcher regardless of whether "
            "the calibrated source also answered.")


def test_no_match_can_bypass_the_strict_token_set_filter():
    """The successor to the authoritative-predicate guard.

    HISTORY, because it is the reason this test exists at all. A flag called
    `authoritative_ran` decided whether the strict filter ran. It compared
    "OpenSanctions" against strings that were URLs, case-sensitively, so it was
    False on every call - the filter ran always, and the tool behaved well for
    a reason nobody intended. The obvious one-line casing fix would have
    switched the safety OFF. That is why the filter was re-tied to match
    provenance rather than to a flag.

    OpenSanctions is now gone entirely, so there is no calibrated matcher and
    no legitimate reason for any match to skip the check. This asserts that -
    both in the source, so an escape hatch cannot be reintroduced quietly, and
    in behaviour, which is what actually protects a customer.
    """
    import inspect
    src = inspect.getsource(ss)

    # The flag must be GONE, not merely always-false. A dead conditional is
    # how the original bug hid for weeks.
    assert "authoritative_ran" not in src, (
        "the retired predicate is back; if a calibrated source is ever added, "
        "give it its own explicit test rather than resurrecting this flag")
    assert "opensanctions_calibrated" not in src, (
        "a matcher tag exists for a matcher we do not have - anything "
        "comparing against it is a permanently-false branch")

    # Behaviour: a SUBSET overlap must never be reported as a finding, however
    # it entered. "Star Trading LLC" shares a word with plenty of listed
    # entities and is not one of them.
    for name in ("Star Trading LLC", "Atlas Trading Company", "Horizon Group",
                 "Maria Garcia"):
        _, d = screen(name)
        assert d.get("matched") is False, (
            f"{name!r} was reported as a MATCH on a partial word overlap - "
            f"this is the failure the filter exists to prevent")
        for m in d.get("matches") or []:
            assert (set(ss._normalize_name(m["name"]).split())
                    == set(ss._normalize_name(name).split())), (
                f"{m['name']!r} was asserted as a finding for {name!r} without "
                f"an identical token set")



# ---------------------------------------------------------------------------
# THE FALSE-NEGATIVE HALF, which is the more dangerous one
# ---------------------------------------------------------------------------
#
# A false positive is discovered immediately and is merely embarrassing. A
# false negative is discovered by a regulator, or never.
#
# The strict token-set filter originally compared RAW tokens, so a query that
# omitted a legal form missed a listed entity outright:
#
#   "Rosneft Trading" vs listed "ROSNEFT TRADING S.A."              -> MISSED
#   "Gazprombank"     vs listed "GAZPROMBANK JOINT STOCK COMPANY"   -> MISSED
#
# Both are on the SDN list. The filter now strips generic corporate words
# before comparing - the same set `_word_match_score` already used - which
# recovers these without loosening the coincidence case above.

TRUE_POSITIVES = [
    ("Kim Jong Un", "kim"),
    ("Rosneft Trading", "rosneft"),
    ("Gazprombank", "gazprombank"),
    # An ALIAS-ONLY hit: "AERO-CARIBBEAN" exists in ALT.CSV, not in SDN.CSV,
    # so this fails outright if the alias file stops being ingested.
    ("Aero-Caribbean", "aero"),
]


@pytest.mark.parametrize("query,expected_fragment", TRUE_POSITIVES,
                         ids=[q for q, _ in TRUE_POSITIVES])
def test_listed_entities_are_surfaced(query, expected_fragment):
    """SURFACED, not necessarily asserted - and the distinction is the product.

    Without name-frequency data we cannot tell "Rosneft Trading" matching
    "ROSNEFT TRADING S.A." (real) from "Atlas Trading Company" matching "ATLAS
    HOLDING" (coincidence). I briefly stripped generic corporate words to
    recover the first and it promoted the second to a MATCH - measured.

    So the contract is: a listed entity must always REACH the caller, either as
    a match or in possible_matches_unverified. What must never happen is that
    it is silently dropped.
    """
    receipt, result = screen(query)
    surfaced = (result.get("matches") or []) + (
        result.get("possible_matches_unverified") or [])
    names = " ".join(m.get("name", "") for m in surfaced).lower()
    assert expected_fragment in names, (
        f"{query!r} is on the OFAC SDN list and did not reach the caller at "
        f"all - neither as a match nor as a candidate. A silently dropped "
        f"listing is the failure nobody discovers until it matters. Got: {names!r}")


def test_the_ofac_source_is_the_us_treasury():
    """Provenance IS the product for a compliance tool.

    We used to fetch OFAC from OpenSanctions' bulk export - a CC-BY-NonCommercial
    dataset - while telling buyers it came "directly from the US Treasury".
    Both halves of that were a problem: the licence, and the claim.
    """
    import core.screen_sanctions as m
    assert "ofac.treas.gov" in m._OFAC_SDN_CSV_URL, (
        f"OFAC must come from Treasury, not {m._OFAC_SDN_CSV_URL}")
    assert "ofac.treas.gov" in m._OFAC_ALT_CSV_URL
    assert "opensanctions" not in m._OFAC_SDN_CSV_URL.lower(), (
        "fetching their aggregated dataset in a commercial product is outside "
        "its CC-BY-NC licence")


def test_attempting_the_calibrated_source_is_not_the_same_as_reaching_it():
    """"Queried" and "answered" are different, and conflating them hides a
    warning the caller needs.

    `sources_queried` always contains the OpenSanctions URL, because we always
    attempt the call; a failure is recorded separately in
    `sources_unavailable`. A predicate that only looked at sources_queried
    therefore reported the calibrated source as having RUN on calls where it
    had been rate-limited and returned nothing - suppressing the "LOCAL-LIST
    CHECK ONLY" warning.

    The safety filter survived that because it keys on match provenance, not on
    this flag. This test guards the DISCLOSURE half.
    """
    receipt, result = screen("Rosneft Trading")
    unavailable = " ".join(result.get("sources_unavailable") or []).lower()
    if "opensanctions" not in unavailable:
        pytest.skip("the calibrated source answered on this run; nothing to assert")

    msg = receipt.human_message.lower()
    assert "local-list check only" in msg or "did not answer" in msg, (
        "the calibrated source did NOT answer, so the caller must be told this "
        f"was an incomplete screen. Got: {receipt.human_message[:200]}")

    # And if near-misses were found, the prose must not say a flat "no matches"
    # while the payload holds candidates. The structured field being honest is
    # not enough - an agent reads the sentence.
    candidates = result.get("possible_matches_unverified") or []
    if candidates:
        assert "candidate" in msg, (
            f"{len(candidates)} candidate(s) were found and the message does "
            f"not mention them: {receipt.human_message[:200]}")


# ---------------------------------------------------------------------------
# EU AND UK COVERAGE (added 2026-08-30)
# ---------------------------------------------------------------------------
#
# We screened OFAC only, which made the tool unusable for a European customer
# and made the "EU/UN/UK" claims we used to publish untrue. The EU consolidated
# list and the UK Sanctions List are published free by the authorities that
# issue them and expressly permit commercial use.
#
# The UN list is deliberately absent: equally easy to fetch, no open licence,
# no commercial carve-out. We screen OFAC, EU and UK and do not claim UN.
#
# EVERY NEW LIST ALSO MULTIPLIES FALSE POSITIVES, because our matcher has no
# name-frequency data. So these tests check coverage AND that the strict rule
# still holds across three lists rather than one.

def _index_ready() -> str:
    """Why the EU/UK tests need a live index, and why they SKIP without one.

    These lists are no longer downloaded into the process - holding both cost
    ~244MB and OOM-killed the instance. They live in `public.sanctions_names`,
    loaded by scripts/refresh_sanctions_lists.py.

    That makes the coverage tests below integration tests. Without database
    config they skip; WITH config and an empty table they FAIL, loudly. That
    asymmetry is the point: an environment that can check must check. A skip
    on an empty index would be the same "green means nothing ran" trap these
    tests exist to catch.
    """
    from storage.supabase_client import _get_config
    url, key = _get_config()
    if not (url and key):
        return "no database config in this environment"
    return ""


def _count(list_code: str) -> int:
    from storage.supabase_client import select_rows
    rows = asyncio.run(select_rows("sanctions_names",
                                   filters={"list_code": list_code}, limit=1000))
    return len(rows)


def test_eu_and_uk_indexes_are_populated():
    """Guard the guard: with an empty index every coverage test below passes
    for the wrong reason - nothing matches because nothing is there."""
    why = _index_ready()
    if why:
        pytest.skip(why)
    # 1000 is the select limit, so this asserts "the page came back full"
    # rather than a true count; it is enough to tell loaded from empty.
    assert _count("EU") == 1000, "EU index looks empty - run refresh_sanctions_lists.py"
    assert _count("UK") == 1000, "UK index looks empty - run refresh_sanctions_lists.py"


def test_a_listed_entity_is_found_on_every_list_that_carries_it():
    """Saddam Hussein Al-Tikriti is on OFAC, the EU list and the UK list.
    Finding him on one and missing the others would mean a list is loaded but
    not actually searched."""
    _w = _index_ready()
    if _w:
        pytest.skip(_w)
    _, result = screen("Saddam Hussein Al-Tikriti")
    assert result.get("matched") is True
    lists = {m.get("list", "").split(" ")[0] for m in result.get("matches") or []}
    assert "OFAC-SDN" in lists
    assert any(l.startswith("EU-CONSOLIDATED") for l in lists), lists
    assert any(l.startswith("UK-SANCTIONS") for l in lists), lists


def test_the_response_names_which_lists_actually_ran():
    """A caller with a European obligation must be able to tell whether the EU
    list was screened ON THIS CALL - not whether we support it in principle."""
    _w = _index_ready()
    if _w:
        pytest.skip(_w)
    _, result = screen("Saddam Hussein Al-Tikriti")
    screened = " ".join(result.get("lists_screened") or [])
    assert "OFAC-SDN" in screened
    assert "EU-CONSOLIDATED" in screened
    assert "UK-SANCTIONS" in screened


def test_we_do_not_claim_the_un_list():
    """It has no open licence and no commercial carve-out, so it is not ours to
    redistribute. Claiming it would be the same overclaim we removed."""
    _w = _index_ready()
    if _w:
        pytest.skip(_w)
    _, result = screen("Saddam Hussein Al-Tikriti")
    screened = " ".join(result.get("lists_screened") or []).upper()
    assert "UN-SECURITY" not in screened and "UNITED NATIONS" not in screened, (
        "we must not advertise UN coverage we do not have a licence to provide")


@pytest.mark.parametrize("name", INNOCENT)
def test_three_lists_do_not_relax_the_rule(name):
    """The whole risk of wider coverage: three times as many chances to accuse
    someone. The strict token-set rule must hold exactly as it did with one."""
    _w = _index_ready()
    if _w:
        pytest.skip(_w)
    _, result = screen(name)
    assert result.get("matched") is not True, (
        f"{name!r} became a MATCH once EU/UK were added. More lists must mean "
        f"more coverage, not a lower bar. "
        f"Matches: {[m.get('name') for m in result.get('matches') or []]}")
