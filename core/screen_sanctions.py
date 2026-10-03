"""
screen_sanctions -- free, read-only sanctions & watchlist screening.

Data sources. Three lists, each fetched from the authority that publishes it,
and each licensed for commercial use:

  1. OFAC SDN -- US Treasury Specially Designated Nationals, from
     sanctionslistservice.ofac.treas.gov (SDN.CSV plus ALT.CSV for alternate
     spellings). US Government work, public domain. No key.
  2. EU Consolidated financial sanctions -- European Commission, webgate.ec.
     europa.eu. Published under the Commission's open-data licence, which
     permits commercial reuse. No key.
  3. UK Sanctions List -- FCDO, sanctionslist.fcdo.gov.uk. Open Government
     Licence v3.0. No key.

  THE UN CONSOLIDATED LIST IS NOT SCREENED. It is as easy to fetch as the
  others and is deliberately absent: no open licence, no commercial carve-out.
  We screen what we are licensed to screen and say exactly that.

  The EU and UK lists are held in our own indexed copy (public.sanctions_names)
  rather than downloaded per call - see the note further down about the 244MB
  that OOM-killed this service. Every response states how old that copy is, and
  past seven days the list reports as unavailable rather than answering stale.

Matching is UNCALIBRATED and the output says so. We have no name-frequency
data, so a finding is asserted only on an exact normalised token-set equality;
every partial overlap is returned as possible_matches_unverified for the caller
to judge. See the note where the filter is applied.

ARABIC NAMES (core/arabic_names.py). A name in Arabic script, or a Latin name
with Arabic-name structure (al-, bin, Abd al-X, Abu X), is also compared BY SOUND
with every name on the three lists, and an Arabic-script name with the
Arabic-script aliases the EU and UK print for their entries. A sound-alike is a
graded CANDIDATE (match_confidence, token_alignment, match_explanation), never a
finding. The one new kind of finding is deliberately narrow: an Arabic-script
query equal, element for element, to an Arabic-script alias the publisher
printed. An Arabic-script query is never reported clean. Method, thresholds and
the measured recall: docs/ARABIC_SANCTIONS_EVAL.md. The English path above is
unchanged, and a test pins that.

Design:
  * 10-second timeout per upstream; fail-open to partial results.
  * If all upstreams fail, returns sources_unavailable populated -- never fabricates
    a match or a clear.
  * matched=False with an explicit "no matches on the screened lists" + WHICH lists
    were screened is returned when no match is found.
  * All string output is ASCII-safe (non-ASCII chars replaced with '?').
  * Cost: 0.00 USD (free read tool; demand probe for compliance positioning).
  * Telemetry: fires via the existing mcp_server dispatch hook (usage_events row).
  * Disclaimer: every response carries "informational screening, not legal advice;
    confirm against the official source before acting."
  * Evidence: every screen carries a `compliance_receipt` - a self-contained,
    hash-bound, optionally Ed25519-signed record of WHICH lists were screened,
    HOW OLD each copy was, WHEN, and WHAT came back. See core/compliance_receipt.
    The operator is the party who has to produce that record years later, so it
    is handed to them and stored nowhere here. Purely additive: a caller that
    ignores the field sees the identical answer it saw before.
"""
from __future__ import annotations

import asyncio
import contextvars
import csv
import hashlib
import io
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from core import arabic_names as _ar
from core.compliance_receipt import attach_receipt, service_version
from core.models import CostRecord, OperationStatus, OutcomeReceipt
from core.untrusted import fence as _fence_untrusted

_log = logging.getLogger("smb_broker.screen_sanctions")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# OpenSanctions publishes free bulk data (no API key needed) at a stable URL.
# Updated daily. 7.5MB CSV with id, schema, name, aliases, sanctions, program_ids.
# This is derived from the official OFAC SDN XML published by US Treasury.
# THE OFAC SDN LIST, FROM THE US TREASURY ITSELF.
#
# This used to fetch data.opensanctions.org's bulk export. Two problems with
# that, and the second is the serious one:
#
#   * their aggregated dataset is licensed CC-BY-NonCommercial and we are a
#     commercial product, so we were using it outside its licence;
#   * the manifest told buyers the list came "directly from the US Treasury",
#     which was simply not true. Provenance IS the product for a compliance
#     tool - it is the thing a customer is actually buying.
#
# Treasury's own publication is a US Government work in the public domain, free,
# unauthenticated, and authoritative. Fetching it removes the licence question
# and makes the provenance claim true rather than requiring us to soften it.
#
# Two files, because OFAC splits them: SDN.CSV carries primary names, ALT.CSV
# carries the aliases. Screening only SDN would silently lose ~20,000 alternate
# spellings - which for sanctions is a false-negative machine.
_OFAC_SDN_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
_OFAC_ALT_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ALT.CSV"
_TIMEOUT = 10  # seconds per upstream

_DISCLAIMER = (
    "Informational screening only, not legal advice; confirm against the official "
    "source before acting on any result. Negative results do not guarantee the "
    "party is not sanctioned on lists not queried."
)

# Maps OpenSanctions dataset IDs to human-readable list names
_DATASET_NAMES: dict[str, str] = {
    "us_ofac_sdn": "OFAC-SDN",
    "us_ofac_cons": "OFAC-Consolidated",
    "eu_fsf": "EU-Financial-Sanctions",
    "un_sc_sanctions": "UN-Security-Council",
    "gb_hmt_sanctions": "UK-HMT-Financial-Sanctions",
    "ca_dfatd_sema_sanctions": "Canada-SEMA-Sanctions",
    "au_dfat_sanctions": "Australia-DFAT-Sanctions",
    "ch_seco_sanctions": "Switzerland-SECO-Sanctions",
    "us_state_debarment": "US-State-Debarment",
    "us_bis_denied": "US-BIS-Denied-Persons",
    "interpol_red_notices": "INTERPOL-Red-Notices",
    "fr_tresor_gels_avoir": "France-TRESOR-Sanctions",
    "de_bafa_sanctions": "Germany-BAFA-Sanctions",
    "ru_nsd_isin": "Russia-NSD",
    "us_dea_fugitives": "US-DEA-Fugitives",
}

# Match score threshold -- results below this are not returned as hits
_MATCH_THRESHOLD_OFAC = 0.60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ascii(s: str) -> str:
    """Normalise a name for output, PRESERVING non-Latin scripts.

    This used to replace every non-ASCII character with '?', so the receipt for
    a Cyrillic or Arabic name recorded literal nonsense:

        screen_sanctions("Сбербанк")  -> "MATCH FOUND for '????????'"
        screen_sanctions("حزب الله")   -> "MATCH FOUND for '??? ????'"

    THE MATCHING IS NOT FINE, AND THIS COMMENT USED TO SAY IT WAS. It read
    "OpenSanctions handles those scripts upstream" - true when written, and
    false from the moment that dependency was removed. Nobody re-checked,
    because the comment said there was nothing to check.

    What is actually true now: _normalize_name reduces to [a-z0-9 ], so a
    Cyrillic or CJK name normalises to NOTHING and cannot match any index
    entry. That is handled honestly rather than silently - see
    _is_screenable, which reports such a name as NOT SCREENED instead of
    returning a clean result - but it is a real coverage gap, not a solved
    problem.

    ARABIC IS NO LONGER IN THAT GAP (2026-10-03). core/arabic_names.py reads
    Arabic script directly and compares Arabic and Latin names by sound; see the
    section "ARABIC-SCRIPT AND TRANSLITERATION-AWARE MATCHING" below. Cyrillic
    and CJK are still not screened, and still say so.

    The receipt is the audit artefact, and an audit record that cannot say
    what was screened is not an audit record. "Wire-safe" was never a real
    constraint: MCP responses are JSON, and JSON is UTF-8 by definition.

    For an Oman-registered company whose home market writes in Arabic, silently
    destroying Arabic names in its own compliance receipts is self-sabotage.

    NFC rather than NFKD: compose to the canonical form so identical names
    compare equal, without decomposing characters into pieces we then throw
    away. Control characters are still stripped, which is the only genuine
    wire-safety concern here.
    """
    if not s:
        return s
    normalized = unicodedata.normalize("NFC", s)
    return "".join(c for c in normalized
                   if unicodedata.category(c)[0] != "C" or c in "\t\n")


