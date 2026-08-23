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
_OFAC_SDN_CSV_URL = "https://ofac.treasury.gov/downloads/sdn.csv"
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
    """Normalize a name for fuzzy matching: lower, alphanum + spaces only."""
    lower = name.lower()
    # Replace hyphens and commas with space (common in SDN names like "AL-QA'IDA")
    lower = re.sub(r"[-,']", " ", lower)
    # Strip all non-alphanumeric chars except spaces
    cleaned = re.sub(r"[^a-z0-9\s]", "", lower)
    # Collapse whitespace
    return " ".join(cleaned.split())


def _word_match_score(query: str, candidate: str) -> float:
    """Compute word-overlap score between normalized query and candidate name.

    score = |query_words & candidate_words| / max(|query_words|, |candidate_words|)
    Returns 0.0-1.0. Higher = better match.
    """
    q_words = set(_normalize_name(query).split())
    c_words = set(_normalize_name(candidate).split())
    if not q_words or not c_words:
        return 0.0
    intersection = q_words & c_words
    return len(intersection) / max(len(q_words), len(c_words))


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
    Parse OFAC SDN CSV and return matches for query_name.

    OFAC SDN CSV format (semicolon-delimited, no header in classic format;
    newer versions may have a header row starting with 'ent_num'):
      field 0: ent_num
      field 1: sdn_name (primary name, typically "LAST NAME, FIRST NAME" or entity name)
      field 2: sdn_type (individual / entity / vessel / aircraft)
      field 3: program (sanctions program, may be comma-separated)
      field 4: title (individuals only)
      fields 5-11: vessel fields
      field 11: remarks

    Rows that begin with "-0-" are continuation lines for long remarks; we skip them
    for name matching purposes.
    """
    matches: list[dict] = []
    seen_names: set[str] = set()  # deduplicate

    try:
        reader = csv.reader(io.StringIO(csv_text), delimiter=";", quotechar='"')
        for row in reader:
            # Skip empty rows, header rows, and continuation lines
            if not row:
                continue
            first_field = row[0].strip()
            if not first_field or first_field.startswith("-0-"):
                continue
            # Skip header row if present (newer OFAC format has it)
            if first_field.lower() in ("ent_num", "#", "ent num"):
                continue

            if len(row) < 4:
                continue

            sdn_name = row[1].strip() if len(row) > 1 else ""
            if not sdn_name or sdn_name == "-0-":
                continue

            score = _word_match_score(query_name, sdn_name)
            if score < _MATCH_THRESHOLD_OFAC:
                continue

            sdn_type = row[2].strip().upper() if len(row) > 2 else ""
            program = row[3].strip() if len(row) > 3 else ""

            # entity_type label
            if sdn_type in ("INDIVIDUAL", "I"):
                etype = "INDIVIDUAL"
            elif sdn_type in ("VESSEL", "V"):
                etype = "VESSEL"
            elif sdn_type in ("AIRCRAFT", "A"):
                etype = "AIRCRAFT"
            else:
                etype = "ENTITY"

            name_key = _normalize_name(sdn_name)
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            matches.append({
                "name": _ascii(sdn_name),
                "list": "OFAC-SDN",
                "match_score": round(score, 3),
                "program": _ascii(program[:80]) if program else None,
                "entity_type": etype,
                "source_url": "https://ofac.treasury.gov/sanctions-list-service",
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
    Query the official OFAC SDN list via Treasury CSV download.
    Returns (matches, sources_queried, sources_unavailable).
    """
    sources_queried = [_OFAC_SDN_CSV_URL]
    sources_unavailable: list[str] = []

    csv_text = await _fetch_ofac_sdn_csv()
    if csv_text is None:
        sources_unavailable.append("OFAC-SDN (download failed)")
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
        screened_lists.append("OFAC-SDN (US Treasury Specially Designated Nationals)")

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
