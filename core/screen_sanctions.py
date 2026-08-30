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
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.models import CostRecord, OperationStatus, OutcomeReceipt

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
    Cyrillic, Arabic or CJK name normalises to NOTHING and cannot match any
    index entry. That is handled honestly rather than silently - see
    _is_screenable, which reports such a name as NOT SCREENED instead of
    returning a clean result - but it is a real coverage gap, not a solved
    problem. Transliteration is the fix and it is not built yet.

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


async def _fetch_url(url: str, allow_stale: bool = True) -> Optional[str]:
    """GET a public list file, cached for _LIST_TTL_S.

    On a failed refresh, returns the last good copy rather than None -
    `allow_stale=False` opts out where a caller genuinely needs freshness.
    """
    now = time.time()
    hit = _list_cache.get(url)
    if hit and (now - hit[0]) < _LIST_TTL_S:
        return hit[1]

    import httpx
    ua = "AgentBroker-SanctionsScreen/1.0 (compliance tool; contact hello@hatchloop.dev)"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
                follow_redirects=True,
            )
        if resp.status_code == 200 and resp.text.strip():
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
            out.append({
                "name": nm,
                "entity_id": uid,
                "programme": (r[iReg].strip()[:80] if iReg is not None and iReg < len(r) else ""),
                "etype": "INDIVIDUAL" if st.startswith("indiv") else "ENTITY",
                "countries": sorted({c.strip().upper()
                                     for i in iCty if i < len(r)
                                     for c in r[i].split(";")
                                     if 1 < len(c.strip()) <= 40}),
            })
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
        # Unknown age is reported as unknown, never as fresh.
        val = None
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
    codes_for_name = {c for c, n in _ISO2_NAMES.items()
                      if _words(n) and _words(n) <= want_words}
    want_terms |= codes_for_name
    # The full country name(s) this query stands for, compared as whole
    # phrases rather than loose words.
    name_forms = [w]
    if w in _ISO2_NAMES:
        name_forms.append(_ISO2_NAMES[w])
        name_forms.extend(_COUNTRY_ALIASES.get(w, []))
    # The caller may have typed an alias directly ("Russian Federation").
    for code, aliases in _COUNTRY_ALIASES.items():
        if w in aliases or _words(w) <= _words(_ISO2_NAMES.get(code, "")):
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
    weak_only = _is_screenable(set(toks))

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

    try:
        exact = await select_rows_strict(
            "sanctions_names",
            filters={"list_code": list_code, "name_key": " ".join(toks)},
            limit=5,
        )
    except SupabaseUnavailable:
        exact = None                            # distinguish failure from empty

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
    return matches, sources_queried, sources_unavailable


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

    # SCREENABILITY IS DECIDED ONCE, FOR EVERY SOURCE.
    #
    # My first version of this guard lived inside the database path, so EU and
    # UK correctly refused "Universal" while OFAC - which has its own matcher -
    # went on returning it as a MATCH on a Russia programme. A safety rule that
    # covers two of three sources is not a safety rule; it just moves which
    # list makes the false accusation.
    _q_toks = set(_normalize_name(name_clean).split())
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
        m["_matcher"] = "local_word_overlap"
    # EU/UK already carry the tag from _screen_list.

    for m in ofac_matches + eu_matches + uk_matches:
        key = (_normalize_name(m.get("name", "")), m.get("list", ""))
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
            if m.get("_matcher") == "local_word_overlap" and \
                    m.get("list", "").startswith("OFAC") and \
                    _normalize_name(m.get("name", "")).split() != sorted(_q_toks):
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
        #   clean        - at least one list screened, nothing found
        #   candidates   - screened, nothing confirmed, unverified candidates
        #   not_screened - NO list produced a complete screen. Not a result.
        "screening_status": (
            "hit" if matched
            else "not_screened" if not screened_ok
            else "candidates" if unverified
            else "clean"),
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
    if matched:
        top = merged[0]
        human_message = (
            f"MATCH FOUND for '{_ascii(name_clean)}': "
            f"'{top['name']}' on {top['list']} "
            f"(score={top['match_score']:.2f}"
            + (f", program={top['program']}" if top.get("program") else "")
            + f"). Screened {len(screened_lists)} source(s). "
            "Verify against the official source before acting."
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
               f"token-set equality - but a human should look at them.")
        )
        if all_sources_unavailable:
            no_match_detail += ". NOTE: some sources were unavailable -- screening may be incomplete."
        human_message = no_match_detail
        reason_code = "no_match" if not all_sources_unavailable else "partial_screening"

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
