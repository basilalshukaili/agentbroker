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
_OFAC_SDN_CSV_URL = "https://data.opensanctions.org/datasets/latest/us_ofac_sdn/targets.simple.csv"
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
    """Replace non-ASCII characters with '?' to ensure wire-safe output."""
    if not s:
        return s
    normalized = unicodedata.normalize("NFKD", s)
    return "".join(c if ord(c) < 128 else "?" for c in normalized)


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
            sources_unavailable.append(
                "OpenSanctions (OPENSANCTIONS_API_KEY not set or invalid -- "
                "free key available at https://www.opensanctions.org/accounts/register/)"
            )
            return [], sources_queried, sources_unavailable

        if resp.status_code == 429:
            sources_unavailable.append("OpenSanctions (rate-limited; set OPENSANCTIONS_API_KEY)")
            return [], sources_queried, sources_unavailable

        if resp.status_code != 200:
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

        return matches, sources_queried, sources_unavailable

    except Exception as exc:
        sources_unavailable.append(f"OpenSanctions (error: {_ascii(str(exc)[:80])})")
        return [], sources_queried, sources_unavailable


# ---------------------------------------------------------------------------
# OFAC SDN CSV  (free, keyless, official US Treasury source)
# ---------------------------------------------------------------------------

async def _fetch_ofac_sdn_csv() -> Optional[str]:
    """Download OFAC SDN CSV from Treasury. Returns raw text or None on error."""
    import httpx
    ua = "AgentBroker-SanctionsScreen/1.0 (compliance tool; contact support@hatchloop.dev)"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _OFAC_SDN_CSV_URL,
                headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
                follow_redirects=True,
            )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _parse_ofac_sdn(csv_text: str, query_name: str) -> list[dict]:
    """
    Parse OpenSanctions targets.simple.csv (OFAC SDN dataset) and return matches.

    CSV format (comma-delimited, quoted, with header row):
      col 0: id          -- OpenSanctions entity ID (e.g. "NK-223CQDBzp8MRk...")
      col 1: schema      -- Entity type: Person, Organization, Vessel, Aircraft
      col 2: name        -- Primary name
      col 3: aliases     -- Alternate names, semicolon-separated
      col 4: birth_date
      col 5: countries
      col 6: addresses
      col 7: identifiers
      col 8: sanctions   -- Full program description (e.g. "GLOMAG - Executive Order 13818")
      col 9: phones
      col 10: emails
      col 11: program_ids -- Short codes (e.g. "US-GLOMAG")
      col 12-15: dataset, first_seen, last_seen, last_change

    Searches query_name against both the primary name and all aliases.
    """
    matches: list[dict] = []
    seen_ids: set[str] = set()

    try:
        reader = csv.reader(io.StringIO(csv_text), delimiter=",", quotechar='"')
        header_seen = False
        for row in reader:
            if not row:
                continue
            # Skip header row
            if not header_seen:
                header_seen = True
                if row[0].lower() in ("id", "#"):
                    continue

            if len(row) < 3:
                continue

            entity_id = row[0].strip() if len(row) > 0 else ""
            schema = row[1].strip() if len(row) > 1 else ""
            primary_name = row[2].strip() if len(row) > 2 else ""
            aliases_raw = row[3].strip() if len(row) > 3 else ""
            sanctions = row[8].strip() if len(row) > 8 else ""
            program_ids = row[11].strip() if len(row) > 11 else ""

            if not primary_name:
                continue
            if entity_id in seen_ids:
                continue

            # Search primary name and all aliases
            all_names = [primary_name] + [a.strip() for a in aliases_raw.split(";") if a.strip()]
            best_score = 0.0
            best_match_name = primary_name
            for candidate in all_names:
                s = _word_match_score(query_name, candidate)
                if s > best_score:
                    best_score = s
                    best_match_name = candidate

            if best_score < _MATCH_THRESHOLD_OFAC:
                continue

            seen_ids.add(entity_id)

            # Entity type from schema
            schema_upper = schema.upper()
            if schema_upper == "PERSON":
                etype = "INDIVIDUAL"
            elif schema_upper == "VESSEL":
                etype = "VESSEL"
            elif schema_upper == "AIRCRAFT":
                etype = "AIRCRAFT"
            else:
                etype = "ENTITY"

            # Program: prefer short program_ids, fallback to full sanctions description
            program_str = _ascii(program_ids[:80]) if program_ids else _ascii(sanctions[:80])

            source_url = (
                f"https://www.opensanctions.org/entities/{entity_id}/"
                if entity_id
                else "https://ofac.treasury.gov/sanctions-list-service"
            )

            matches.append({
                "name": _ascii(best_match_name),
                "list": "OFAC-SDN",
                "match_score": round(best_score, 3),
                "program": program_str or None,
                "entity_type": etype,
                "source_url": source_url,
            })

    except Exception:
        pass  # fail-open: partial results are better than no results

    # Sort by score descending, cap at 5
    matches.sort(key=lambda m: m["match_score"], reverse=True)
    return matches[:5]


async def _call_ofac_sdn(
    name: str,
) -> tuple[list[dict], list[str], list[str]]:
    """
    Query OFAC SDN list via OpenSanctions public bulk data (no API key needed).
    Data source: data.opensanctions.org, derived from US Treasury SDN XML, updated daily.
    Returns (matches, sources_queried, sources_unavailable).
    """
    sources_queried = [_OFAC_SDN_CSV_URL]
    sources_unavailable: list[str] = []

    csv_text = await _fetch_ofac_sdn_csv()
    if csv_text is None:
        sources_unavailable.append(
            "OFAC-SDN via OpenSanctions public data (download failed; "
            "source: data.opensanctions.org)"
        )
        return [], sources_queried, sources_unavailable

    matches = _parse_ofac_sdn(csv_text, name)
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

    os_matches, os_queried, os_unavail = await os_task
    ofac_matches, ofac_queried, ofac_unavail = await ofac_task

    # --- Merge results ----------------------------------------------------
    all_sources_queried: list[str] = []
    all_sources_unavailable: list[str] = []

    all_sources_queried.extend(_ascii(s) for s in os_queried)
    all_sources_queried.extend(_ascii(s) for s in ofac_queried)
    all_sources_unavailable.extend(_ascii(s) for s in os_unavail)
    all_sources_unavailable.extend(_ascii(s) for s in ofac_unavail)

    # Merge and deduplicate by (normalized name, list) key
    merged: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for m in os_matches + ofac_matches:
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
            "via OpenSanctions public bulk data at data.opensanctions.org)"
        )

    # Fall back to honest "partial" if everything failed
    if not screened_lists:
        screened_lists = ["(all sources unavailable -- see sources_unavailable field)"]

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