def _clean(v) -> Optional[str]:
    """Return ASCII string or None."""
    if v is None:
        return None
    return _ascii(str(v).strip()) or None


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching: lower, alphanum + spaces only.

    APOSTROPHES ARE DELETED, NOT SPLIT ON, and that one character was a P0.

    This used to turn `'` into a space, so "Joe's Pizza LLC" tokenised to
    ['joe', 's', 'pizza', 'llc'] and "RICA'S PIZZA" to ['rica', 's', 'pizza'].
    The orphaned "s" counted as a distinctive word, overlapped, and the pizza
    shop scored 0.667 - over threshold. Live result:

        MATCH FOUND for 'Joe's Pizza LLC': 'RICA'S PIZZA' on OFAC-SDN
        (program=US-NARCO)

    And it did not stop there: `map_trade_restriction` runs on the same engine
    and returned "RESTRICTED... Matched parties: Joe's Pizza LLC, Mahan Air.
    Halt the transaction and seek legal counsel." An ordinary pizza shop named
    beside an actual sanctioned airline, with an instruction to stop a legal
    transaction. OpenSanctions returns nothing for that name - we invented all
    of it, out of one apostrophe.

    Deleting instead of splitting gives "joes" and "ricas", which do not match.
    Hyphens and commas still become spaces: "Kim Jong-un" must tokenise the
    same as "Kim Jong un", and that behaviour is load-bearing for real hits.

    SINGLE CHARACTERS ARE DROPPED for the same reason - one letter is never an
    identity, it is debris from punctuation or an initial.
    """
    lower = name.lower()
    # Possessives and internal apostrophes vanish; hyphens and commas separate.
    lower = lower.replace("'", "").replace("’", "")
    lower = re.sub(r"[-,]", " ", lower)
    cleaned = re.sub(r"[^a-z0-9\s]", "", lower)
    return " ".join(w for w in cleaned.split() if len(w) > 1)


# Words that carry NO identifying information about a company.
#
# MEASURED FALSE POSITIVE (2026-08-29). Screening the invented name "Acme
# Trading LLC" returned "MATCH FOUND ... 'ONCU Trading L.L.C.' on OFAC-SDN
# (score=0.67, program=US-IRAN)". OpenSanctions itself returns ZERO results for
# "Acme Trading" - we manufactured that hit.
#
# The arithmetic: {acme, trading, llc} vs {oncu, trading, llc} overlaps on
# `trading` and `llc`, which is 2 of 3 words = 0.67, comfortably over the 0.60
# threshold. The two matching tokens were a generic activity word and a legal
# form. NOTHING about the actual identity matched.
#
# This is the worst failure mode a compliance tool has. A false negative lets
# one bad actor through; a false positive that fires on "<anything> Trading LLC"
# tells an agent that an ordinary business appears on a US-Iran sanctions
# programme - and an agent acting on that may refuse a legitimate customer.
# Being wrong in that direction, at scale, is how a screening tool becomes
# worse than no screening tool.
#
# So these words may still APPEAR in a name; they simply cannot be what a match
# is made of.
_GENERIC_NAME_WORDS = frozenset({
    # legal forms
    "llc", "l", "c", "ltd", "limited", "inc", "incorporated", "corp",
    "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "sas", "sarl",
    "bv", "nv", "ab", "as", "oy", "kft", "srl", "spa", "pte", "pty", "kk",
    "llp", "lp", "est", "establishment", "fze", "fzc", "fzco", "wll", "psc",
    # RUSSIAN / CIS LEGAL FORMS - the most load-bearing entries in this set.
    #
    # Without them, "Zarubezhneft" vs "Zarubezhneft OAO" scored 0.50 and fell
    # under the 0.60 threshold: one distinctive word against {distinctive,
    # legal-form}. Russian and CIS entities are among the most heavily
    # sanctioned in the world, so leaving their legal forms as "distinctive"
    # would have turned a false-positive fix into a FALSE-NEGATIVE generator
    # aimed squarely at the entities that matter most. Caught only by testing
    # a real sanctioned name against its own registered form.
    "oao", "zao", "ooo", "pao", "ao", "jsc", "ojsc", "cjsc", "pjsc", "joint",
    "stock", "fgup", "gup", "mup", "nko", "too", "chp", "ip",
    # other common forms seen on sanctions lists
    "bhd", "sdn", "tbk", "pt", "cv", "kg", "ohg", "se", "scs", "snc",
    "eurl", "sasu", "aps", "asa", "oyj", "doo", "dooel", "ad", "ead",
    # generic descriptors
    "trading", "trade", "group", "holding", "holdings", "international",
    "enterprise", "enterprises", "services", "service", "general", "global",
    "industries", "industrial", "commercial", "business", "solutions",
    "partners", "associates", "ventures", "investment", "investments",
    "development", "projects", "contracting", "supplies", "supply", "export",
    "import", "exports", "imports", "and", "of", "the", "for",
    # GEOGRAPHIC AND POSITIONAL WORDS - weak identifiers in a company name.
    #
    # Observed live after the recall fix deployed: "Gulf General Trading LLC"
    # matched "Gulf General Contracting Limited". Both names reduce to the
    # single distinctive token "gulf", so the query was fully covered and
    # scored 1.00. A region is not an identity - half the companies in the
    # Gulf have "Gulf" in their name.
    #
    # DELIBERATELY EXCLUDES COUNTRY NAMES. "Iran", "Korea", "Syria", "Russia"
    # carry real sanctions signal and must stay distinctive; a regional or
    # directional word does not. That line is the difference between removing
    # noise and removing evidence.
    "gulf", "middle", "east", "west", "north", "south", "central", "eastern",
    "western", "northern", "southern", "arab", "arabian", "regional",
    "overseas", "worldwide", "continental", "universal", "united", "national",
})

# The Arabic/transliteration layer must never disagree with this set about what
# a generic word is, so it is handed the set rather than keeping its own copy.
_ar.set_generic_latin(_GENERIC_NAME_WORDS)


def _word_match_score(query: str, candidate: str) -> float:
    """Word-overlap score, computed on DISTINCTIVE words only.

    score = |distinctive overlap| / max(|distinctive query|, |distinctive candidate|)

    A name is identified by what is unusual about it. Two companies sharing
    "Trading" and "LLC" have nothing in common; two sharing "Zarubezh" do.

    If either side has no distinctive words at all (a name made entirely of
    generic terms, e.g. "General Trading Company"), fall back to the full word
    sets rather than dividing by zero - such a name genuinely cannot be
    discriminated on, and the honest behaviour is to score it as the plain
    overlap and let the threshold and the human caveat do their work.
    """
    q_all = set(_normalize_name(query).split())
    c_all = set(_normalize_name(candidate).split())
    if not q_all or not c_all:
        return 0.0

    q_words = q_all - _GENERIC_NAME_WORDS
    c_words = c_all - _GENERIC_NAME_WORDS

    # Fall back to full word sets ONLY when NEITHER side has anything
    # distinctive - e.g. "General Trading Company" against itself, which is a
    # real company name made entirely of generic parts and must still match.
    #
    # The earlier version fell back when EITHER side was all-generic, which
    # meant screening the bare word "Trading" matched "ONCU Trading L.L.C." at
    # 1.00: one side had nothing distinctive, so the comparison silently
    # reverted to exactly the generic-word matching this whole function exists
    # to stop. If one side has distinctive words and the other has none, there
    # is no distinctive basis for a match and the honest answer is zero.
    if not q_words and not c_words:
        q_words, c_words = q_all, c_all
    elif not q_words or not c_words:
        return 0.0

    overlap = len(q_words & c_words)
    if not overlap:
        return 0.0

    # A ONE-WORD QUERY MUST EARN ITS MATCH. Screening the bare name "Al"
    # returned score 1.00 against "Abu Usama AL-JAZA'IRI" on a US-TERR
    # programme - because "al" is one of the query's tokens and it appears in
    # the listed name, so recall was perfect. "Al" is an Arabic article that
    # occurs in a large fraction of the list; a two-letter token is not an
    # identity, and 1.00 is the top of the confidence range.
    #
    # "Rosneft" is also a single token and MUST still match, so this cannot be
    # a ban on one-word queries. Length is a crude proxy for distinctiveness
    # and it separates these two cleanly: 2 characters carries no information,
    # 7 does. Anything shorter than 4 characters standing alone is treated as
    # unscreenable rather than as a perfect match.
    if len(q_words) == 1:
        only = next(iter(q_words))
        if len(only) < 4:
            return 0.0

    # HOW MUCH OF THE SCREENED NAME APPEARS IN THE LISTED ONE.
    #
    # THIS IS DELIBERATELY ASYMMETRIC, and the asymmetry is the point: the
    # query is the entity someone is checking, the candidate is a sanctions
    # list entry. They are not interchangeable, and the question a screener
    # actually asks is "is the thing in front of me on the list?" - not "do
    # these two strings resemble each other".
    #
    # Four denominators were measured against real sanctioned names, and each
    # of the first three MISSES or MANUFACTURES something specific:
    #
    #   max():  penalises a short query against a long official name. MISSED
    #           "Rosneft" vs "OJSC Rosneft Oil Company" and "Sberbank" vs
    #           "Sberbank of Russia PJSC" at 0.50 - false negatives on
    #           household-name sanctioned entities.
    #   min():  lets a candidate whose only distinctive word is a PLACE match
    #           anything from that place: "Muscat Coffee House" vs "Muscat
    #           Trading LLC" scored 1.00.
    #   F1:     fixed both, then manufactured a live hit anyway - "Bright Star
    #           Trading Company" vs "GLOBAL STAR" scored 0.67, because the
    #           candidate reduced to one distinctive word so precision was
    #           perfect. Observed on the deployed endpoint, not in theory.
    #   recall: correct on all 17 measured cases.
    #
    # THE TRADE, stated plainly: recall is generous to short queries. Screening
    # the single word "Star" flags every listed name containing it. For a
    # SANCTIONS tool that is the right direction to err - a flagged name costs
    # one verification, a missed one can be a sanctions breach - and it is only
    # safe because generic words were removed first, so the noise is confined
    # to genuinely distinctive tokens rather than "Trading" and "LLC".
    #
    # A proper fix is inverse-document-frequency weighting, so "Rosneft" counts
    # for more than "Star". That needs corpus statistics we do not have here.
    # Until then this is a heuristic that is honest about being one, and every
    # result carries "Verify against the official source before acting".
    return overlap / len(q_words)


# ---------------------------------------------------------------------------
# WE DO NOT USE OPENSANCTIONS.  (founder decision, 2026-08-30)
# ---------------------------------------------------------------------------
#
# It was the primary source here: a calibrated matcher with name-frequency
# data, plus breadth across 40+ national lists. Removed for two reasons, and
# the second one is the disqualifying one:
#
#   * Commercially: pay-as-you-go per query, which the founder does not want.
#   * Legally: their DATA is CC-BY-NonCommercial. We sell screening. We could
#     not have used it commercially whatever we paid, which makes the
#     dependency a liability rather than a cost.
#
# WHAT WE GAVE UP, STATED PLAINLY BECAUSE IT MATTERS. OpenSanctions knew that
# "Ali Mohammed" is a common name and "Zarubezhneft" is not. We do not, and we
# are not going to reproduce name-frequency scoring from nothing. So the honest
# matcher is a narrow one: identical normalised token sets are reported as
# matches, and every partial overlap goes to possible_matches_unverified for
# the caller to judge. That is not a degraded state waiting to be restored -
# it is the permanent, disclosed method, and every response says so.
#
# WHAT WE KEPT: the three lists we fetch from the publishers themselves, all
# licensed for commercial use - OFAC SDN (US Treasury, public domain), the EU
# consolidated list (European Commission, open data) and the UK Sanctions List
# (FCDO, OGL v3.0). The UN list stays out: no open licence, no carve-out.

# ---------------------------------------------------------------------------
# OFAC SDN CSV  (free, keyless, official US Treasury source)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LIST CACHE
# ---------------------------------------------------------------------------
#
# EVERY SCREEN USED TO RE-DOWNLOAD THE FULL LISTS. Measured before this
# existed: 11-16 SECONDS per call, re-fetching 5.6MB of SDN.CSV plus 1MB of
# ALT.CSV from Treasury on every single screen. That is slow for the caller,
# rude to the publisher, and fragile - one Treasury blip and every screen in
# flight degrades at once.
#
# It also capped what we could ever add. The UK list is ~50MB; re-fetching that
# per call is not an option, so caching was a prerequisite for wider coverage,
# not a nicety.
#
# Sanctions lists are published roughly daily, so a few hours of staleness is
# immaterial next to the alternative - and STALE IS BETTER THAN ABSENT here:
# if a refresh fails we keep serving the last good copy and say how old it is,
# because a screen against yesterday's list beats no screen at all.
_LIST_TTL_S = 6 * 3600
_list_cache: dict[str, tuple[float, str]] = {}
# How stale the copy we last SERVED was, so the receipt can say so.
_stale_ages: dict[str, float] = {}


def list_cache_age_s(url: str) -> Optional[float]:
    """Seconds since this list was fetched, or None if never."""
    hit = _list_cache.get(url)
    return (time.time() - hit[0]) if hit else None


# The smallest number of parseable rows a real OFAC list file has. SDN.CSV is
# ~18,000 lines and ALT.CSV ~11,000; a hundred is far below either and far
# above anything an error page or a truncated response produces.
_MIN_LIST_ROWS = 100


def _looks_like_a_sanctions_list(text: str) -> bool:
    """Is this body plausibly the CSV we asked for, or is it an error page?

    A 200 IS NOT A SANCTIONS LIST, AND ACCEPTING ONE PRODUCED A FALSE CLEAN.

    _fetch_url accepted any non-empty 200 body and cached it for six hours.
    _parse_ofac_sdn silently yields nothing for a body that is not the CSV -
    every row has fewer than four columns, so every row is skipped - and
    _call_ofac_sdn reports success whenever the text is not None. Net effect,
    measured: a JSON error body or an HTML maintenance page from Treasury made
    screen_sanctions report

        screening_status: "clean"  ... "No matches on the screened lists"
        lists_screened:   ["OFAC-SDN (...; fetched fresh)"]

    for Mahan Air, a designated airline, with ZERO lists actually screened -
    and the six-hour cache kept it doing that for six hours.

    The database path already guards this ("AN EMPTY INDEX IS NOT A CLEAN
    SCREEN", _screen_list_db); the HTTP path had no equivalent. A truncated
    200 - a CDN cutting the body after a few hundred of ~18,000 lines - has
    exactly the same effect and also needs to be refused, which is why this
    counts ROWS rather than sniffing for a header.
    """
    if not text or not text.strip():
        return False
    head = text.lstrip()[:400].lower()
    if head.startswith(("<!doctype", "<html", "{", "[")):
        return False                # an HTML error page or a JSON error body
    # Count rows that carry the comma-separated shape the parsers require.
    rows = 0
    for line in text.splitlines():
        if line.count(",") >= 3:
            rows += 1
            if rows >= _MIN_LIST_ROWS:
                return True
    return False


async def _fetch_url(url: str, allow_stale: bool = True) -> Optional[str]:
    """GET a public list file, cached for _LIST_TTL_S.

    On a failed refresh, returns the last good copy rather than None -
    `allow_stale=False` opts out where a caller genuinely needs freshness.

    A 200 IS NOT A SANCTIONS LIST. See _looks_like_a_sanctions_list: this
    used to accept any non-empty 200 body, cache it for six hours, and report
    OFAC as SCREENED with a clean result.
    """
    now = time.time()
    hit = _list_cache.get(url)
    if hit and (now - hit[0]) < _LIST_TTL_S:
        return hit[1]

    import httpx  # noqa: F401  (imported here to keep startup light)
    ua = "AgentBroker-SanctionsScreen/1.0 (compliance tool; contact hello@hatchloop.dev)"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
                follow_redirects=True,
            )
        if resp.status_code == 200 and _looks_like_a_sanctions_list(resp.text):
            _list_cache[url] = (now, resp.text)
            # CLEAR THE STALE MARK ON A SUCCESSFUL FETCH.
            #
            # _stale_ages was written on a stale serve and never cleared,
            # so once Treasury had been unreachable once every later
            # response kept saying "served from a cached copy 30h old"
            # for the life of the process, with a fresh copy in hand.
            # A stamp written once and never re-derived - inside the
            # commit written to stop exactly that.
            _stale_ages.pop(url, None)
            return resp.text
    except Exception:
        pass

    # Refresh failed. A list from a few hours ago is still a real screen -
    # but "a few hours" was never enforced. This served the last good copy
    # INDEFINITELY, with no age reported anywhere, while the EU and UK lists
    # refuse to answer past 7 days and stamp their age on every response. A
    # long-running process with Treasury unreachable would have screened
    # against a weeks-old SDN list and called it current.
    #
    # Same rule for all three lists now.
    if hit and allow_stale:
        age_s = now - hit[0]
        if age_s > _STALE_AFTER_DAYS * 86400:
            return None                         # too old to be a screen
        _stale_ages[url] = age_s
        return hit[1]
    return None


def _ofac_age_note() -> str:
    """State the age of the OFAC copy we served, when it was not fresh.

    EU and UK stamp their index age on every response. OFAC said nothing at
    all, so a cached copy served after a Treasury outage was reported exactly
    like a live fetch.
    """
    ages = [a for u, a in _stale_ages.items()
            if u in (_OFAC_SDN_CSV_URL, _OFAC_ALT_CSV_URL)]
    if not ages:
        return "; fetched fresh"
    hrs = max(ages) / 3600.0
    return (f"; served from a cached copy {hrs:.0f}h old - Treasury was "
            f"unreachable on the last refresh")






def _parse_ofac_sdn(csv_text: str, query_name: str,
                    alt_text: Optional[str] = None) -> list[dict]:
    """Parse OFAC's own SDN.CSV (+ optional ALT.CSV) and return matches.

    TREASURY'S FORMAT IS NOT THE ONE WE USED TO PARSE. SDN.CSV is a legacy
    headerless 12-column export:

        0 ent_num   1 SDN_Name   2 SDN_Type   3 Program   4 Title
        5 Call_Sign 6 Vess_type  7 Tonnage    8 GRT       9 Vess_flag
        10 Vess_owner   11 Remarks

    Names are written "SURNAME, Given" - "KIM, Jong Un". `_normalize_name`
    already reduces that to the same token set as "Kim Jong Un", so the comma
    costs nothing; the ordering discipline applied downstream is what makes the
    comparison safe.

    Absent fields are the literal string "-0-", not empty, which is why every
    read below is guarded rather than trusted.

    ALT.CSV is [ent_num, alt_num, alt_type, alt_name, remarks] and holds ~20k
    alternate spellings. Screening without it would silently miss the aliases
    that sanctions evasion depends on.
    """
    def _clean(v: str) -> str:
        v = (v or "").strip()
        return "" if v in ("-0-", "-0- ", "") else v

    # ent_num -> [alternate names]
    aliases: dict[str, list[str]] = {}
    if alt_text:
        try:
            for row in csv.reader(io.StringIO(alt_text)):
                if len(row) < 4:
                    continue
                ent, alt_name = row[0].strip(), _clean(row[3])
                if ent and alt_name:
                    aliases.setdefault(ent, []).append(alt_name)
        except Exception:
            pass  # aliases are an enhancement; never fail the screen on them

    matches: list[dict] = []
    seen_ids: set[str] = set()
    try:
        for row in csv.reader(io.StringIO(csv_text)):
            if len(row) < 4:
                continue
            entity_id = row[0].strip()
            primary_name = _clean(row[1])
            sdn_type = _clean(row[2]).lower()
            program = _clean(row[3])
            if not primary_name or entity_id in seen_ids:
                continue

            candidates = [primary_name] + aliases.get(entity_id, [])
            best_score, best_name = 0.0, primary_name
            for cand in candidates:
                sc = _word_match_score(query_name, cand)
                if sc > best_score:
                    best_score, best_name = sc, cand
            if best_score < _MATCH_THRESHOLD_OFAC:
                continue

            seen_ids.add(entity_id)
            etype = ("INDIVIDUAL" if sdn_type == "individual"
                     else "VESSEL" if sdn_type == "vessel"
                     else "AIRCRAFT" if sdn_type == "aircraft"
                     else "ENTITY")
            matches.append({
                "name": _ascii(best_name),
                "list": "OFAC-SDN",
                "match_score": round(best_score, 3),
                "program": _ascii(program[:80]) or None,
                "entity_type": etype,
                # Treasury's own search UI, so a caller can confirm against the
                # authority rather than against us.
                "source_url": "https://sanctionssearch.ofac.treas.gov/",
            })
    except Exception:
        pass  # fail-open: partial results beat no results

    matches.sort(key=lambda m: m["match_score"], reverse=True)
    return matches[:5]


async def _fetch_ofac_sdn_csv() -> Optional[str]:
    """Primary SDN names. Kept as its own function for the existing tests."""
    return await _fetch_url(_OFAC_SDN_CSV_URL)


async def _fetch_ofac_alt_csv() -> Optional[str]:
    """Alternate spellings (a.k.a., f.k.a., n.k.a.).

    Fetched separately because OFAC publishes them separately. If this one
    fails we still screen, on primary names alone - and say so, rather than
    quietly screening a smaller list than the caller believes.
    """
    return await _fetch_url(_OFAC_ALT_CSV_URL)


# ---------------------------------------------------------------------------
# EU AND UK LISTS - fetched from the issuing authorities, free, commercial-OK
# ---------------------------------------------------------------------------
#
# Until now this tool screened OFAC only, which makes it unusable for a
# European customer and made "EU/UN/UK" claims we could not honour. Both lists
# below are published by the authority that issues them, need no key, and
# EXPRESSLY permit commercial use - the EU under its open-data licence, the UK
# under the Open Government Licence v3.0.
#
# The UN Consolidated List is deliberately NOT here. It is equally easy to
# fetch and has NO open licence and no commercial carve-out, so redistributing
# it is not ours to do. We screen OFAC, EU and UK, and we do not claim UN.
#
# THE TRAP THE UK LIST SETS: the older "OFSI Consolidated List of Financial
# Sanctions Targets" was closed in January 2026 and its endpoints can still
# answer with stale data rather than an error - a silently outdated sanctions
# screen, which is the worst failure this tool has. The URL below is the
# current FCDO-published list.
_EU_CSV_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
               "csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw")
_UK_CSV_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"

# THE EU AND UK LISTS LIVE IN THE DATABASE, NOT IN THIS PROCESS.
#
# They were held in memory first: ~172MB of parsed index plus ~72MB of cached
# source text, per worker. That was an out-of-memory kill - the origin entered
# a restart loop and served 502 to everything for several minutes, while Render
# reported the deploy as "live" throughout because the container started fine
# and was then killed.
#
# Two compaction schemes were tried before accepting the real answer: a
# frozenset-keyed index (WORSE - 112MB) and a sorted-token-string index
# (~100MB). Python object overhead dominates; no tuning makes 46,000 records
# cheap enough to live in every worker.
#
# `scripts/refresh_sanctions_lists.py` now loads both lists into
# `public.sanctions_names` (exact key + GIN-indexed token array), and
# `_screen_list_db()` below looks them up. The parsers stay here because that
# script imports them - they are the definition of how these feeds are read,
# and the feeds are the thing that changes shape.

def _eu_parse(raw: str) -> list[dict]:
    """EU consolidated list -> [{name, entity_id, programme, etype}].

    Semicolon-delimited, UTF-8 BOM, 118 columns, ONE ROW PER ALIAS - so an
    entity appears many times and is grouped by Entity_LogicalId.
    `NameAlias_WholeName` already carries the assembled name.
    """
    out: list[dict] = []
    try:
        rows = csv.reader(io.StringIO(raw), delimiter=";")
        hdr = next(rows)
        iW = hdr.index("NameAlias_WholeName")
        iId = hdr.index("Entity_LogicalId")
        iType = hdr.index("Entity_SubjectType")
        iProg = hdr.index("Entity_Regulation_Programme") if             "Entity_Regulation_Programme" in hdr else None
        # COUNTRY IS A HINT, NEVER A FILTER - see _country_note() below. We
        # take address, citizenship and birth country because a listed person
        # is reachable through any of them, and a caller asking about "IR"
        # means "connected to Iran", not "whose postal address is in Iran".
        iCty = [hdr.index(c) for c in ("Address_CountryIso2Code",
                                       "Citizenship_CountryIso2Code",
                                       "BirthDate_CountryIso2Code") if c in hdr]
        # TWO PASSES, because of how this feed is shaped. It is one row per
        # ALIAS, and the country columns are populated on the rows carrying an
        # ADDRESS - which are usually not the same rows. Reading country off
        # the row that supplied the name returned it empty for all 30,739
        # records, and would have shipped a country field that was always
        # blank: a filter that silently never matches, which is worse than no
        # filter at all.
        #
        # So countries are gathered per ENTITY across every one of its rows,
        # then attached to each of that entity's names.
        buffered = list(rows)
        ent_countries: dict[str, set] = {}
        for r in buffered:
            if iId >= len(r):
                continue
            eid = r[iId]
            for i in iCty:
                v = r[i].strip().upper() if i < len(r) else ""
                # "00" is the feed's placeholder for unknown. Storing it would
                # make a country field that looks populated and matches nothing.
                if len(v) == 2 and v.isalpha():
                    ent_countries.setdefault(eid, set()).add(v)

        seen: set[tuple[str, str]] = set()
        for r in buffered:
            if len(r) <= iW:
                continue
            nm = r[iW].strip()
            if not nm:
                continue
            eid = r[iId] if iId < len(r) else ""
            if (eid, nm) in seen:
                continue
            seen.add((eid, nm))
            # THIS COLUMN HOLDS A CODE, NOT A WORD. It contains "P" or "E";
            # the test was `st.startswith("person")`, which is False for
            # every row ever published, so all 23,941 EU entries were typed
            # ENTITY - including every individual. The word "person" lives in
            # the NEXT column, Entity_SubjectType_ClassificationCode.
            st = (r[iType] if iType < len(r) else "").strip().upper()
            out.append({
                "name": nm,
                "entity_id": eid,
                "programme": (r[iProg].strip()[:80] if iProg and iProg < len(r) else ""),
                "etype": "INDIVIDUAL" if st == "P" else "ENTITY",
                "countries": sorted(ent_countries.get(eid, ())),
            })
    except Exception:
        pass  # a parse failure must not take the whole screen down
    return out


def _uk_parse(raw: str) -> list[dict]:
    """UK Sanctions List -> [{name, entity_id, programme, etype}].

    Row 0 is a "Report Date:" preamble, NOT the header - row 1 is. Names are
    split across `Name 1`..`Name 6` (given names then family name) and must be
    joined; rows repeat per address/identifier, so they are deduped by
    (Unique ID, assembled name).
    """
    out: list[dict] = []
    try:
        rows = list(csv.reader(io.StringIO(raw)))
        hdr_i = 1 if len(rows) > 1 and len(rows[1]) > 5 else 0
        hdr = rows[hdr_i]
        name_cols = [hdr.index(f"Name {i}") for i in range(1, 7) if f"Name {i}" in hdr]
        iUid = hdr.index("Unique ID") if "Unique ID" in hdr else 1
        iReg = hdr.index("Regime Name") if "Regime Name" in hdr else None
        # THE COLUMN THIS LOOKED FOR DOES NOT EXIST. The FCDO feed has no
        # "Individual, Entity, Ship" header - it has "Designation Type" - so
        # iType was None on every run and every UK entry was typed ENTITY.
        # A hand-written column name that silently resolves to None is the
        # same defect as a hand-maintained file list that silently skips a
        # missing path.
        iType = next((hdr.index(c) for c in
                      ("Designation Type", "Individual, Entity, Ship",
                       "Type of entity") if c in hdr), None)
        # The UK feed writes country names, not ISO codes ("Iran", "Russia"),
        # so these are stored as given and compared case-insensitively against
        # both the code and the name the caller supplies.
        iCty = [hdr.index(c) for c in ("Address Country", "Nationality(/ies)",
                                       "Country of birth") if c in hdr]
        # THE NAME IN THE LISTED PARTY'S OWN SCRIPT. The FCDO publishes it in a
        # column of its own ("Name non-latin script"), which this parser used to
        # ignore: on the 2026-10-03 copy 735 rows (677 entries) carry an Arabic or
        # Persian spelling that was never indexed, so an Arabic-script query had
        # nothing of the publisher's to be compared with. Only Arabic-script
        # values are taken; Cyrillic (3,391 rows) and CJK are not screened (see
        # _is_screenable).
        # Looked up by a candidate list and case-insensitively, like iType above: an exact-string lookup that
        # silently resolved to None when the FCDO renamed or re-cased the column would have dropped every UK
        # Arabic alias with no signal at all (and the sweep would then have deleted the stored ones).
        iNL = next((i for i, h in enumerate(hdr)
                    if h.strip().lower().replace("_", " ").replace("-", " ") in
                    ("name non latin script", "name non latin", "non latin script name")), None)
        if iNL is None:
            _log.warning("uk_parse_no_non_latin_column header_columns=%d -- UK Arabic-script aliases will NOT be "
                         "indexed from this copy", len(hdr))
        seen: set[tuple[str, str]] = set()
        for r in rows[hdr_i + 1:]:
            if len(r) <= max(name_cols + [iUid]):
                continue
            parts = [r[i].strip() for i in name_cols if r[i].strip()]
            if not parts:
                continue
            nm = " ".join(parts)
            uid = r[iUid]
            if (uid, nm) in seen:
                continue
            seen.add((uid, nm))
            st = (r[iType] if iType is not None and iType < len(r) else "").lower()
            # "Individual" / "Entity" / "Ship" in the current feed.
            rec = {
                "name": nm,
                "entity_id": uid,
                "programme": (r[iReg].strip()[:80] if iReg is not None and iReg < len(r) else ""),
                "etype": "INDIVIDUAL" if st.startswith("indiv") else "ENTITY",
                "countries": sorted({c.strip().upper()
                                     for i in iCty if i < len(r)
                                     for c in r[i].split(";")
                                     if 1 < len(c.strip()) <= 40}),
            }
            out.append(rec)
            if iNL is not None and iNL < len(r):
                nl = r[iNL].strip()
                if nl and _ar.has_arabic_script(nl) and (uid, nl) not in seen:
                    seen.add((uid, nl))
                    out.append(dict(rec, name=nl))
    except Exception:
        pass
    return out


_warming: set[str] = set()


# HOW OLD THE INDEX IS, DISCLOSED ON EVERY SCREEN.
#
# The EU and UK lists are a local copy, refreshed on a schedule. A copy can go
# stale, and a stale sanctions screen is the worst failure this tool has: it
# returns "no match" with full confidence for someone designated last week.
#
# The honest handling is not to promise freshness - it is to STATE the age and
# let the caller judge against their own obligation. So every screen carries
# the date its EU/UK data was last refreshed, and past _STALE_AFTER_DAYS the
# list moves into sources_unavailable, where a degraded source belongs.
_STALE_AFTER_DAYS = 7
_age_cache: dict[str, tuple[float, Optional[str]]] = {}
_AGE_TTL_S = 900


async def _list_refreshed_at(list_code: str) -> Optional[str]:
    """Newest refreshed_at for a list, as YYYY-MM-DD. None if unknown."""
    now = time.time()
    hit = _age_cache.get(list_code)
    if hit and (now - hit[0]) < _AGE_TTL_S:
        return hit[1]
    try:
        from storage.supabase_client import select_rows_strict
        # ASCENDING - the OLDEST row, not the newest.
        #
        # This read the MAX, so a single freshly-stamped row made 23,940 stale
        # rows report as current. The freshness rule exists to stop us
        # answering from an outdated list; keying it on the newest row is the
        # one ordering that cannot detect that.
        rows = await select_rows_strict("sanctions_names",
                                        filters={"list_code": list_code},
                                        order="refreshed_at.asc", limit=1)
        val = (rows[0].get("refreshed_at") or "")[:10] if rows else None
    except Exception:                           # noqa: BLE001
        # DO NOT CACHE A FAILURE. Unknown age now sends the list to
        # sources_unavailable (see the gate in _screen_list_db), so caching it
        # would take that list out of service for the full _AGE_TTL_S of 15
        # minutes after a single blip - long after Supabase recovered.
        # Returning without writing the cache means the next call retries.
        return None
    _age_cache[list_code] = (now, val)
    return val


def _days_since(day: Optional[str]) -> Optional[int]:
    if not day:
        return None
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).days


# COUNTRY IS A HINT. IT MUST NEVER REMOVE A MATCH.
#
# `country` was accepted and ignored for as long as it existed - it was
# consumed only by OpenSanctions - while the response said "(country filter:
# IR)". It now does something real, but deliberately NOT what the name
# suggests, and the difference matters more than the feature.
#
# Our country data is the address, nationality and birth country recorded on a
# listing. It is not an exhaustive record of where a sanctioned party operates,
# it is absent on ~15% of entries, and the two feeds disagree on format (the EU
# writes ISO2, the UK writes names like "FORMER USSR CURRENTLY UKRAINE"). If a
# country mismatch removed a match, every one of those gaps would become a
# FALSE NEGATIVE - a clean screen for someone who is on the list. That is the
# one failure this whole file is arranged to prevent.
#
# So country annotates and RANKS. Nothing is ever dropped for it.
_ISO2_NAMES = {
    "IR": "IRAN", "RU": "RUSSIA", "KP": "NORTH KOREA", "SY": "SYRIA", "IQ": "IRAQ",
    "BY": "BELARUS", "CU": "CUBA", "VE": "VENEZUELA", "MM": "MYANMAR",
    "AF": "AFGHANISTAN", "LY": "LIBYA", "SD": "SUDAN", "SS": "SOUTH SUDAN",
    "SO": "SOMALIA", "YE": "YEMEN", "ZW": "ZIMBABWE", "LB": "LEBANON",
    "UA": "UKRAINE", "CN": "CHINA", "TR": "TURKEY", "AE": "UNITED ARAB EMIRATES",
    "GB": "UNITED KINGDOM", "US": "UNITED STATES", "ML": "MALI", "NI": "NICARAGUA",
    "HT": "HAITI", "CF": "CENTRAL AFRICAN REPUBLIC", "CD": "DEMOCRATIC REPUBLIC OF THE CONGO", "ER": "ERITREA",
    "GN": "GUINEA", "GW": "GUINEA-BISSAU", "TN": "TUNISIA", "EG": "EGYPT",
    "PK": "PAKISTAN", "IN": "INDIA", "TH": "THAILAND", "MD": "MOLDOVA",
    "RS": "SERBIA", "BA": "BOSNIA", "ME": "MONTENEGRO", "NE": "NIGER",
    "BF": "BURKINA FASO", "TD": "CHAD", "ET": "ETHIOPIA", "BI": "BURUNDI",
    "LR": "LIBERIA", "SL": "SIERRA LEONE", "CI": "COTE D'IVOIRE", "KG": "KYRGYZSTAN",

    # THE SECOND HALF OF THIS MAP EXISTS BECAUSE OF WHAT THE FIRST HALF DID
    # WHEN IT ENDED HERE.
    #
    # The map used to stop at the ~48 countries a sanctions programme is NAMED
    # after, on the reasoning that those are the ones that matter. But the
    # `country` argument is the caller's counterparty, not the sanctions
    # regime: someone screening a French supplier passes "FR". Sweeping the
    # live index found 33 country strings - ALGERIA, FRANCE, GERMANY, ISRAEL,
    # SAUDI ARABIA, THE GAMBIA - that no code in the map could reach, so every
    # one of those queries fell through to "we do not know this code".
    #
    # Honest, but useless: the ranking signal silently switched off for most
    # of the world. The unknown branch is the tail, not the common case.
    "DZ": "ALGERIA", "AO": "ANGOLA", "AR": "ARGENTINA", "AM": "ARMENIA",
    "AU": "AUSTRALIA", "AT": "AUSTRIA", "AZ": "AZERBAIJAN", "BH": "BAHRAIN",
    "BD": "BANGLADESH", "BE": "BELGIUM", "BO": "BOLIVIA", "BR": "BRAZIL",
    "BG": "BULGARIA", "KH": "CAMBODIA", "CM": "CAMEROON", "CA": "CANADA",
    "CL": "CHILE", "CO": "COLOMBIA", "CG": "CONGO", "HR": "CROATIA",
    "CY": "CYPRUS", "CZ": "CZECHIA", "DK": "DENMARK", "DO": "DOMINICAN REPUBLIC",
    "EC": "ECUADOR", "SV": "EL SALVADOR", "GQ": "EQUATORIAL GUINEA",
    "EE": "ESTONIA", "FI": "FINLAND", "FR": "FRANCE", "GM": "GAMBIA",
    "GE": "GEORGIA", "DE": "GERMANY", "GH": "GHANA", "GR": "GREECE",
    "GT": "GUATEMALA", "HN": "HONDURAS", "HK": "HONG KONG", "HU": "HUNGARY",
    "IS": "ICELAND", "ID": "INDONESIA", "IE": "IRELAND", "IL": "ISRAEL",
    "IT": "ITALY", "JM": "JAMAICA", "JP": "JAPAN", "JO": "JORDAN",
    "KZ": "KAZAKHSTAN", "KE": "KENYA", "KR": "SOUTH KOREA", "KW": "KUWAIT",
    "LA": "LAOS", "LV": "LATVIA", "LT": "LITHUANIA", "LU": "LUXEMBOURG",
    "MW": "MALAWI", "MY": "MALAYSIA", "MT": "MALTA", "MR": "MAURITANIA",
    "MX": "MEXICO", "MN": "MONGOLIA", "MA": "MOROCCO", "MZ": "MOZAMBIQUE",
    "NP": "NEPAL", "NL": "NETHERLANDS", "NZ": "NEW ZEALAND", "NG": "NIGERIA",
    "NO": "NORWAY", "OM": "OMAN", "PS": "PALESTINE", "PA": "PANAMA",
    "PG": "PAPUA NEW GUINEA", "PY": "PARAGUAY", "PE": "PERU",
    "PH": "PHILIPPINES", "PL": "POLAND", "PT": "PORTUGAL", "QA": "QATAR",
    "RO": "ROMANIA", "RW": "RWANDA", "SA": "SAUDI ARABIA", "SN": "SENEGAL",
    "SG": "SINGAPORE", "SK": "SLOVAKIA", "SI": "SLOVENIA", "ZA": "SOUTH AFRICA",
    "ES": "SPAIN", "LK": "SRI LANKA", "SE": "SWEDEN", "CH": "SWITZERLAND",
    "TW": "TAIWAN", "TJ": "TAJIKISTAN", "TZ": "TANZANIA",
    "TT": "TRINIDAD AND TOBAGO", "TM": "TURKMENISTAN", "UG": "UGANDA",
    "UY": "URUGUAY", "UZ": "UZBEKISTAN", "VN": "VIETNAM", "ZM": "ZAMBIA",
    "AL": "ALBANIA",
}


# Words that turn one country name into a DIFFERENT country. If a listing
# carries one of these and the query does not, they are not the same place -
# SUDAN is not SOUTH SUDAN, GUINEA is not GUINEA-BISSAU, and the two Koreas
# sit at opposite ends of a sanctions regime.
_DISTINGUISHING = frozenset({
    "NORTH", "SOUTH", "EAST", "WEST", "NEW", "EQUATORIAL", "BISSAU",
    "PAPUA", "DEMOCRATIC", "PEOPLES", "PEOPLE", "IVORY", "CENTRAL",
})


# Official long forms and adjectival spellings the feeds actually use.
#
# EXPLICIT, NOT CLEVER. A prefix rule would map RUSSIA -> RUSSIAN in one line
# and NIGER -> NIGERIA in the same line, and Niger and Nigeria are different
# countries that both appear on these lists. Stemming country names is how a
# screening tool corroborates a hit against the wrong nation, so the aliases
# are written down instead of derived.
_COUNTRY_ALIASES = {
    "RU": ["RUSSIAN FEDERATION"],
    "IR": ["ISLAMIC REPUBLIC OF IRAN"],
    "SY": ["SYRIAN ARAB REPUBLIC"],
    # THE MOST SANCTIONED JURISDICTION ON EARTH MATCHED NOTHING.
    #
    # _ISO2_NAMES["KP"] held "KOREA DEMOCRATIC PEOPLES REPUBLIC NORTH KOREA" -
    # four alternative names in one string - and the name-form rule requires
    # EVERY word to appear in the listing. No real listing carries all five,
    # so KP matched none of them. Our own index stores "NORTH KOREA" on real
    # DPRK entities, and they were coming back country_match: FALSE, which the
    # schema defines as "we checked and it is not that country" - and sorting
    # BELOW listings with no country at all.
    "KP": ["DPRK", "KOREA DEMOCRATIC PEOPLES REPUBLIC OF",
           "KOREA DEMOCRATIC PEOPLES REPUBLIC"],
    "VE": ["BOLIVARIAN REPUBLIC OF VENEZUELA"],
    "MM": ["BURMA"],
    # Same shape: "CONGO" alone was blocked by the DEMOCRATIC qualifier that
    # distinguishes it from the Republic of the Congo.
    "CD": ["DRC", "CONGO DEMOCRATIC REPUBLIC"],
    "CI": ["COTE DIVOIRE", "IVORY COAST"],
    "GB": ["UK", "GREAT BRITAIN"],
    "US": ["USA", "UNITED STATES OF AMERICA"],
    "AE": ["UAE"],
    "MD": ["REPUBLIC OF MOLDOVA"],
    "BY": ["REPUBLIC OF BELARUS"],
    # South Korea, spelled the way the feeds spell it. Without this the ONLY
    # form KR matched was the literal words "SOUTH KOREA", while both lists
    # write "KOREA, REPUBLIC OF".
    "KR": ["KOREA REPUBLIC OF", "REPUBLIC OF KOREA"],
    # The listing our index actually holds is "PALESTINIAN" on some rows and
    # "OCCUPIED PALESTINIAN TERRITORIES" on others; neither contains the word
    # PALESTINE, so the plain name form reaches neither.
    "PS": ["PALESTINIAN", "PALESTINIAN TERRITORIES",
           "OCCUPIED PALESTINIAN TERRITORIES", "STATE OF PALESTINE"],
    # "LAO PEOPLE'S DEMOCRATIC REPUBLIC" carries two qualifier words, so the
    # bare "LAO" form is refused by the qualifier rule on purpose - the full
    # official form has to be spelled out to get through it.
    "LA": ["LAO PEOPLES DEMOCRATIC REPUBLIC", "LAO"],
    "CZ": ["CZECH REPUBLIC"],
    "NL": ["THE NETHERLANDS", "HOLLAND"],
    "VN": ["VIET NAM", "SOCIALIST REPUBLIC OF VIETNAM"],
    "TZ": ["UNITED REPUBLIC OF TANZANIA"],
    "TW": ["CHINESE TAIPEI", "TAIWAN PROVINCE OF CHINA"],
    "HK": ["HONG KONG SAR", "HONG KONG SPECIAL ADMINISTRATIVE REGION"],
    "DO": ["DOMINICAN REP"],
}


def _country_matches(want: str, have: list) -> Optional[bool]:
    """Does a listing look connected to `want`?

    True / False / None, where None means the listing records no country -
    which is NOT a mismatch and must never be presented as one.

    THE FIRST VERSION MATCHED RAW SUBSTRINGS AND WAS WRONG TEN WAYS. Measured:

        KP     vs "KOREA, REPUBLIC OF"  -> True   (North Korea query, South
                                                   Korea listing)
        ML     vs "SOMALIA"             -> True   ("MALI" inside "SOMALIA")
        NE     vs "NIGERIA"             -> True
        SD     vs "SOUTH SUDAN"         -> True
        GN     vs "GUINEA-BISSAU"       -> True
        RUSSIA vs "US"                  -> True   (reverse direction: any
                                                   2-letter code inside any
                                                   country name)

    `country_match: true` is corroboration of a hit. Asserting it for the
    wrong country is the same defect as reporting a mismatch we never
    checked, pointed the other way - and on the Korea case it is the
    difference between two countries at opposite ends of a sanctions regime.

    So: whole-word matching only, and a 2-letter code never matches inside a
    longer word.
    """
    if not have:
        return None
    w = (want or "").strip().upper()
    if not w:
        return None

    def _words(text: str) -> set:
        # APOSTROPHES ARE REMOVED, NOT SPLIT ON. Splitting turned
        # "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF" into {..., PEOPLE, S, ...},
        # which no alias could ever be a subset of - so the official long form
        # of the most sanctioned country on earth matched nothing. Sanctions
        # lists are full of possessives and Arabic transliterations
        # ("AL-JAZA'IRI"), so this is general, not a special case.
        return {t for t in re.split(r"[^A-Z0-9]+", text.upper().replace("'", ""))
                if t}

    names = {str(c).strip().upper() for c in have if str(c).strip()}
    if w in names:
        return True

    want_words = _words(w)
    codes_all = set(_ISO2_NAMES)
    # What the caller means, as words: the code's country name, or the name
    # itself, plus any ISO2 codes that spell it.
    want_terms = set(want_words)
    if w in _ISO2_NAMES:
        want_terms |= _words(_ISO2_NAMES[w])
    # Same qualifier rule as the alias loop below: "CONGO" must not pull in
    # the DRC's code, and "KOREA" must not pull in either Korea's.
    codes_for_name = {
        c for c, n in _ISO2_NAMES.items()
        if _words(n) and _words(n) <= want_words
        and not ((want_words - _words(n)) & _DISTINGUISHING)
    }
    want_terms |= codes_for_name
    # The full country name(s) this query stands for, compared as whole
    # phrases rather than loose words.
    name_forms = [w]
    if w in _ISO2_NAMES:
        name_forms.append(_ISO2_NAMES[w])
        name_forms.extend(_COUNTRY_ALIASES.get(w, []))
    # The caller may have typed an alias directly ("Russian Federation").
    #
    # THIS SUBSET TEST IS WHERE THE TWO CONGOS LEAKED, and it is upstream of
    # both qualifier rules, which is why neither could stop it.
    #
    # `_words(w) <= _words(_ISO2_NAMES[code])` fired for w="CONGO" against
    # CD="DEMOCRATIC REPUBLIC OF THE CONGO", because {CONGO} is a subset. That
    # appended the DRC's own name forms and its code to a query meaning the
    # OTHER Congo - and once "DEMOCRATIC REPUBLIC OF THE CONGO" is in
    # name_forms, the name branch has no leftover words to reject on, and once
    # "CD" is in want_terms, want_words minus the DRC's name is empty so the
    # code branch cannot reject either. Both guards defeated by construction.
    #
    # Measured against the live index: 506 rows (250 EU "CD" + 256 UK "CONGO
    # (DEMOCRATIC REPUBLIC)") returned country_match TRUE for a query meaning
    # the Republic of the Congo. The same shape made "KOREA" match ~900 DPRK
    # designations AND South Korea.
    #
    # A qualifier that makes it a different country disqualifies the alias,
    # exactly as it does everywhere else in this function.
    for code, aliases in _COUNTRY_ALIASES.items():
        code_words = _words(_ISO2_NAMES.get(code, ""))
        if w in aliases:
            matched_alias = True
        elif _words(w) and _words(w) <= code_words:
            # Subset only counts when the EXTRA words are not distinguishing.
            matched_alias = not ((code_words - _words(w)) & _DISTINGUISHING)
        else:
            matched_alias = False
        if matched_alias:
            name_forms.append(_ISO2_NAMES.get(code, ""))
            name_forms.extend(aliases)
            want_terms.add(code)

    # A CODE WE CANNOT INTERPRET IS UNKNOWN, NOT A MISMATCH.
    #
    # _ISO2_NAMES started at ~48 sanctions-relevant countries. A caller passing
    # a two-letter code outside that set - "DZ" for Algeria, "BE" for Belgium,
    # both of which appear in our index by NAME - got country_match: false,
    # which the schema defines as "we checked and it is not that country". We
    # had not checked; we did not know the word.
    #
    # This is the same defect as the KP mapping, one level up: asserting a
    # negative from ignorance. Sweeping the live index found 33 country
    # strings no code in the map could reach, so the map was widened to cover
    # them. It is still not every ISO code, and it never will be - which is
    # exactly why the unknown case has to answer None rather than guess.
    if len(w) == 2 and w not in _ISO2_NAMES:
        # UNLESS THE LISTING IS ALSO A BARE CODE. Two two-letter codes are
        # directly comparable whether or not we know what either stands for,
        # and the exact-match check above already handled the equal case - so
        # reaching here with all-code listings is a genuine mismatch, not
        # ignorance. Anything else (a country NAME we cannot map the code to)
        # stays unknown.
        if names and all(len(n) == 2 and n.isalpha() for n in names):
            return False
        return None

    for n in names:
        listing_words = _words(n)
        # A 2-letter code must BE one of the listing's words, never a
        # fragment of one - that is what made RUSSIA match "US".
        if want_terms & codes_all & listing_words:
            # THE QUALIFIER RULE APPLIES HERE TOO. This branch skipped it, so
            # a query naming a qualified country still matched the unqualified
            # code: "SOUTH SUDAN" vs ["SD"] -> True, "GUINEA-BISSAU" vs ["GN"]
            # -> True, "REPUBLIC OF THE CONGO" vs ["CD"] (the DRC) -> True.
            # The reverse direction was already refused; this is the same
            # false corroboration pointing the other way.
            if not (want_words - _words(_ISO2_NAMES.get(
                    next(iter(want_terms & codes_all & listing_words)), ""))
                    ) & _DISTINGUISHING:
                return True
        # Otherwise the country NAME must appear in full, and the listing must
        # not carry a qualifier that makes it a DIFFERENT country.
        #
        # Shared-word matching is not enough here, because so many country
        # names contain another: SUDAN in SOUTH SUDAN, GUINEA in
        # GUINEA-BISSAU and EQUATORIAL GUINEA, KOREA in both Koreas. Those
        # four survived the first rewrite of this function.
        for cand in name_forms:
            cw = _words(cand)
            if not cw or not cw <= listing_words:
                continue
            if (listing_words - cw) & _DISTINGUISHING:
                continue                        # "SOUTH" SUDAN is not SUDAN
            return True
    return False

def _is_screenable(toks: set) -> Optional[str]:
    """Can this name carry a finding at all? Returns a REASON if it cannot.

    THE DATABASE PATH BYPASSED EVERY SAFETY LAYER IN _word_match_score.
    Moving EU/UK to an indexed exact-key lookup made matching faster and
    dropped, silently, the two guards that file spends 140 lines justifying.
    Measured live on the deployed service before this was added:

        "Dave"      -> MATCH on EU (TAQA) and UK ("Isil (Da'esh) and Al-Qaeda")
        "Said"      -> MATCH on EU (TERR) and UK Counter-Terrorism regs
        "Universal" -> MATCH on OFAC (RUSSIA-EO14024) and UK Russia regs
        "East"      -> MATCH on UK Russia regs
        "OOO"       -> MATCH on EU (SYR)

    Those are not near-misses; they were returned as FINDINGS, with a
    programme name attached, to anyone who asked. Telling a customer their
    counterparty appears on an Al-Qaeda list because the name is "Dave" is
    the worst output this product can produce.

    The index really does contain those rows - 16 whose entire key is generic
    words, and dozens of 2-3 character keys like "ig", "ao", "rim". They are
    legitimate entries whose short form is simply not enough to identify
    anyone by name alone.

    Both rules are lifted from _word_match_score so the two paths cannot
    disagree again:
      * a single token under 4 characters carries no information ("Rosneft"
        is 7 and must still match; "ig" is 2 and must not);
      * a name made only of legal forms and generic words identifies nobody.
    """
    if not toks:
        return "the name contains no Latin-script characters to match on"
    # THE RULE IS ABOUT DISTINCTIVENESS, NOT TOKEN COUNT.
    #
    # This used to fire only when the WHOLE query was one token, so a name
    # made of two short tokens sailed past it into the strict token-set
    # filter and was ASSERTED as a finding. Measured on production:
    #
    #     screen_sanctions("Li Na")
    #       -> MATCH FOUND: 'LI, Na' on OFAC-SDN, program NPWMD] [IFSR
    #
    # "Li Na" is one of the most common names in China, returned as an
    # Iran-WMD-proliferation finding. Identical harm to the "Dave" case this
    # guard was written for, and the guard did not cover it because I keyed it
    # on how MANY tokens there were instead of how much information they
    # carry.
    #
    # A name needs at least one token long enough to identify somebody.
    # "Rosneft" is 7 characters and must still match; "li", "na", "kim", "il",
    # "ao" carry nothing on their own and carry nothing together either.
    distinctive = toks - _GENERIC_NAME_WORDS
    if not any(len(t) >= 4 for t in (distinctive or toks)):
        longest = max((distinctive or toks), key=len)
        return (f"no token in this name is 4 characters or longer (longest: "
                f"'{longest}') - too short to identify anyone by name")
    if not (toks - _GENERIC_NAME_WORDS):
        return ("the name consists only of generic and legal-form words, "
                "which identify no specific party")
    return None


# ---------------------------------------------------------------------------
# ARABIC-SCRIPT AND TRANSLITERATION-AWARE MATCHING  (core/arabic_names.py)
# ---------------------------------------------------------------------------
#
# WHY. An Arabic-script name normalised to nothing and so matched nothing, and a
# Latin-script Arabic name matched only if the SPELLING was identical - while
# the lists themselves carry six romanisations of one man inside one entry. For
# an Oman-registered company whose customers write in Arabic, that was the
# largest hole in this tool. core/arabic_names.py compares names by sound;
# this section wires it into the three lists.
#
# THE RULE THAT DOES NOT CHANGE: A SOUND-ALIKE IS A CANDIDATE, NEVER A FINDING.
# Candidates carry match_confidence (high or medium), the element-by-element
# alignment that produced them, and a sentence saying what kind of evidence they
# are. The one new kind of finding is deliberately narrow: an Arabic-script
# query that is, element for element and after folding only spelling
# conventions, an Arabic-script ALIAS the publisher itself printed. That is the
# same evidence as a Latin token-set equality, in the other script.
#
# LOW-CONFIDENCE ALIGNMENTS ARE COUNTED, NOT LISTED. A pair that shares only
# very common Arab name elements ("Muhammad Ali") is a coincidence, and listing
# it would teach callers to ignore the field. The count is reported
# (arabic_matching.low_confidence_not_listed) so the omission is visible.
#
# AN ARABIC-SCRIPT QUERY IS NEVER REPORTED "CLEAN". It is matched against Latin
# list entries by transliteration, which is lossy by nature, so an empty result
# means "nothing found by sound or spelling" - and the screen reports `partial`
# with that sentence, exactly as a reduced screen does.

_PHONETIC_STATS: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "screen_sanctions_phonetic_stats", default=None)

_PHONETIC_FETCH_LIMIT = 150     # index rows pulled per lookup pattern
_PHONETIC_PER_LIST = 5          # candidates reported per list
_PHONETIC_LISTED = ("medium", "high")
_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}

_ARABIC_SCRIPT_NOTE = (
    "Arabic-script query: matched against Latin-script list entries by "
    "transliteration (sound) and against Arabic-script aliases by spelling. "
    "Romanisation is not standardised, so a spelling this matcher does not "
    "generate can be missed - an empty result means nothing was found by sound "
    "or spelling, not that the party is clear")

_ARABIC_METHOD = (
    "Arabic-aware layer (core/arabic_names.py): the article, filial particles "
    "(bin, ibn, bint), titles and compound elements (Abd al-X, Abu X, X al-Din) "
    "are normalised; Arabic spelling conventions are folded; and Arabic and "
    "Latin name elements are compared by consonant skeleton with a weighted edit "
    "distance. A sound-based match is a CANDIDATE with a stated confidence, "
    "never a finding. Only an Arabic-script query equal to an Arabic-script alias "
    "the publisher printed is treated as a finding.")


def _bump(key: str, n: int = 1) -> None:
    stats = _PHONETIC_STATS.get()
    if stats is not None and n:
        stats[key] = stats.get(key, 0) + n


def _alignment_fields(al) -> dict:
    """The structured evidence for one sound-based match. The listed element
    strings are the publisher's words reduced to bare letters; they are fenced
    as untrusted like every other list-derived string (core/untrusted.py)."""
    return {
        "match_basis": al.basis,
        "match_confidence": al.confidence,
        "match_explanation": al.explanation,
        "token_alignment": [
            {"query_element": _ascii(q), "listed_element": _ascii(lst),
             "similarity": sim, "relation": how}
            for q, lst, sim, how in al.pairs],
        "query_elements_unmatched": [_ascii(x) for x in al.unmatched_query],
        "listed_elements_unmatched": len(al.unmatched_listed),
    }


def _sound_match(al, *, name, list_label, source_url, program, etype,
                 countries, want_country, matcher, primary=None) -> dict:
    m = {
        "name": _ascii(name),
        "list": list_label,
        # A name-level similarity in 0..1. NOT comparable with the token-overlap
        # score on exact matches, and uncalibrated like it.
        "match_score": round(al.score, 2),
        "program": _ascii(program or "") or None,
        "entity_type": etype or "ENTITY",
        "source_url": source_url,
        "_matcher": matcher,
    }
    m.update(_alignment_fields(al))
    if countries is not None:
        m["countries"] = [_ascii(c) for c in countries] or None
    if want_country:
        m["country_match"] = _country_matches(want_country, countries or [])
    if primary and _ascii(primary) != m["name"]:
        m["listed_primary_name"] = _ascii(primary)
    return m


async def _arabic_db_matches(aq, list_code: str, list_label: str,
                             source_url: str, want_country: Optional[str],
                             version: Optional[str] = None,
                             ) -> tuple[list[dict], list[str]]:
    """Arabic-script and sound-based matches from the EU or UK index.

    Returns (matches, notes). `notes` are disclosures about a lookup that could
    not run or hit its limit; the caller files them as reduced-screen notes.
    `version` is the list's refresh day, which says which copy of the list an
    in-process sound index was built from.

    TWO LOOKUPS, because the two kinds of row need different handling:

      * Arabic-script ALIAS rows (the publisher printed them) are found by the
        Arabic name_key and by token containment, exactly as Latin rows are.
      * Latin rows are found by SOUND, from the in-process index of the list
        (see "In-process sound indexes"). While that index is still being built,
        or if it cannot be built, the database is asked instead: one regular
        expression per identifying element over-fetches the rows that could
        contain it and arabic_names.compare() re-scores what came back. That
        path needs no new column and no re-index, and it reaches the right row
        less often, so a call that had to use it says so.
    """
    from storage.supabase_client import (
        select_rows_strict, SupabaseUnavailable, RawFilter)

    notes: list[str] = []
    found: dict[str, tuple] = {}
    low = 0
    arabic_script = aq.script in ("arabic", "mixed")

    def consider(r: dict, al, matcher: str) -> None:
        nonlocal low
        if al is None:
            return
        if matcher != "arabic_script_exact" and al.confidence not in _PHONETIC_LISTED:
            low += 1
            return
        who = r.get("entity_id") or r.get("name_key") or r.get("display_name")
        rank = (1 if matcher == "arabic_script_exact" else 0,
                _CONF_ORDER.get(al.confidence, 0), al.score)
        prev = found.get(who)
        if prev is not None and prev[0] >= rank:
            return
        found[who] = (rank, al, r, matcher)

    # 1. Arabic-script aliases ------------------------------------------------
    if arabic_script and aq.arabic_all_key:
        key = " ".join(aq.arabic_all_key)
        try:
            rows = list(await select_rows_strict(
                "sanctions_names",
                filters={"list_code": list_code, "name_key": key}, limit=5))
            if len(aq.arabic_key) >= 2:
                sup = await select_rows_strict(
                    "sanctions_names",
                    filters={"list_code": list_code,
                             "tokens": "cs.{" + ",".join(aq.arabic_key) + "}"},
                    limit=8)
                have = {r.get("name_key") for r in rows}
                rows += [r for r in sup if r.get("name_key") not in have]
        except SupabaseUnavailable:
            rows = []
            notes.append(
                f"{list_label} (Arabic-script alias lookup unavailable on this "
                f"call; Arabic spelling was NOT checked)")
        for r in rows:
            # The one finding this feature adds is "the query IS the printed alias". That is a claim about the
            # WHOLE query, so it needs a query with no Latin name element beside the Arabic ones; a mixed query
            # is scored like any other row and is at most a candidate.
            if r.get("name_key") == key and _ar.is_arabic_only(aq):
                consider(r, _ar.exact_alignment(aq), "arabic_script_exact")
            else:
                consider(r, _ar.compare(aq, _ar.analyse(r.get("display_name") or "")),
                         "name_sound_match")

    # 2. Latin rows, by sound -------------------------------------------------
    served_by_index = False
    if aq.phonetic_ok and version:
        try:
            idx, info = await _sound_index(
                _list_phonetic_state.setdefault(list_code, _new_slot()), version,
                lambda: _build_list_phonetic_index(list_code))
            hits = await asyncio.to_thread(idx.search, aq, 60, "low")
            for al, i in hits:
                ent, prog, etype, countries = info[idx.meta[i]]
                consider({"entity_id": ent, "display_name": idx.names[i],
                          "programme": prog, "etype": etype,
                          "countries": list(countries)}, al, "name_sound_match")
            served_by_index = True
        except _PhoneticIndexNotReady:
            notes.append(
                f"{list_label} (the sound index for this list was still being built; "
                f"a narrower database lookup was used on this call, which can miss "
                f"names written as one word in one list and two in another)")
        except Exception:                       # noqa: BLE001
            notes.append(
                f"{list_label} (the sound index for this list could not be built; "
                f"a narrower database lookup was used on this call, which can miss "
                f"names written as one word in one list and two in another)")
    if aq.phonetic_ok and not served_by_index:
        if not version:
            notes.append(
                f"{list_label} (no refresh date for the list, so no sound index could "
                f"be used; a narrower database lookup was used on this call, which can "
                f"miss names written as one word in one list and two in another)")

        async def fetch(extra_filters: list[dict]) -> list:
            return await asyncio.gather(*[
                select_rows_strict(
                    "sanctions_names",
                    # RawFilter: these values are built here from consonant
                    # classes, never from the caller's text, and use operators
                    # (`imatch`, a logic tree) that plain values may not.
                    filters=dict({k: RawFilter(v) for k, v in f.items()},
                                 list_code=list_code),
                    order="name_key.asc", limit=_PHONETIC_FETCH_LIMIT)
                for f in extra_filters], return_exceptions=True)

        # A regular expression per identifying element (PostgREST `imatch`): the
        # consonants in order, the confusable spellings of each as alternatives,
        # vowels free, anchored to whole tokens. Selective enough that the row
        # limit is rarely reached. If the server refuses the operator, fall back
        # to ILIKE patterns of the stable consonants (broader, same results after
        # re-scoring, but it reaches the row limit sooner).
        values = _ar.retrieval_filters(aq)
        results = await fetch(values) if values else []
        if values and any(isinstance(r, SupabaseUnavailable) for r in results):
            values = [{"name_key": "ilike." + p} for p in _ar.retrieval_patterns(aq)]
            results = await fetch(values) if values else []
        if not values:
            notes.append(
                f"{list_label} (this name has too few stable consonants to be "
                f"looked up by sound; only exact spelling was checked)")
        fetched: dict[str, dict] = {}
        truncated = False
        for res in results:
            if isinstance(res, SupabaseUnavailable):
                notes.append(
                    f"{list_label} (sound lookup unavailable on this call; only "
                    f"exact spelling was checked)")
                break
            if isinstance(res, BaseException):
                raise res
            if len(res) >= _PHONETIC_FETCH_LIMIT:
                truncated = True
            for r in res:
                fetched.setdefault(str(r.get("name_key")), r)
        if truncated:
            notes.append(
                f"{list_label} (a very common name: the sound lookup reached its "
                f"row limit, so further candidates may exist)")
        for r in fetched.values():
            nm = r.get("display_name") or ""
            if _ar.has_arabic_script(nm):
                continue
            consider(r, _ar.compare(aq, _ar.analyse(nm)), "name_sound_match")

    _bump("low_confidence_not_listed", low)
    ranked = sorted(found.values(), key=lambda t: t[0], reverse=True)[:_PHONETIC_PER_LIST]

    # An Arabic alias on its own is hard to act on for a reader of English: say
    # whose alias it is. Bounded to the top few hits; failure just omits it.
    primaries: dict[int, str] = {}
    for n, (rank, al, r, matcher) in enumerate(ranked[:3]):
        nm = r.get("display_name") or ""
        if _ar.has_arabic_script(nm) and r.get("entity_id"):
            try:
                sib = await select_rows_strict(
                    "sanctions_names",
                    filters={"list_code": list_code, "entity_id": r["entity_id"]},
                    limit=12)
            except SupabaseUnavailable:
                continue
            for srow in sib:
                snm = srow.get("display_name") or ""
                if snm and not _ar.has_arabic_script(snm):
                    primaries[n] = snm
                    break

    out = []
    for n, (rank, al, r, matcher) in enumerate(ranked):
        out.append(_sound_match(
            al, name=r.get("display_name") or "", list_label=list_label,
            source_url=source_url, program=r.get("programme"),
            etype=r.get("etype"), countries=r.get("countries") or [],
            want_country=want_country, matcher=matcher,
            primary=primaries.get(n)))
    return out, notes


# --- In-process sound indexes: OFAC, EU, UK ---------------------------------
#
# Finding the names that SOUND like a query means comparing it with every name
# on a list. The OFAC list lives in this process already (parsed from a CSV), and
# the EU and UK lists are read out of the index table once and kept the same way:
# each list is analysed ONCE per copy into a SkeletonIndex (core/arabic_names.py)
# and a query is compared with the few hundred names that index returns.
#
# WHY NOT ASK THE DATABASE FOR THE ROWS. That was the first design (a regular
# expression per name element, see _arabic_db_matches), and it is kept as the
# fallback, but measured against the same 150 gold entries it reached the right
# row only 80% of the time: a regex over a sorted token string cannot follow a
# name that is spelled as one word in one list and two in another (Gholam Reza /
# Gholamreza, Abd al-Hai / Abdulhai, Shams-abad / Shamsabad), and it hits its row
# limit on any query built from common names. The index has neither problem.
#
# Each index is built in a thread of its own, so a request that stops waiting for
# it does not stop it, and a rebuild for a newer copy of the list runs while the
# previous copy keeps answering.

_SOUND_INDEX_WAIT_S = 9.0     # how long ONE request waits for a first build
_SOUND_FAIL_BACKOFF_S = 60.0  # after a failed build, do not retry sooner than this
_LIST_PAGE = 1000
_LIST_PAGE_CAP = 400          # 400 pages of 1000: ten times the biggest list
_sound_guard = threading.Lock()


def _new_slot() -> dict:
    return {"key": None, "index": None, "info": None,
            "future": None, "future_key": None, "failed_at": 0.0}


_ofac_phonetic_state: dict = _new_slot()
_list_phonetic_state: dict[str, dict] = {"EU": _new_slot(), "UK": _new_slot()}


class _PhoneticIndexNotReady(Exception):
    """A sound index was still being built when this call stopped waiting for it."""


def _start_index_build(slot: dict, key, work):
    """Start (or join) the build of `slot` for `key`; `work()` returns
    (index, info). Returns a concurrent.futures.Future, or None when the slot
    already holds `key`.

    A thread and a concurrent Future, not an asyncio task: the index outlives any
    one request, and a task would die with the event loop that made it."""
    from concurrent.futures import Future

    with _sound_guard:
        if slot["key"] == key and slot["index"] is not None:
            return None
        running = slot["future"]
        if running is not None and slot["future_key"] == key:
            if not running.done():
                return running
            if (running.exception() is not None
                    and time.time() - slot["failed_at"] < _SOUND_FAIL_BACKOFF_S):
                return running              # failed a moment ago: report that, do not retry
        fut: Future = Future()
        slot["future"], slot["future_key"] = fut, key

    def run() -> None:
        try:
            idx, info = work()
        except BaseException as exc:            # noqa: BLE001
            with _sound_guard:
                slot["failed_at"] = time.time()
            fut.set_exception(exc)
            return
        with _sound_guard:
            # Publish only while this is still the build the slot wants. A newer
            # copy of the list may have been asked for while this one ran, and
            # whichever finishes LAST must not overwrite the newer with the older.
            if slot["future"] is fut:
                slot.update(key=key, index=idx, info=info)
        fut.set_result(True)

    try:
        threading.Thread(target=run, name="sound-index", daemon=True).start()
    except BaseException as exc:                # noqa: BLE001
        # No thread to run it: fail the published Future now, or every request
        # for this copy of the list would join a build nobody is doing.
        with _sound_guard:
            slot["failed_at"] = time.time()
        fut.set_exception(exc)
    return fut


async def _sound_index(slot: dict, key, work):
    """(index, info) for `key`. Builds it if need be; if an older copy is already
    held it is served while the new one builds (the lists change once a day and
    the table is already gated on its own age), otherwise this call waits up to
    _SOUND_INDEX_WAIT_S and then raises _PhoneticIndexNotReady."""
    fut = _start_index_build(slot, key, work)
    if fut is not None:
        with _sound_guard:
            have_older = slot["index"] is not None
        if not have_older:
            # SHIELDED: wait_for cancels what it waits on when the time is up, and
            # cancelling an asyncio future made by wrap_future cancels the build's
            # own Future - the build would finish and then fail to record its
            # result, for the next request as well. The shield takes the
            # cancellation instead. (Nor is this wait made in an executor thread:
            # concurrent first calls would fill the pool and queue behind each
            # other's timeouts, so the bound would not be a bound.)
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)),
                                       _SOUND_INDEX_WAIT_S)
            except asyncio.TimeoutError:
                raise _PhoneticIndexNotReady() from None
    with _sound_guard:
        if slot["index"] is None:
            raise _PhoneticIndexNotReady()
        return slot["index"], slot["info"]


async def _load_list_rows(list_code: str) -> list[dict]:
    """Every row of one list in the index table, read a page at a time.

    Pages by what the server actually returned, not by what was asked for: a
    server that caps a page below _LIST_PAGE must not be taken for the end of the
    table. Stops at an empty page, and if a page begins where the last one did
    (a server that ignores `offset`) rather than reading the same page for ever."""
    from storage.supabase_client import select_rows_strict

    keep = ("name_key", "display_name", "entity_id", "programme", "etype", "countries")
    rows: list[dict] = []
    offset = 0
    last_first = None
    for _ in range(_LIST_PAGE_CAP):
        page = await select_rows_strict(
            "sanctions_names", filters={"list_code": list_code},
            order="name_key.asc", limit=_LIST_PAGE, offset=offset)
        if not page:
            return rows
        first = page[0].get("name_key")
        if last_first is not None and first == last_first:
            # (list_code, name_key) is unique, so two pages never begin alike
            # unless the server is not honouring `offset`. A partial list must
            # FAIL the build, not become the index: the caller then says the
            # sound index could not be built and uses the database instead.
            raise RuntimeError(f"{list_code}: the server repeated a page, so the "
                               f"list was not read completely")
        last_first = first
        rows.extend({k: r.get(k) for k in keep} for r in page)
        offset += len(page)
    raise RuntimeError(f"{list_code}: more than {_LIST_PAGE_CAP} pages, which is "
                       f"not a list this code has seen; refusing a partial index")


def _build_list_phonetic_index(list_code: str):
    """Read one list out of the index table and index its Latin names by sound.
    Runs in a worker thread, so it brings its own event loop for the reads."""
    rows = asyncio.run(_load_list_rows(list_code))
    if not rows:
        raise RuntimeError(f"{list_code}: the index table returned no rows")
    idx = _ar.SkeletonIndex()
    info: list[tuple] = []
    for r in rows:
        nm = r.get("display_name") or ""
        # Arabic-script rows are found by spelling (see _arabic_db_matches); the
        # sound index holds the Latin ones, which is what an Arabic query is
        # compared against.
        if not nm or _ar.has_arabic_script(nm):
            continue
        idx.add(nm, len(info))
        info.append((sys.intern(str(r.get("entity_id") or "")), r.get("programme"),
                     sys.intern(r.get("etype") or "ENTITY"),
                     tuple(r.get("countries") or ())))
    return idx.freeze(), info


def _build_ofac_phonetic_index(csv_text: str, alt_text: Optional[str]):
    """Index every OFAC name and alias by sound. CPU-heavy (a few seconds), done
    once per copy of the list, and run in a worker thread."""
    idx = _ar.SkeletonIndex()
    info: dict[str, tuple] = {}
    aliases: dict[str, list[str]] = {}
    if alt_text:
        try:
            for row in csv.reader(io.StringIO(alt_text)):
                if len(row) < 4:
                    continue
                ent, alt = row[0].strip(), (row[3] or "").strip()
                if ent and alt and alt != "-0-":
                    aliases.setdefault(ent, []).append(alt)
        except Exception:                       # noqa: BLE001
            pass
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 4:
            continue
        ent = row[0].strip()
        primary = (row[1] or "").strip()
        if not primary or primary == "-0-" or ent in info:
            continue
        sdn_type = (row[2] or "").strip().lower()
        etype = ("INDIVIDUAL" if sdn_type == "individual"
                 else "VESSEL" if sdn_type == "vessel"
                 else "AIRCRAFT" if sdn_type == "aircraft" else "ENTITY")
        program = (row[3] or "").strip()
        info[ent] = (primary, "" if program == "-0-" else program[:80], etype)
        for nm in [primary] + aliases.get(ent, []):
            if re.search(r"[A-Za-z]", nm):
                idx.add(nm, ent)
    return idx.freeze(), info


async def _ofac_phonetic_matches(csv_text: str, alt_text: Optional[str], aq,
                                 ) -> list[dict]:
    # The version of the index is a digest of BOTH texts in full. The first version keyed on the lengths and the
    # hash of the first and last few KB, so an in-place correction of the same length (a typo fixed in the
    # middle of the file) left the key unchanged and the stale index kept answering until a restart.
    key = (hashlib.blake2b(csv_text.encode("utf-8", "replace"), digest_size=16).hexdigest(),
           hashlib.blake2b((alt_text or "").encode("utf-8", "replace"), digest_size=16).hexdigest())
    idx, info = await _sound_index(
        _ofac_phonetic_state, key,
        lambda: _build_ofac_phonetic_index(csv_text, alt_text))
    hits = await asyncio.to_thread(idx.search, aq, 60, "low")

    best: dict[str, tuple] = {}
    low = 0
    for al, i in hits:
        if al.confidence not in _PHONETIC_LISTED:
            low += 1
            continue
        ent = idx.meta[i]
        rank = (_CONF_ORDER.get(al.confidence, 0), al.score)
        if ent not in best or best[ent][0] < rank:
            best[ent] = (rank, al, idx.names[i])
    _bump("low_confidence_not_listed", low)
    out = []
    for ent, (rank, al, listed_name) in sorted(
            best.items(), key=lambda kv: kv[1][0], reverse=True)[:_PHONETIC_PER_LIST]:
        primary, program, etype = info[ent]
        out.append(_sound_match(
            al, name=listed_name, list_label="OFAC-SDN",
            source_url="https://sanctionssearch.ofac.treas.gov/",
            program=program, etype=etype, countries=None, want_country=None,
            matcher="name_sound_match", primary=primary))
    return out


async def _screen_list_db(name: str, list_code: str, list_label: str,
                          source_url: str,
                          want_country: Optional[str] = None,
                          ) -> tuple[list[dict], list[str], list[str]]:
    """Screen one list from the DATABASE index.

    Replaces holding 244MB of parsed list in every worker, which OOM-killed the
    instance and served 502 to everything for several minutes. Postgres does
    the lookup on an index; the process holds nothing.

    TWO QUERIES, because a screen needs two different relationships:

      * EXACT `name_key` - the normalised tokens, sorted. Identical token sets.
      * SUPERSET - listed entries whose tokens include every token of the
        query. "Rosneft Trading" against a listed "ROSNEFT TRADING S.A." is
        that shape, and so is "Vladimir Putin" against the EU's "Vladimir
        Vladimirovich PUTIN" - which is a real, currently-sanctioned person we
        would otherwise return NOTHING for.

    BOTH COME BACK IN ONE LIST, tagged `local_word_overlap`. The strict filter
    in screen_sanctions() is what decides which are findings and which are
    `possible_matches_unverified`, and it applies the same token-set rule this
    function would have applied. Splitting them here would mean two places
    deciding the same question, free to disagree after the next edit; the
    filter that already carries the reasoning stays the only judge.
    """
    # STRICT, because this function's whole contract is that "nothing matched"
    # and "I could not check" are different answers. select_rows() returns []
    # for both, so the except-handler below was dead code from the moment I
    # wrote it - as the fix for a bug whose lesson was exactly this.
    from storage.supabase_client import (
        select_rows, select_rows_strict, SupabaseUnavailable)

    toks = sorted(set(_normalize_name(name).split()))
    aq = _ar.analyse(name)
    arabic_script = aq.script in ("arabic", "mixed")

    # A NAME WE CANNOT SCREEN IS NOT A CLEAN SCREEN.
    #
    # This used to `return [], [], []` - no matches, nothing queried, nothing
    # unavailable - so handle_screen_sanctions went on to list EU and UK under
    # lists_screened. Measured on the live service: a Cyrillic query for a
    # sanctioned individual returned matched=false, reason_code=no_match, and
    # claimed all three lists had been screened. Total confidence, zero
    # coverage, on exactly the populations these lists are full of.
    # ...BUT REFUSING TO LOOK IS NOT A SAFE DEFAULT EITHER.
    #
    # This used to return immediately, screening nothing. Measured against the
    # live index, that silence covered 1.5% of all entries - and the entries
    # it covered include the ones a compliance user is most likely to type:
    #
    #     GRU  -> listed on EU and UK    (Russian military intelligence)
    #     M23  -> listed on EU and UK    (the DRC armed group)
    #     PIJ  -> listed on EU
    #
    # Screening "GRU" returned matched: false under the sentence "No matches
    # on the screened lists". The refusal was disclosed further down, but the
    # field a program branches on and the sentence a human reads both said
    # clean, about an entity that is on two of the three lists we screen.
    #
    # The harm this rule was written for came from the SUPERSET query - "Dave"
    # reaching "Isil (Da'esh) and Al-Qaeda" because one shared word is enough
    # to be a subset. An EXACT whole-name match cannot do that: it fires only
    # when the listed name IS the query. That is a weak signal on a 3-letter
    # name and a real one, so it is surfaced as a candidate and never asserted
    # as a finding - which is the same treatment single-token queries already
    # get, for the same reason.
    # An Arabic-script name has no Latin tokens, so _is_screenable would call it
    # unscreenable; the Arabic layer decides instead (see arabic_names).
    weak_only = aq.weak_reason if arabic_script else _is_screenable(set(toks))

    refreshed = await _list_refreshed_at(list_code)
    age = _days_since(refreshed)
    stamp = f"local index refreshed {refreshed}" if refreshed else "local index, age unknown"
    queried = [f"{list_label} ({stamp})"]
    if weak_only:
        # Reported as a REDUCED screen, not an absent one. The old wording
        # ("NOT screened") was true when nothing ran; saying it now would
        # understate coverage the same way the silence overstated it.
        partial_note = (
            f"{list_label} (reduced screen: {weak_only} - only EXACT "
            f"whole-name matches were checked, and any hit is reported as an "
            f"unverified candidate rather than a finding)")
    else:
        partial_note = None

    # A copy this old is not a screen. Say so instead of answering from it.
    if age is not None and age > _STALE_AFTER_DAYS:
        return [], queried, [
            f"{list_label} (local index is {age} days old, older than the "
            f"{_STALE_AFTER_DAYS}-day limit; NOT screened on this call)"]

    # AN AGE WE COULD NOT READ IS NOT A FRESH ONE.
    #
    # _list_refreshed_at swallows every exception and returns None, so
    # _days_since(None) is None and the gate above - `age is not None and ...`
    # - cannot fire. The list was then counted in lists_screened as "local
    # index, age unknown" and the screen reported CLEAN. The stamp said
    # unknown; the decision treated it as fresh, which is the gap between what
    # a receipt says and what it means.
    #
    # It matters most exactly when it is most likely: the age query fails
    # because Supabase is struggling, which is also when the refresh job may
    # have been failing for days.
    if age is None:
        return [], queried, [
            f"{list_label} (could not read when this index was last "
            f"refreshed, so its age cannot be checked against the "
            f"{_STALE_AFTER_DAYS}-day limit; NOT screened on this call)"]

    def _to_match(r: dict, score: float) -> dict:
        return {
            "name": _ascii(r.get("display_name", "")),
            "list": list_label,
            "match_score": round(score, 2),
            "program": _ascii(r.get("programme") or "") or None,
            "entity_type": r.get("etype") or "ENTITY",
            "source_url": source_url,
            # UNCALIBRATED, exactly like our OFAC matcher: no name-frequency
            # data behind the number. The tag is what keeps the strict filter
            # applying to these too.
            "_matcher": "local_word_overlap",
            "countries": [_ascii(c) for c in (r.get("countries") or [])] or None,
        }

    exact: Optional[list] = []
    if toks:                                    # an Arabic-script name has no Latin key
        try:
            exact = await select_rows_strict(
                "sanctions_names",
                filters={"list_code": list_code, "name_key": " ".join(toks)},
                limit=5,
            )
        except SupabaseUnavailable:
            exact = None                        # distinguish failure from empty

    if exact is None:
        # NOT SCREENED, and the caller is told so. An index we could not reach
        # must never read as a clean result on this list.
        return [], queried, [
            f"{list_label} (name index unreachable; NOT screened on this call)"]

    rows = list(exact)
    seen = {r.get("name_key") for r in exact}
    partial: list[str] = [partial_note] if partial_note else []

    # AN EMPTY INDEX IS NOT A CLEAN SCREEN.
    #
    # Found by deleting the EU rows during a test of the refresh sweep: with
    # the table empty, this function returned "no matches, nothing
    # unavailable" - and the tool reported EU-CONSOLIDATED as SCREENED with a
    # clean result. The most dangerous possible output: total confidence,
    # zero coverage.
    #
    # "No row matched" and "there are no rows" are different answers and must
    # never collapse into each other. When nothing matched, prove the list is
    # actually loaded before reporting a clean screen.
    if not rows:
        try:
            probe = await select_rows("sanctions_names",
                                      filters={"list_code": list_code}, limit=1)
        except Exception:                       # noqa: BLE001
            probe = []
        if not probe:
            return [], queried, [
                f"{list_label} (local index is EMPTY -- NOT screened on this "
                f"call; the list needs reloading)"]

    # Superset candidates. Skipped for a single-token query: "smith" alone
    # would drag back every Smith on the list, which is noise, not a candidate.
    # Skipped for a weak name too - this is the query that turned "Dave" into
    # an Al-Qaeda programme hit, and the exact lookup above cannot.
    if len(toks) >= 2 and not weak_only:
        try:
            sup = await select_rows_strict(
                "sanctions_names",
                filters={"list_code": list_code,
                         "tokens": "cs.{" + ",".join(toks) + "}"},
                limit=8,
            )
            for r in sup:
                if r.get("name_key") in seen:
                    continue
                seen.add(r.get("name_key"))
                rows.append(r)
        except SupabaseUnavailable:
            # THIS IS THE QUERY THAT FINDS "Vladimir Putin" INSIDE THE EU'S
            # "Vladimir Vladimirovich PUTIN". Losing it does not fail the
            # screen - the exact lookup already succeeded - but it DOES narrow
            # coverage, so it is disclosed rather than swallowed. Silently
            # returning a narrower screen under the same clean verdict is the
            # failure this whole file is arranged against.
            partial.append(
                f"{list_label} (near-match lookup unavailable; only exact "
                f"name matches were checked on this call)")

    out = []
    for r in rows:
        rt = set(r.get("tokens") or [])
        _cm = _country_matches(want_country, r.get("countries") or [])             if want_country else None
        # Proportion of the LISTED name our query accounts for: 1.0 when the
        # token sets are identical, lower the more extra words the listing has.
        score = (len(set(toks) & rt) / len(rt)) if rt else 0.0
        m = _to_match(r, score)
        if weak_only:
            # Tagged so the strict filter in handle_screen_sanctions demotes
            # it, exactly as it demotes a single-token query. The reason
            # travels with the candidate so the caller sees WHY it is a
            # candidate rather than a finding.
            m["_single_token_query"] = True
            m["_weak_name_reason"] = weak_only
        if want_country:
            # Three states, and "unknown" is its own answer. Collapsing it into
            # False would tell a caller we had checked and ruled the country
            # out, when the listing simply does not record one.
            m["country_match"] = _cm
        out.append(m)

    # Arabic-script aliases and sound-based candidates (core/arabic_names.py).
    # Only for a name with Arabic script or positive evidence of an Arab name
    # (structure, or a given name that is not also a common Western one).
    # Measured: 0 of 125 ordinary English names reach this
    # (docs/ARABIC_SANCTIONS_EVAL.md), which is what keeps every behaviour
    # pinned by the existing tests unchanged.
    if aq.engaged:
        try:
            extra, notes = await _arabic_db_matches(
                aq, list_code, list_label, source_url, want_country,
                version=refreshed)
        except Exception:                       # noqa: BLE001
            extra, notes = [], [
                f"{list_label} (the Arabic/sound lookup failed on this call; "
                f"only exact spelling was checked)"]
        if weak_only:
            for m in extra:
                m["_single_token_query"] = True
                m["_weak_name_reason"] = weak_only
        out.extend(extra)
        partial.extend(notes)

    # A country hit ranks above a country miss, and an unknown sits between
    # them - but every one of them is still in the list.
    _rank = {True: 0, None: 1, False: 2}
    out.sort(key=lambda m: (_rank.get(m.get("country_match"), 1),
                            -m["match_score"]))

    # A SINGLE WORD CANNOT IDENTIFY A PARTY, so it is never a finding.
    #
    # "Dave" was being returned as a MATCH against a list entry literally
    # named Dave, with the programme "Isil (Da'esh) and Al-Qaeda" attached.
    # So were "Said", "Universal" and "East". The rows are real; one word is
    # simply not enough to say WHICH Dave.
    #
    # Telling apart "Dave" from "Rosneft" - both single tokens, one
    # meaningless and one decisive - requires name-frequency data. That is
    # exactly the thing we do not have and are not going to invent, and it is
    # why the calibrated source was worth something. Without it, the only
    # honest rule that does not depend on guessing is: one word gets
    # SURFACED, prominently, and never ASSERTED.
    #
    # The cost is real and accepted: screening "Rosneft" alone now returns
    # matched=false with Rosneft at the top of possible_matches_unverified,
    # rather than a finding. Under-claiming costs the caller one look. The
    # alternative cost is telling someone their counterparty is on an
    # Al-Qaeda list because he is called Dave.
    return out, queried, partial


async def _call_ofac_sdn(
    name: str,
) -> tuple[list[dict], list[str], list[str]]:
    """
    Screen the OFAC SDN list published by the US Treasury itself.
    Source: sanctionslistservice.ofac.treas.gov (SDN.CSV + ALT.CSV), a US
    Government work in the public domain. No key, no licence question.
    Returns (matches, sources_queried, sources_unavailable).
    """
    sources_queried = [_OFAC_SDN_CSV_URL]
    sources_unavailable: list[str] = []

    csv_text = await _fetch_ofac_sdn_csv()
    if csv_text is None:
        sources_unavailable.append(
            "OFAC-SDN (download from sanctionslistservice.ofac.treas.gov failed)"
        )
        return [], sources_queried, sources_unavailable

    # Aliases are a second file. If it does not arrive we still screen, on
    # primary names only - and SAY so, because a caller who believes aliases
    # were checked and finds out later that they were not has been misled
    # about the one thing they were buying.
    alt_text = await _fetch_ofac_alt_csv()
    if alt_text is None:
        sources_unavailable.append(
            "OFAC-SDN alternate spellings (ALT.CSV unavailable; primary names "
            "only -- aliases NOT screened on this call)"
        )
    else:
        sources_queried.append(_OFAC_ALT_CSV_URL)

    matches = _parse_ofac_sdn(csv_text, name, alt_text)

    # Sound-based and Arabic-script matching. OFAC publishes Latin script only,
    # so an Arabic query reaches it by transliteration.
    aq = _ar.analyse(name)
    if aq.phonetic_ok:
        try:
            matches = matches + await _ofac_phonetic_matches(csv_text, alt_text, aq)
        except _PhoneticIndexNotReady:
            sources_unavailable.append(
                "OFAC-SDN (the transliteration index for this copy of the list "
                "was still being built; only exact spelling was checked on this "
                "call - ask again in a minute)")
        except Exception:                       # noqa: BLE001
            sources_unavailable.append(
                "OFAC-SDN (the transliteration pass failed on this call; only "
                "exact spelling was checked)")
    return matches, sources_queried, sources_unavailable


# ---------------------------------------------------------------------------
# Evidence record (see core/compliance_receipt.py)
# ---------------------------------------------------------------------------

# WHAT THE RECEIPT REFUSES TO CLAIM, carried inside the record itself.
#
# Everything this file is arranged to prevent - a clean screen from a screen
# that never ran, a name collision presented as a finding, a stale copy read as
# current - becomes readable again the moment the answer is filed away as "we
# screened them and they were clean". The limits have to travel WITH the
# evidence, in the artefact the auditor opens, not in documentation nobody
# keeps next to it.
_DOES_NOT_ASSERT = [
    "It does NOT assert that the subject is not sanctioned. A 'clean' outcome "
    "means the lists named in evidence.lists_screened returned no confirmed "
    "match against the copies whose ages are recorded here. That is all it "
    "means.",
    "It does not assert coverage of any list absent from "
    "evidence.lists_screened. The UN Consolidated List is never screened, and "
    "PEP databases, adverse media, export-control party lists and internal "
    "watchlists are outside this tool entirely.",
    "It does not assert that the list copies matched the publishers' live "
    "lists at the moment of screening. It records how old each copy was so "
    "that judgement stays with you.",
    "It does not assert identity. A match is a NAME similarity produced by an "
    "uncalibrated matcher with no name-frequency data behind it - not a "
    "determination that your counterparty is the listed party - and a "
    "non-match does not rule out an alias, transliteration or spelling that "
    "does not appear in the copies screened.",
    "It is not legal advice, not a KYC/AML clearance, and not a sanctions "
    "determination by any authority.",
]

_ASSERTS = (
    "AgentBroker screened the name recorded here against the sanctions list "
    "copies described in evidence.data_provenance, at the instant recorded "
    "here, using the matching method recorded here, and returned the outcome "
    "recorded here. Every field is a statement about what this service did, "
    "not about the party screened."
)


async def _refreshed_on(list_code: str) -> Optional[str]:
    """The refresh date the screen ACTUALLY USED for this list, or None.

    Reads _age_cache directly first. The screen populated it microseconds ago,
    so this reports the value the decision was made on rather than a fresh
    lookup that could disagree with it - and it adds no database round trip to
    a free tool. Falls back to the normal (cached) accessor when the cache is
    empty, which is the case when a test has replaced _list_refreshed_at.
    """
    hit = _age_cache.get(list_code)
    if hit:
        return hit[1]
    try:
        return await _list_refreshed_at(list_code)
    except Exception:                           # noqa: BLE001
        return None


async def _data_provenance(ofac_ok: bool, eu_ok: bool, uk_ok: bool) -> list[dict]:
    """Which data answered this screen, from whom, under what licence, how old.

    THE MOST VALUABLE FIELD IN THE WHOLE RECEIPT. An auditor's first question
    about a sanctions screen from 2026 is not what it said - it is what it was
    screened against and whether that copy was current. The tool already
    enforces a 7-day limit and already knows every one of these numbers; until
    now it spent them on a prose sentence and threw the structure away.
    """
    entries: list[dict] = []

    ofac_age_s = list_cache_age_s(_OFAC_SDN_CSV_URL)
    ofac_stale = any(u in _stale_ages
                     for u in (_OFAC_SDN_CSV_URL, _OFAC_ALT_CSV_URL))
    entries.append({
        "list": "OFAC-SDN",
        "publisher": "US Department of the Treasury, Office of Foreign Assets "
                     "Control",
        "sources": [_OFAC_SDN_CSV_URL, _OFAC_ALT_CSV_URL],
        "licence": "US Government work, public domain",
        "screened_on_this_call": bool(ofac_ok),
        "copy_fetched_hours_ago": (int(ofac_age_s // 3600)
                                   if ofac_age_s is not None else None),
        "refresh_state": ("stale_copy_after_failed_refresh" if ofac_stale
                          else "refreshed_within_ttl" if ofac_age_s is not None
                          else "unknown"),
        "refresh_ttl_hours": int(_LIST_TTL_S // 3600),
        "max_copy_age_days": _STALE_AFTER_DAYS,
    })

    for code, label, publisher, source, licence, ok in (
        ("EU", "EU-CONSOLIDATED", "European Commission",
         _EU_CSV_URL,
         "European Commission open-data licence (commercial reuse permitted)",
         eu_ok),
        ("UK", "UK-SANCTIONS", "UK Foreign, Commonwealth & Development Office",
         _UK_CSV_URL, "Open Government Licence v3.0", uk_ok),
    ):
        day = await _refreshed_on(code)
        age = _days_since(day)
        entries.append({
            "list": label,
            "publisher": publisher,
            "source": source,
            "licence": licence,
            "screened_on_this_call": bool(ok),
            "index_refreshed_on": day,
            "index_age_days": age,
            "max_index_age_days": _STALE_AFTER_DAYS,
            # None, not False, when the age could not be read. "We do not know"
            # and "we know it is outside the limit" are different facts, and
            # the gate in _screen_list_db refuses to screen on either.
            "within_freshness_limit": (None if age is None
                                       else age <= _STALE_AFTER_DAYS),
        })

    # THE LIST WE DO NOT SCREEN, NAMED IN THE EVIDENCE.
    #
    # A reader deciding whether this receipt covers their obligation needs the
    # boundary as explicitly as the coverage. Leaving it out would let the
    # record be read as "the sanctions lists" rather than "these three".
    entries.append({
        "list": "UN-CONSOLIDATED (UN Security Council)",
        "publisher": "United Nations Security Council",
        "screened_on_this_call": False,
        "reason_not_screened": (
            "No open licence permitting commercial redistribution, so "
            "AgentBroker never screens this list. A permanent boundary of the "
            "service, not an outage on this call."),
    })
    return entries


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle_screen_sanctions(
    name: str,
    country: Optional[str] = None,
    entity_type: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> OutcomeReceipt:
    """
    Screen a name/entity against official sanctions & watchlists.

    Queries:
      1. OFAC SDN, the EU Consolidated list and the UK Sanctions List
      2. OFAC SDN directly (Treasury CSV, always free, no key)

    Returns an OutcomeReceipt with result dict containing:
      matched (bool), matches (list), sources_queried (list),
      screened_at (ISO timestamp), disclaimer (str).
    """
    t0 = time.monotonic()
    op_id = str(uuid.uuid4())
    screened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Input validation -------------------------------------------------
    name_clean = name.strip() if name else ""
    if not name_clean:
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message="name is required -- provide the person or entity name to screen.",
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    country_upper = country.strip().upper() if country else None

    # Validate entity_type
    if entity_type and entity_type not in ("person", "entity"):
        entity_type = None  # ignore unknown values; degrade gracefully

    # --- Run upstreams (OpenSanctions primary, OFAC CSV fallback) ----------
    import asyncio
    _phonetic_stats: dict = {}
    _PHONETIC_STATS.set(_phonetic_stats)

    # SCREENABILITY IS DECIDED ONCE, FOR EVERY SOURCE.
    #
    # My first version of this guard lived inside the database path, so EU and
    # UK correctly refused "Universal" while OFAC - which has its own matcher -
    # went on returning it as a MATCH on a Russia programme. A safety rule that
    # covers two of three sources is not a safety rule; it just moves which
    # list makes the false accusation.
    _q_toks = set(_normalize_name(name_clean).split())
    _aq = _ar.analyse(name_clean)
    _arabic_script = _aq.script in ("arabic", "mixed")
    if _arabic_script:
        # No Latin tokens to judge: the Arabic layer says whether the name
        # carries enough to identify anyone by sound.
        _unscreenable = _aq.weak_reason
        # Titles count here: in "Abd al-Manan Agha" the last word is a surname
        # that happens to be spelled like a title. Only generic words do not.
        _single_token = len([u for u in _aq.units if not u.generic]) == 1
    else:
        _unscreenable = _is_screenable(_q_toks)
        _single_token = len(_q_toks) == 1

    ofac_task = asyncio.create_task(
        _call_ofac_sdn(name_clean)
    )
    # EU and UK run alongside OFAC, from the authorities that publish them.
    eu_task = asyncio.create_task(
        _screen_list_db(name_clean, "EU",
                        "EU-CONSOLIDATED (European Commission financial sanctions)",
                        "https://www.sanctionsmap.eu/", country_upper)
    )
    uk_task = asyncio.create_task(
        _screen_list_db(name_clean, "UK",
                        "UK-SANCTIONS (FCDO UK Sanctions List)",
                        "https://sanctionslist.fcdo.gov.uk/", country_upper)
    )

    ofac_matches, ofac_queried, ofac_unavail = await ofac_task
    eu_matches, eu_queried, eu_unavail = await eu_task
    uk_matches, uk_queried, uk_unavail = await uk_task

    # --- Merge results ----------------------------------------------------
    all_sources_queried: list[str] = []
    all_sources_unavailable: list[str] = []

    all_sources_queried.extend(_ascii(s) for s in ofac_queried)
    all_sources_queried.extend(_ascii(s) for s in eu_queried)
    all_sources_queried.extend(_ascii(s) for s in uk_queried)
    all_sources_unavailable.extend(_ascii(s) for s in ofac_unavail)
    all_sources_unavailable.extend(_ascii(s) for s in eu_unavail)
    all_sources_unavailable.extend(_ascii(s) for s in uk_unavail)
    if _arabic_script and not _unscreenable:
        # NEVER "CLEAN": transliteration is lossy. See the section above.
        all_sources_unavailable.append(_ARABIC_SCRIPT_NOTE)

    # Merge and deduplicate by (normalized name, list) key
    merged: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    # TAG EACH MATCH WITH WHICH MATCHER FOUND IT. Without this the two are
    # indistinguishable downstream, and they must not be treated alike:
    # `os_matches` come from OpenSanctions' CALIBRATED scorer, which knows that
    # "Ali Mohammed" is a common name and "Zarubezhneft" is not. `ofac_matches`
    # come from our own word-overlap function, which has no frequency data at
    # all and scores "Star Trading LLC" against "Star Dragon Corporation" at
    # 1.00. One of those numbers is evidence; the other is a coincidence.
    if _single_token:
        # One word identifies nobody, from ANY list. Surfaced, never asserted.
        for m in ofac_matches + eu_matches + uk_matches:
            m["_single_token_query"] = True

    for m in ofac_matches:
        # setdefault: the sound-based matches added in _call_ofac_sdn carry their
        # own tag, and overwriting it would file them with the matcher that has
        # a different safety rule attached (see the weak-name block below).
        m.setdefault("_matcher", "local_word_overlap")
    # EU/UK already carry the tag from _screen_list.

    for m in ofac_matches + eu_matches + uk_matches:
        # An Arabic-script name normalises to "" in the Latin tokeniser, which
        # would collapse every Arabic alias on one list into a single key.
        key = (_normalize_name(m.get("name", ""))
               or _ar.normalise_arabic(m.get("name", ""))
               or m.get("name", ""), m.get("list", ""))
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(m)

    # Sort by score descending
    merged.sort(key=lambda m: m.get("match_score", 0.0), reverse=True)

    lat = int((time.monotonic() - t0) * 1000)

    # --- Determine lists actually screened --------------------------------
    screened_lists: list[str] = []
    if not ofac_unavail:
        screened_lists.append(
            "OFAC-SDN (US Treasury Specially Designated Nationals, "
            "published by sanctionslistservice.ofac.treas.gov"
            + _ofac_age_note() + ")"
        )
    # NAME EACH LIST THAT ACTUALLY RAN. A caller with a European obligation
    # needs to know whether the EU list was screened on THIS call, not whether
    # we support it in principle - so a list that failed to load is absent from
    # here and present in sources_unavailable.
    if not eu_unavail:
        screened_lists.append(
            "EU-CONSOLIDATED (European Commission consolidated financial "
            "sanctions, webgate.ec.europa.eu; "
            + (eu_queried[0].split("(")[-1].rstrip(")") if eu_queried else "local index")
            + ")"
        )
    if not uk_unavail:
        screened_lists.append(
            "UK-SANCTIONS (UK Sanctions List published by the FCDO, "
            "sanctionslist.fcdo.gov.uk; "
            + (uk_queried[0].split("(")[-1].rstrip(")") if uk_queried else "local index")
            + ")"
        )

    if _unscreenable:
        # A REDUCED SCREEN, NOT AN ERASED ONE.
        #
        # This block used to set `merged = []` and drop every list from
        # screened_lists, so a weak name produced a receipt with nothing in
        # it. Combined with the "No matches on the screened lists" sentence
        # below, screening GRU - listed on both the EU and UK lists - read as
        # clean. The refusal was disclosed; the headline contradicted it.
        #
        # EU and UK have now run an exact-whole-name lookup and tagged
        # whatever it found for demotion, so those results are kept and
        # surfaced as candidates. OFAC is different: it screens through
        # _word_match_score, which has no frequency data and is the matcher
        # that scored "Universal" as a Russia-programme hit. Nothing from it
        # survives here unless the listed name IS the query.
        _kept = []
        for m in merged:
            # TWO DIFFERENT DATA SHAPES WERE BEING COMPARED, so this only ever
            # kept a match by luck. The left side was an ORDERED list with
            # duplicates, in the order OFAC wrote the name; the right side a
            # SORTED set of the query's tokens. Any OFAC entry whose own token
            # order is not alphabetical could not be retained by any query in
            # any word order:
            #
            #   "Ri Je Son"  -> OFAC 'RI, Je-Son'  (NPWMD)  score 1.00
            #     ['ri','je','son'] != ['je','ri','son']    -> DROPPED
            #
            # Measured over the whole SDN file: 276 weak-name entries the
            # filter could never keep, 239 of them absent from the EU and UK
            # lists too - so invisible from every source we have. MS-13, the
            # Mahan Air tail numbers, and a run of North Korean WMD
            # designations were among them, while the receipt claimed exact
            # whole-name matches HAD been checked on OFAC.
            if m.get("_matcher") == "local_word_overlap" and \
                    m.get("list", "").startswith("OFAC") and \
                    sorted(set(_normalize_name(m.get("name", "")).split())) \
                    != sorted(set(_q_toks)):
                continue
            m["_single_token_query"] = True      # demote: candidate, never finding
            m.setdefault("_weak_name_reason", _unscreenable)
            _kept.append(m)
        merged = _kept
        # EXTEND, do not replace. EU and UK report their own reason from
        # inside _screen_list_db; overwriting the list threw those away and
        # left only OFAC's, so the receipt named one source when three had
        # failed to screen.
        all_sources_unavailable = all_sources_unavailable + [
            f"{lst.split(' (')[0]} (reduced screen: {_unscreenable} - only "
            f"exact whole-name matches were checked)"
            for lst in screened_lists]
        if not all_sources_unavailable:
            all_sources_unavailable = [f"(NOT screened: {_unscreenable})"]
        screened_lists = []

    # Fall back to honest "partial" if everything failed
    screened_ok = bool(screened_lists)
    if not screened_lists:
        screened_lists = ["(all sources unavailable -- see sources_unavailable field)"]

    # --- OUR MATCHER IS UNCALIBRATED, PERMANENTLY, AND SAYS SO -------------
    #
    # This used to be a fallback rule for when the calibrated source was dark.
    # There is no calibrated source any more (see the note at the top of the
    # file), so it is not a fallback: it is the method.
    #
    # WHAT THE OLD CODE GOT RIGHT AND WHY IT IS KEPT. The predicate that
    # decided "is the authoritative source up" was broken for a long time - the
    # test was case-sensitive for "OpenSanctions" against lowercase URLs, so it
    # was False on every call, the strict filter ran on every call, and the
    # tool behaved well for a reason nobody intended. The obvious one-line fix
    # would have switched the safety OFF. The fix was to tie the filter to
    # match PROVENANCE instead of to a flag, and that decoupling is the only
    # reason this removal is a deletion rather than a rewrite: the safety was
    # never actually resting on OpenSanctions being up.
    #
    # What the filter enforces, on every match, always: a finding requires the
    # SAME NORMALISED TOKEN SET. Not a subset, not a high overlap score.
    # Sanctions lists write names in every order - "Kim Jong Un" is listed as
    # "Jong Un Kim" - so order-insensitive set equality is right, and it is
    # also the ONLY relationship a word-set matcher can assert without
    # name-frequency data. Everything else is a candidate for the caller.
    #
    # Over-claiming tells someone that Maria Garcia is a narcotics trafficker.
    # Under-claiming costs them one lookup.
    #
    # `degraded` is gone as a runtime state. A permanent property of the method
    # is not an outage, and reporting one on every call trains callers to
    # ignore the field that matters. What replaces it is a method statement in
    # every response, matched or not.
    degraded = False

    unverified: list = []
    # THE FILTER NOW APPLIES TO OUR OWN MATCHER ALWAYS, not only when degraded.
    #
    # It used to run only under `degraded`, which was accidentally always true.
    # Tie it to the thing that actually justifies it instead: a match found by
    # OUR word-overlap function has no frequency calibration behind it and must
    # never be presented as a finding on a subset overlap - whether or not
    # OpenSanctions also answered on this call.
    #
    # Calibrated matches from the OpenSanctions API pass through on their own
    # score, because that score means something. This is the whole distinction
    # the `_matcher` tag was added to preserve.
    _needs_strict = merged   # every match we can make is uncalibrated now
    if _needs_strict:
        # I TRIED STRIPPING GENERIC CORPORATE WORDS HERE AND REVERTED IT.
        #
        # The motive was real: comparing raw token sets misses true positives
        # whose only difference is a legal form - "Rosneft Trading" against a
        # listed "ROSNEFT TRADING S.A.", "Gazprombank" against "GAZPROMBANK
        # JOINT STOCK COMPANY". Both ARE on the SDN list.
        #
        # But stripping collapses a multi-word name to its one distinctive
        # token, and then unrelated companies become identical:
        #
        #   "Atlas Trading Company" -> {atlas} == {atlas} <- "ATLAS HOLDING"
        #   "Horizon Group"         -> {horizon} == {horizon} <- "HORIZON"
        #
        # Measured, both were promoted to MATCHED. Telling a customer their
        # counterparty is sanctioned because both names contain "Atlas" is the
        # exact failure this filter exists to prevent, and it is worse than the
        # miss it was meant to fix.
        #
        # Deciding when one shared token is evidence and when it is a
        # coincidence requires name-frequency data, which we do not have. That
        # is precisely the thing OpenSanctions sells and the thing we should
        # not try to reproduce. So the conservative rule stays - and the
        # near-misses are not lost: they are returned as possible_matches_unverified
        # for the caller to judge, which is the honest answer from an
        # uncalibrated matcher.
        q_set = set(_normalize_name(name_clean).split())
        confident, possible = [], []
        for m in merged:
            # SAME TOKEN SET, order-insensitive. Sanctions lists write names in
            # every order - "Kim Jong Un" is listed as "Jong Un Kim" - so exact
            # string equality misses real entities, while a subset ("Rosneft"
            # inside "ROSNEFT TRADING S.A.") is exactly the shape that also
            # produces "Star Trading LLC" inside "Star Dragon Corporation".
            #
            # Identical token sets is the one relationship a word-set matcher
            # can assert without frequency data. It still surfaces common-name
            # collisions like "Mohammed Ali" against a listed "Ali Mohammed" -
            # and it SHOULD: that is a genuine collision a screener must show.
            # What it no longer does is dress a subset overlap as a finding.
            if m.get("_single_token_query"):
                # One word identifies nobody. Surfaced, never asserted.
                possible.append(m)
            elif m.get("_matcher") == "arabic_script_exact":
                # The query IS an Arabic-script alias the publisher printed, element
                # for element: the same evidence as a Latin token-set equality.
                confident.append(m)
            elif q_set and set(_normalize_name(m.get("name", "")).split()) == q_set:
                confident.append(m)
            else:
                possible.append(m)
        merged, unverified = confident, possible

    # --- Build result payload ---------------------------------------------
    matched = len(merged) > 0
    result_payload: dict = {
        "matched": matched,
        # WHAT `matched: false` MEANT WAS AMBIGUOUS, AND THE TWO MEANINGS ARE
        # OPPOSITES. It was false both for "we screened three lists and this
        # party is on none of them" and for "we screened nothing". A program
        # branching on it read the second as the first - a clean bill of
        # health from a screen that never ran. This field says which:
        #
        #   hit          - a confirmed match, matched=true
        #   clean        - EVERY list screened, nothing found
        #   partial      - some lists screened and clean, others did not run
        #   candidates   - screened, nothing confirmed, unverified candidates
        #   not_screened - NO list produced a complete screen. Not a result.
        #
        # `partial` exists because `clean` had the same defect `matched: false`
        # had, one level up: it was returned both for "all three lists screened
        # and this party is on none of them" and for "OFAC screened, the EU and
        # UK indexes were unreachable". The docs tell buyers to branch on this
        # field, so a US-only screen must not present as a European clearance.
        "screening_status": (
            "hit" if matched
            else "not_screened" if not screened_ok
            else "candidates" if unverified
            else "clean" if not all_sources_unavailable
            else "partial"),
        "matches": merged,
        "lists_screened": screened_lists,
        "sources_queried": all_sources_queried,
        "screened_at": screened_at,
        "disclaimer": _DISCLAIMER,
    }
    if country_upper:
        result_payload["country_filter_applied"] = False
        result_payload["country_note"] = (
            f"country={country_upper} was used to ANNOTATE and RANK results, "
            f"never to remove any. Each EU/UK match carries country_match: "
            f"true, false, or null when the listing records no country at all. "
            f"Nothing is dropped for a country mismatch, because our country "
            f"data is the address/nationality on the listing rather than an "
            f"exhaustive record of where a party operates - excluding on it "
            f"would turn every gap into a clean screen for someone who IS "
            f"listed. Use country_match to prioritise your own review.")
    if entity_type:
        result_payload["entity_type_filter_applied"] = False
        result_payload["entity_type_note"] = (
            "entity_type is accepted but does not narrow the screen. Each "
            "match reports its own entity_type, taken from the publishing "
            "authority's own type column on all three lists; filter on that "
            "if you need to.")
    if all_sources_unavailable:
        result_payload["sources_unavailable"] = all_sources_unavailable
    if _aq.engaged:
        result_payload["arabic_matching"] = {
            "applied": bool(_aq.phonetic_ok),
            "query_script": _aq.script,
            "query_elements": [u.text.replace("|", "") for u in _aq.required],
            "not_applied_reason": _aq.weak_reason,
            "method": _ARABIC_METHOD,
            "sound_based_candidates": sum(
                1 for m in unverified + merged
                if str(m.get("_matcher", "")) == "name_sound_match"),
            "low_confidence_not_listed": _phonetic_stats.get(
                "low_confidence_not_listed", 0),
        }
    if unverified:
        # Surfaced, never hidden. The caller may well want to look at these -
        # they simply must not be handed over as findings.
        result_payload["possible_matches_unverified"] = unverified
        result_payload["matching_method"] = (
            "Name matching is exact normalised-token-set equality, and it is "
            "uncalibrated: we have no name-frequency data, so we do not guess "
            "whether a partial overlap is meaningful. Entries that share some "
            "but not all of your query's words are listed here as candidates. "
            "They are NOT sanctions findings and do not set matched=true. "
            "Check each against the official source before acting on it.")

    # --- Human message ---------------------------------------------------
    _n_sound = sum(1 for m in unverified if m.get("_matcher") == "name_sound_match")
    _sound_sentence = (
        f" {_n_sound} of them are sound-based transliteration or spelling-variant "
        f"candidates: see match_confidence, match_explanation and token_alignment "
        f"on each." if _n_sound else "")
    if matched:
        top = merged[0]
        # top['name'] and top['program'] are the PUBLISHER'S strings - the
        # listed name out of Treasury's SDN.CSV or the EC/FCDO rows. They are
        # data about a match, not part of our sentence, and a model reading a
        # compliance verdict is exactly the reader you do not want inheriting a
        # stranger's phrasing unmarked.
        human_message = (
            f"MATCH FOUND for '{_ascii(name_clean)}': "
            f"{_fence_untrusted(top['name'])} on {top['list']} "
            f"(score={top['match_score']:.2f}"
            + (f", program={_fence_untrusted(top['program'])}" if top.get("program") else "")
            + f"). Screened {len(screened_lists)} source(s). "
            "Verify against the official source before acting."
            # A HIT THAT NAMES ONE LIST UNDERSTATES THE EXPOSURE.
            #
            # Found by an independent external review (2026-09-02) and it is the
            # more dangerous direction of error. Screening "Vladimir Putin"
            # returns matched=true on the UK list and says nothing here about
            # four candidates — including OFAC's "PUTIN, Vladimir Vladimirovich"
            # at score 1.0. Our exact-token-set rule demoted the OFAC and EU
            # listings to "candidate" purely because their stored form carries
            # the patronymic, so WHICH list happens to store the bare two-token
            # form decides what the sentence reports. A reader skimming this
            # line would conclude "UK only" about a party listed on three.
            #
            # The structured payload separates confirmed from candidate clearly,
            # but the sentence a human actually reads did not — and the
            # exact-spelling caveat added earlier appeared only on a CLEAN
            # result, which is exactly backwards: the near-miss warning matters
            # MOST on the result where near-misses were demoted.
            + ("" if not unverified else
               f" IMPORTANT: {len(unverified)} further name-similarity "
               f"candidate(s) were found on the screened lists and are NOT "
               f"included above — see possible_matches_unverified. Our matcher "
               f"asserts a match only on exact name-token equality, so the SAME "
               f"party listed under a fuller or different spelling (a "
               f"patronymic, a middle name, a transliteration) appears there "
               f"rather than here. Treat this result as a FLOOR on exposure, "
               f"not a complete picture." + _sound_sentence)
        )
        reason_code = "matched"
    elif not screened_ok:
        # NOTHING WAS ACTUALLY SCREENED, so there is no "no matches" to report.
        #
        # This branch used to fall through to the sentence below, which opens
        # "No matches on the screened lists for 'X'" and only mentions the
        # outage in a trailing NOTE. The first sentence is what a human skims
        # and what an LLM summarises, and it said clean. Same defect as
        # verify_company_record's not_found: the shape of the answer has to
        # change when the basis for it disappears, not just a caveat appended.
        human_message = (
            f"COULD NOT FULLY SCREEN '{_ascii(name_clean)}': no sanctions "
            f"list returned a complete screen on this call ("
            + "; ".join(_ascii(u) for u in all_sources_unavailable)
            + "). This is NOT a clean result. "
              "Retry, or check OFAC, the EU consolidated list and the UK "
              "sanctions list directly."
            + ("" if not unverified else
               f" {len(unverified)} unverified candidate(s) are listed in "
               f"possible_matches_unverified.")
        )
        reason_code = "not_screened"
    else:
        no_match_detail = (
            # "No matches" IS THE WRONG OPENING WHEN THERE ARE CANDIDATES.
            # Screening "Rosneft" put three Rosneft entities in
            # possible_matches_unverified and still opened with "No matches on
            # the screened lists for 'Rosneft'", relegating them to a trailing
            # NOTE. The first clause is what gets skimmed and summarised, so
            # it has to carry the finding-shaped part of the answer.
            (f"No CONFIRMED match for '{_ascii(name_clean)}', but "
             f"{len(unverified)} name-similarity candidate(s) were found - see "
             f"possible_matches_unverified"
             if unverified else
             f"No matches on the screened lists for '{_ascii(name_clean)}'")
            # NOT "country filter: IR". `country` annotates and ranks; it
            # never removes a match, so saying "filter applied" would tell a
            # caller their search was narrowed when it was not - and on a
            # screening tool that means they read a clean result as more
            # specific than it is.
            #
            # This comment previously said the index "does not carry a country
            # column yet". The commit that added the column did not update it,
            # which is verbatim the defect that commit was written to remove:
            # a sentence left behind by code that changed underneath it.
            + (f" (note: country={country_upper} was NOT used to narrow this "
               f"screen - see country_filter_applied)" if country_upper else "")
            + ". Screened: "
            + "; ".join(screened_lists)
            # Say it in the sentence a caller actually reads, not only in a
            # field they may not parse. "No match" from a degraded screen means
            # something weaker than "no match" from a complete one, and an
            # agent deciding whether to trade needs to know which it got.
            + ("" if not unverified else
               f". NOTE: {len(unverified)} name-similarity candidate(s) share "
               f"some of these words and are listed in "
               f"possible_matches_unverified. They are not findings - our "
               f"matcher is uncalibrated and only asserts a match on an exact "
               f"token-set equality - but a human should look at them."
               + _sound_sentence)
            # THE UNQUALIFIED CLEAN IS THE DANGEROUS ONE.
            #
            # An external black-box review (2026-09-01, run from a different
            # machine) landed on this exact sentence as the single most likely
            # source of real-world harm in the service. Its evidence: screening
            # the everyday spelling "Vladimir Putin" confirms only on the UK
            # list, because OFAC's canonical "PUTIN, Vladimir Vladimirovich"
            # carries a patronymic our exact-token-set rule treats as a
            # different name. Any variant spelling, transliteration, or
            # missing/extra middle name can therefore produce ZERO candidates
            # and land here - where, until now, the caller read a bare "No
            # matches on the screened lists" with nothing qualifying it.
            #
            # The limitation was disclosed in `matching_method` and in the
            # receipt's `does_not_assert`, so this was never dishonest. But a
            # caveat only in a field a hurried caller does not parse is not the
            # same as telling them: the branch above already learned that
            # lesson for the candidates case, and this is the same lesson for
            # the zero-candidate case. A clean screen from a crude matcher must
            # not read like a clean screen from an exhaustive one.
            #
            # Deliberately NOT done here: promoting exact-token subset hits to
            # confirmed matches. That would trade these false negatives for
            # false positives, and telling a caller an innocent party IS
            # sanctioned is its own serious harm. Changing match semantics on a
            # sanctions tool is a founder decision, not a patch.
            + ("" if unverified else
               (". NOTE: this is an Arabic-aware screen - names are also matched "
                "by sound across Arabic and Latin spellings - but romanisation is "
                "not standardised, so a spelling it does not generate, or a "
                "missing or extra middle name, can still read as clean. Treat "
                "this as 'nothing matched by sound or spelling', not as 'this "
                "party is not sanctioned'."
                if _aq.engaged else
                ". NOTE: this screen matches on exact name-token equality only, "
                "so a variant spelling, transliteration, or a missing or extra "
                "middle name can read as clean. Treat this as 'nothing matched "
                "the name as written', not as 'this party is not sanctioned'."))
        )
        if all_sources_unavailable:
            no_match_detail += ". NOTE: some sources were unavailable -- screening may be incomplete."
        human_message = no_match_detail
        reason_code = "no_match" if not all_sources_unavailable else "partial_screening"

    # --- evidence record ---------------------------------------------------
    #
    # `screened_ok` is ANDed in deliberately. A reduced screen - the weak-name
    # path above, where only exact whole-name matches were checked - is not a
    # screen, and the receipt must not record one list as covered when the very
    # branch that set screened_ok=False exists to say it was not.
    _ofac_ok = screened_ok and not ofac_unavail
    _eu_ok = screened_ok and not eu_unavail
    _uk_ok = screened_ok and not uk_unavail
    attach_receipt(
        result_payload,
        tool="screen_sanctions",
        operation_id=op_id,
        service_version=service_version(),
        asserts=_ASSERTS,
        does_not_assert=_DOES_NOT_ASSERT,
        subject={
            "name_screened": _ascii(name_clean),
            "country_hint": country_upper,
            "entity_type_hint": entity_type,
        },
        inputs={"name": name_clean, "country": country, "type": entity_type},
        evidence={
            "screened_at": screened_at,
            "data_provenance": await _data_provenance(_ofac_ok, _eu_ok, _uk_ok),
            "lists_screened": screened_lists,
            "sources_queried": all_sources_queried,
            "sources_unavailable": all_sources_unavailable,
            "matching_method": (
                "Exact normalised token-set equality, order-insensitive, and "
                "UNCALIBRATED: no name-frequency data, so a partial overlap is "
                "never asserted as a finding. Entries sharing some but not all "
                "of the query's words are returned as unverified candidates."
                + (" " + _ARABIC_METHOD if _aq.engaged else "")),
            "outcome": {
                "screening_status": result_payload["screening_status"],
                "reason_code": reason_code,
                "confirmed_matches": len(merged),
                "unverified_candidates": len(unverified),
                # The names of confirmed matches, so the record says WHAT was
                # found and not merely how many. Candidates are counted, not
                # named: they are not findings, and copying them into an
                # evidence file is how a coincidence becomes an allegation.
                "confirmed_match_names": [m.get("name") for m in merged],
            },
        },
    )

    return OutcomeReceipt(
        operation_id=op_id,
        status=OperationStatus.SUCCESS,
        reason_code=reason_code,
        human_message=human_message,
        result=result_payload,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=lat,
        retriable=False,
        trace_id=trace_id,
    )
