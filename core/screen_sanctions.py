"""
screen_sanctions -- free, read-only sanctions & watchlist screening.

Data sources (live calls, no stored dataset; cite sources in output):
  1. OpenSanctions (api.opensanctions.org) -- primary aggregator; covers OFAC SDN,
     EU Consolidated Financial Sanctions, UN Security Council, UK HMT, and 40+
     official lists worldwide. FREE API key required (non-commercial free tier
     available at https://www.opensanctions.org/accounts/register/).
     FOUNDER GATE: register for a free key, set OPENSANCTIONS_API_KEY in Render
     env vars. Without the key, an unauthenticated attempt is made (may be rate-
     limited) and the OFAC SDN fallback runs.
  2. OFAC SDN (ofac.treasury.gov/downloads/sdn.csv) -- official US Treasury
     Specially Designated Nationals list, fetched live, NO KEY NEEDED. Covers
     the primary OFAC SDN list only (not consolidated or program-specific lists).
     Used as always-on supplement and fallback for when OpenSanctions is unavailable.

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

_OPENSANCTIONS_BASE = "https://api.opensanctions.org"
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
_MATCH_THRESHOLD_OPENSANCTIONS = 0.70
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

    The MATCHING was fine - OpenSanctions handles those scripts upstream - but
    the receipt is the audit artefact, and an audit record that cannot say what
    was screened is not an audit record. "Wire-safe" was never a real
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


def _dataset_id_to_list_name(dataset_id: str) -> str:
    """Map an OpenSanctions dataset ID to a human-readable list name."""
    return _DATASET_NAMES.get(dataset_id, _ascii(dataset_id).upper())


# ---------------------------------------------------------------------------
# OpenSanctions API  (primary)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OpenSanctions circuit breaker
# ---------------------------------------------------------------------------
#
# Their API is optional breadth on top of the three lists we fetch ourselves.
# When it is failing - exhausted quota, no key, an outage - calling it anyway
# costs ~900ms on EVERY screen and returns nothing. This stops that without
# removing the capability.
_OS_FAIL_THRESHOLD = 3
_OS_COOLOFF_S = 900          # 15 min; a quota reset or a new key recovers fast
_os_fails = {"count": 0, "until": 0.0}


def _os_circuit_open() -> bool:
    return _os_fails["count"] >= _OS_FAIL_THRESHOLD and time.time() < _os_fails["until"]


def _os_record(ok: bool) -> None:
    if ok:
        _os_fails["count"] = 0
        _os_fails["until"] = 0.0
    else:
        _os_fails["count"] += 1
        if _os_fails["count"] >= _OS_FAIL_THRESHOLD:
            _os_fails["until"] = time.time() + _OS_COOLOFF_S


async def _call_opensanctions(
    name: str,
    country: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> tuple[list[dict], list[str], list[str]]:
    """
    Query OpenSanctions /match/sanctions endpoint.

    Returns (matches, sources_queried, sources_unavailable).
    `matches` is a list of dicts following our standard match record format.
    """
    import httpx

    api_key = os.getenv("OPENSANCTIONS_API_KEY", "").strip()
    url = f"{_OPENSANCTIONS_BASE}/match/sanctions?threshold={_MATCH_THRESHOLD_OPENSANCTIONS}"
    sources_queried = [url]
    sources_unavailable: list[str] = []

    # DO NOT SPEND A SECOND ON A SOURCE THAT IS NOT ANSWERING.
    #
    # Measured 2026-08-30: this call took 923ms of a 1.6s screen and returned
    # ZERO matches, because the account's monthly quota was exhausted. Nearly
    # two thirds of our latency, for nothing, on every single screen.
    #
    # After a run of consecutive failures we stop calling until the cool-off
    # passes. The capability is not removed - the moment the quota resets or a
    # working key is set, the next probe succeeds and normal service resumes -
    # but a dead upstream stops taxing every caller in the meantime.
    #
    # We still DECLARE it unavailable, because silently dropping a source a
    # caller believes is being screened is exactly the failure this file exists
    # to prevent.
    if not api_key:
        sources_unavailable.append(
            "OpenSanctions (no API key configured; EU/UN/UK breadth beyond our "
            "own OFAC/EU/UK lists NOT screened)")
        return [], sources_queried, sources_unavailable

    if _os_circuit_open():
        sources_unavailable.append(
            "OpenSanctions (skipped: repeated failures, retrying later -- not "
            "screened on this call)")
        return [], sources_queried, sources_unavailable

    # Build entity schema -- OpenSanctions uses FtM (Follow the Money) schemas
    schema = "Thing"  # top-level; matches both Person and Organization
    if entity_type == "person":
        schema = "Person"
    elif entity_type == "entity":
        schema = "Organization"

    properties: dict = {"name": [name]}
    if country:
        properties["country"] = [country.upper()]

    payload = {
        "queries": {
            "q1": {
                "schema": schema,
                "properties": properties,
            }
        }
    }

    headers: dict = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)

        if resp.status_code == 401:
            _os_record(False)
            sources_unavailable.append(
                "OpenSanctions (OPENSANCTIONS_API_KEY not set or invalid -- "
                "free key available at https://www.opensanctions.org/accounts/register/)"
            )
            return [], sources_queried, sources_unavailable

        if resp.status_code == 429:
            _os_record(False)
            sources_unavailable.append("OpenSanctions (rate-limited or quota exhausted -- EU/UN/UK lists NOT screened on this call)")
            return [], sources_queried, sources_unavailable

        if resp.status_code != 200:
            _os_record(False)
            sources_unavailable.append(
                f"OpenSanctions (HTTP {resp.status_code})"
            )
            return [], sources_queried, sources_unavailable

        data = resp.json()
        responses = data.get("responses", {})
        q_result = responses.get("q1", {})
        raw_results = q_result.get("results", [])

        matches: list[dict] = []
        for item in raw_results:
            score = float(item.get("score", 0.0))
            if score < _MATCH_THRESHOLD_OPENSANCTIONS:
                continue

            item_id = _clean(item.get("id", ""))
            caption = _clean(item.get("caption", ""))
            schema_name = _clean(item.get("schema", "Thing"))
            datasets = item.get("datasets", [])

            props = item.get("properties", {})
            programs = props.get("sanctionProgram", props.get("program", []))
            topics = props.get("topics", [])

            # Derive entity_type label
            if schema_name in ("Person",):
                etype = "INDIVIDUAL"
            elif schema_name in ("Vessel",):
                etype = "VESSEL"
            elif schema_name in ("Aircraft",):
                etype = "AIRCRAFT"
            else:
                etype = "ENTITY"

            # Derive list names from datasets
            list_names = [_dataset_id_to_list_name(ds) for ds in datasets]
            list_str = ", ".join(list_names) if list_names else "OpenSanctions"

            # Derive program string
            prog_str = None
            if programs:
                prog_str = _ascii(", ".join(str(p) for p in programs[:3]))
            elif "sanction" in topics:
                prog_str = "SANCTIONED"

            source_url = (
                f"https://www.opensanctions.org/entities/{item_id}/"
                if item_id
                else "https://www.opensanctions.org/"
            )

            matches.append({
                "name": caption or _ascii(name),
                "list": list_str,
                "match_score": round(score, 3),
                "program": prog_str,
                "entity_type": etype,
                "source_url": source_url,
            })

        _os_record(True)
        return matches, sources_queried, sources_unavailable

    except Exception as exc:
        _os_record(False)
        sources_unavailable.append(f"OpenSanctions (error: {_ascii(str(exc)[:80])})")
        return [], sources_queried, sources_unavailable


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
            return resp.text
    except Exception:
        pass

    # Refresh failed. A list from a few hours ago is still a real screen.
    if hit and allow_stale:
        return hit[1]
    return None


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

# ---------------------------------------------------------------------------
# EU/UK ARE OFF BY DEFAULT, AND THAT IS NOT TIMIDITY - IT IS MEASURED.
# ---------------------------------------------------------------------------
#
# Holding both lists in memory costs ~172MB of parsed index plus ~72MB of
# cached source text. On this instance that is an out-of-memory kill: enabling
# them put the origin into a restart loop and served 502 to everything for
# several minutes. Render reported the deploy as "live" throughout, because the
# container started fine and was then killed.
#
# I tried two compaction schemes before accepting the real answer: a
# frozenset-keyed index (WORSE - 112MB) and a sorted-token-string index (~100MB).
# Python object overhead dominates, and no amount of tuning makes 46,000 records
# cheap enough to live in every worker process.
#
# THE RIGHT PLACE FOR THIS DATA IS A DATABASE, not each worker's RAM. 46k rows
# is nothing for Postgres, it survives restarts, it does not multiply by worker
# count, and a screen becomes an indexed lookup. That is the build; this flag
# keeps the code present and inert until it lands.
#
# Set SANCTIONS_EU_UK_INMEMORY=true only on an instance with headroom to spare.
_EU_UK_ENABLED = os.getenv("SANCTIONS_EU_UK_INMEMORY", "").lower() in ("1", "true", "yes")

# Parsed indexes are cached alongside the raw text. Downloading 25MB and 50MB
# per screen would be absurd; so would re-parsing them. Both happen once per
# _LIST_TTL_S.
_index_cache: dict[str, tuple[float, list]] = {}


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
        seen: set[tuple[str, str]] = set()
        for r in rows:
            if len(r) <= iW:
                continue
            nm = r[iW].strip()
            if not nm:
                continue
            eid = r[iId] if iId < len(r) else ""
            if (eid, nm) in seen:
                continue
            seen.add((eid, nm))
            st = (r[iType] if iType < len(r) else "").lower()
            out.append({
                "name": nm,
                "entity_id": eid,
                "programme": (r[iProg].strip()[:80] if iProg and iProg < len(r) else ""),
                "etype": "INDIVIDUAL" if st.startswith("person") else "ENTITY",
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
        iType = hdr.index("Individual, Entity, Ship") if             "Individual, Entity, Ship" in hdr else None
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
            out.append({
                "name": nm,
                "entity_id": uid,
                "programme": (r[iReg].strip()[:80] if iReg is not None and iReg < len(r) else ""),
                "etype": "INDIVIDUAL" if st.startswith("indiv") else "ENTITY",
            })
    except Exception:
        pass
    return out


_warming: set[str] = set()


async def _build_index(url: str, parser) -> list[dict]:
    """Download and parse one list into the cache. Slow by nature."""
    raw = await _fetch_url(url)
    if not raw:
        return []
    idx = parser(raw)
    if idx:
        _index_cache[url] = (time.time(), idx)
    return idx


async def _get_index(url: str, parser, block: bool = False) -> list[dict]:
    """The cached index, WITHOUT making a caller wait for a cold download.

    THE EU AND UK LISTS ARE 25MB AND 50MB. Fetching and parsing both inline
    took 48 SECONDS on a cold cache - and the person who pays that is whichever
    caller happens to arrive first after a restart. One agent waiting 48s for a
    sanctions screen is a worse product than one that says "the EU list was not
    screened on this call" and returns in a second.
    
    So a cold cache starts a background warm and reports the list as
    unavailable for THIS call. The next caller gets it. `warm_lists()` runs at
    startup so in practice nobody sees the gap at all.

    `block=True` exists for warm_lists() and for tests that need determinism.
    """
    now = time.time()
    hit = _index_cache.get(url)
    if hit and (now - hit[0]) < _LIST_TTL_S:
        return hit[1]

    if block:
        return await _build_index(url, parser) or (hit[1] if hit else [])

    # Stale but present: serve it and refresh behind the caller's back.
    if url not in _warming:
        _warming.add(url)

        async def _warm():
            try:
                await _build_index(url, parser)
            finally:
                _warming.discard(url)

        try:
            asyncio.get_running_loop().create_task(_warm())
        except RuntimeError:
            _warming.discard(url)

    return hit[1] if hit else []


async def warm_lists() -> dict[str, int]:
    """Preload every list. Call once at startup so no caller pays the cold cost."""
    out: dict[str, int] = {}
    for label, url, parser in (("eu", _EU_CSV_URL, _eu_parse),
                               ("uk", _UK_CSV_URL, _uk_parse)):
        try:
            out[label] = len(await _get_index(url, parser, block=True))
        except Exception:                       # noqa: BLE001
            out[label] = 0
    try:
        await _fetch_url(_OFAC_SDN_CSV_URL)
        await _fetch_url(_OFAC_ALT_CSV_URL)
        out["ofac"] = 1
    except Exception:                           # noqa: BLE001
        out["ofac"] = 0
    return out


async def _screen_list(name: str, url: str, parser, list_label: str,
                       source_url: str) -> tuple[list[dict], list[str], list[str]]:
    """Screen one list. Returns (matches, sources_queried, sources_unavailable)."""
    if not _EU_UK_ENABLED:
        # Say it plainly. A caller with a European obligation must not read
        # silence as coverage - this list is NOT being screened.
        return [], [], [f"{list_label} (not enabled on this deployment; "
                        f"NOT screened)"]
    idx = await _get_index(url, parser)
    if not idx:
        # Loading in the background. Say plainly that this call did NOT screen
        # it - a caller with a European obligation must not read silence as
        # coverage.
        return [], [], [f"{list_label} (loading; NOT screened on this call)"]

    matches: list[dict] = []
    seen_ids: set[str] = set()
    for rec in idx:
        sc = _word_match_score(name, rec["name"])
        if sc < _MATCH_THRESHOLD_OFAC:
            continue
        if rec["entity_id"] in seen_ids:
            continue
        seen_ids.add(rec["entity_id"])
        matches.append({
            "name": _ascii(rec["name"]),
            "list": list_label,
            "match_score": round(sc, 3),
            "program": _ascii(rec["programme"]) or None,
            "entity_type": rec["etype"],
            "source_url": source_url,
            # UNCALIBRATED, exactly like our OFAC matcher - so the strict
            # token-set filter applies and these can surface as candidates but
            # never be asserted as findings on a subset overlap.
            "_matcher": "local_word_overlap",
        })
    matches.sort(key=lambda m: m["match_score"], reverse=True)
    return matches[:5], [url], []


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
      1. OpenSanctions (40+ official lists: OFAC, EU, UN, UK, and more)
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

    os_task = asyncio.create_task(
        _call_opensanctions(name_clean, country_upper, entity_type)
    )
    ofac_task = asyncio.create_task(
        _call_ofac_sdn(name_clean)
    )
    # EU and UK run alongside OFAC, from the authorities that publish them.
    eu_task = asyncio.create_task(
        _screen_list(name_clean, _EU_CSV_URL, _eu_parse,
                     "EU-CONSOLIDATED (European Commission financial sanctions)",
                     "https://www.sanctionsmap.eu/")
    )
    uk_task = asyncio.create_task(
        _screen_list(name_clean, _UK_CSV_URL, _uk_parse,
                     "UK-SANCTIONS (FCDO UK Sanctions List)",
                     "https://sanctionslist.fcdo.gov.uk/")
    )

    os_matches, os_queried, os_unavail = await os_task
    ofac_matches, ofac_queried, ofac_unavail = await ofac_task
    eu_matches, eu_queried, eu_unavail = await eu_task
    uk_matches, uk_queried, uk_unavail = await uk_task

    # --- Merge results ----------------------------------------------------
    all_sources_queried: list[str] = []
    all_sources_unavailable: list[str] = []

    all_sources_queried.extend(_ascii(s) for s in os_queried)
    all_sources_queried.extend(_ascii(s) for s in ofac_queried)
    all_sources_queried.extend(_ascii(s) for s in eu_queried)
    all_sources_queried.extend(_ascii(s) for s in uk_queried)
    all_sources_unavailable.extend(_ascii(s) for s in os_unavail)
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
    for m in os_matches:
        m["_matcher"] = "opensanctions_calibrated"
    for m in ofac_matches:
        m["_matcher"] = "local_word_overlap"
    # EU/UK already carry the tag from _screen_list.

    for m in os_matches + ofac_matches + eu_matches + uk_matches:
        key = (_normalize_name(m.get("name", "")), m.get("list", ""))
        if key not in seen_keys:
            seen_keys.add(key)
            merged.append(m)

    # Sort by score descending
    merged.sort(key=lambda m: m.get("match_score", 0.0), reverse=True)

    lat = int((time.monotonic() - t0) * 1000)

    # --- Determine lists actually screened --------------------------------
    screened_lists: list[str] = []
    if not os_unavail:
        # OpenSanctions was available -- we covered all its lists
        screened_lists.append(
            "OpenSanctions (OFAC-SDN, EU-Financial-Sanctions, UN-Security-Council, "
            "UK-HMT-Financial-Sanctions, and 40+ more)"
        )
    if not ofac_unavail:
        screened_lists.append(
            "OFAC-SDN (US Treasury Specially Designated Nationals, "
            "published by sanctionslistservice.ofac.treas.gov)"
        )
    # NAME EACH LIST THAT ACTUALLY RAN. A caller with a European obligation
    # needs to know whether the EU list was screened on THIS call, not whether
    # we support it in principle - so a list that failed to load is absent from
    # here and present in sources_unavailable.
    if not eu_unavail:
        screened_lists.append(
            "EU-CONSOLIDATED (European Commission consolidated financial "
            "sanctions, webgate.ec.europa.eu)"
        )
    if not uk_unavail:
        screened_lists.append(
            "UK-SANCTIONS (UK Sanctions List published by the FCDO, "
            "sanctionslist.fcdo.gov.uk)"
        )

    # Fall back to honest "partial" if everything failed
    if not screened_lists:
        screened_lists = ["(all sources unavailable -- see sources_unavailable field)"]

    # --- WHEN THE AUTHORITATIVE SOURCE IS DARK, DO NOT ASSERT A MATCH -------
    #
    # This is the most important safety rule in the file, and it exists because
    # of what the tool was doing live on 2026-08-29:
    #
    #   "Mohammed Ali"       -> MATCH, score 1.00, programme US-TERR
    #   "Maria Garcia"       -> MATCH, score 1.00, programme US-NARCO
    #   "Star Trading LLC"   -> MATCH, score 1.00, programme US-DRC
    #   "Delta Services Group" -> MATCH, score 1.00, US-RUSHAR
    #
    # Ordinary names, at MAXIMUM confidence, on terrorism and narcotics
    # programmes. Four rounds of fixes that afternoon each closed one specific
    # false positive and none touched the cause: our local matcher compares
    # word sets, so any short name whose distinctive words appear anywhere
    # inside one of ~17,000 long listed names scores perfectly.
    #
    # A word-overlap matcher cannot do this job. Deciding that "Ali Mohammed"
    # is a common name and "Zarubezhneft" is not requires knowing how often
    # each token occurs across the corpus. OpenSanctions does exactly that and
    # returns a calibrated score - which is why it is the primary source.
    #
    # OUR OPENSANCTIONS KEY IS OVER ITS MONTHLY LIMIT (HTTP 429), so every
    # answer above came from the local OFAC-CSV fallback alone. The fallback is
    # useful for confirming a name IS listed; it is not fit to assert that an
    # ordinary name is a sanctions hit.
    #
    # So when the calibrated source did not answer, a local-only hit is
    # reported as a POSSIBLE match requiring verification, and `matched` stays
    # false unless the names are effectively identical. Under-claiming costs
    # the caller one lookup. Over-claiming tells someone that Maria Garcia is a
    # narcotics trafficker.
    # THIS PREDICATE WAS ALWAYS FALSE, AND THAT ACCIDENT WAS THE ONLY THING
    # KEEPING THE TOOL HONEST.
    #
    # `all_sources_queried` holds URLs - "https://api.opensanctions.org/..." -
    # and the test was case-SENSITIVE for "OpenSanctions", which never appears
    # in a lowercase URL. So `authoritative_ran` was False on every call,
    # `degraded` was True on every call, and the strict token-set filter below
    # ran on every call. The tool behaved well for a reason nobody intended.
    #
    # That made it a booby trap: the obvious one-line fix to the casing would
    # have silently switched the filter OFF whenever OpenSanctions answered,
    # exposing raw word-overlap scores as findings. A safety that depends on a
    # bug is not a safety.
    #
    # So: the predicate is correct now (it means what it says), AND the filter
    # no longer depends on it - see below.
    # "QUERIED" IS NOT "ANSWERED", and my first correction of this predicate
    # conflated them. Making the test case-insensitive turned it TRUE whenever
    # the OpenSanctions URL appeared in sources_QUERIED - which it always does,
    # because we always attempt the call. The failure is recorded separately,
    # in sources_UNAVAILABLE.
    #
    # So the flag started claiming the calibrated source had run on calls where
    # it had been rate-limited and returned nothing, which suppressed the
    # "LOCAL-LIST CHECK ONLY" warning a caller needs.
    #
    # The strict filter did not regress with it, because it is tied to match
    # provenance rather than to this flag. That decoupling was worth doing for
    # exactly this reason: one wrong predicate should not be able to switch off
    # the safety AND the disclosure at once.
    _os_failed = any("opensanctions" in u.lower() for u in all_sources_unavailable)
    authoritative_ran = (not _os_failed) and any(
        "opensanctions" in s.lower() for s in all_sources_queried)
    degraded = bool(merged) and not authoritative_ran

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
    _needs_strict = [m for m in merged
                     if m.get("_matcher") == "local_word_overlap"]
    if degraded or _needs_strict:
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
            # A calibrated match is evidence on its own; do not re-filter it.
            if m.get("_matcher") == "opensanctions_calibrated" and not degraded:
                confident.append(m)
                continue
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
            if q_set and set(_normalize_name(m.get("name", "")).split()) == q_set:
                confident.append(m)
            else:
                possible.append(m)
        merged, unverified = confident, possible

    # --- Build result payload ---------------------------------------------
    matched = len(merged) > 0
    result_payload: dict = {
        "matched": matched,
        "matches": merged,
        "lists_screened": screened_lists,
        "sources_queried": all_sources_queried,
        "screened_at": screened_at,
        "disclaimer": _DISCLAIMER,
    }
    if all_sources_unavailable:
        result_payload["sources_unavailable"] = all_sources_unavailable
    if unverified:
        # Surfaced, never hidden. The caller may well want to look at these -
        # they simply must not be handed over as findings.
        result_payload["possible_matches_unverified"] = unverified
        result_payload["degraded"] = (
            "The calibrated sanctions source did not answer, so these are "
            "name-similarity candidates from a local list only. They are NOT "
            "sanctions findings and matched=false. Check each against the "
            "official source before acting on it.")

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
    else:
        no_match_detail = (
            f"No matches on the screened lists for '{_ascii(name_clean)}'"
            + (f" (country filter: {country_upper})" if country_upper else "")
            + ". Screened: "
            + "; ".join(screened_lists)
            # Say it in the sentence a caller actually reads, not only in a
            # field they may not parse. "No match" from a degraded screen means
            # something weaker than "no match" from a complete one, and an
            # agent deciding whether to trade needs to know which it got.
            + ("" if not degraded else
               f". WARNING: the calibrated source did not answer, so this was a "
               f"LOCAL-LIST CHECK ONLY and is not a complete screening. "
               f"{len(unverified)} name-similarity candidate(s) were found and "
               f"are in possible_matches_unverified - they are not findings.")
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
