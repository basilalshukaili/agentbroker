"""
verify_company_record -- free, read-only company registry lookup.

Data sources (free, no API key, live calls):
  1. GLEIF LEI API  https://api.gleif.org/api/v1/  -- global legal entities, primary.
  2. SEC EDGAR company search  https://www.sec.gov/  -- US public companies,
     fallback / enrichment.

Design:
  * 10-second timeout per upstream; fail-open to partial results.
  * If both upstreams time out or fail, returns status=not_found with
    sources_unavailable populated -- never fabricates.
  * All string output is ASCII-safe (non-ASCII chars are replaced with '?').
  * Cost: 0.00 USD (free read tool, probe for demand).
  * Telemetry: fires via the existing mcp_server dispatch hook (usage_events row).
"""
from __future__ import annotations

import re
import time
import unicodedata
import urllib.parse
import uuid
from typing import Optional

from core.models import CostRecord, OperationStatus, OutcomeReceipt

_GLEIF_BASE = "https://api.gleif.org/api/v1"
_EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q={q}&dateRange=custom&startdt=2000-01-01&enddt=2030-01-01&_source=file_date,period_of_report,entity_name,file_num,period_of_report,form_type,inc_states,locations&forms=10-K"
_EDGAR_COMPANY_SEARCH = "https://www.sec.gov/cgi-bin/browse-edgar?company={q}&CIK=&type=10-K&dateb=&owner=include&count=5&search_text=&action=getcompany"
_TIMEOUT = 10  # seconds per upstream


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


class RegistryUnavailable(RuntimeError):
    """The registry did not answer. NOT the same as "no record exists".

    _gleif_by_name and _edgar_search each caught their own exceptions and
    returned None, so the outer handlers that append to `sources_unavailable`
    could only ever fire on an ImportError. With both upstreams down this tool
    returned status "not_found" and the sentence "No registry record found for
    X. Searched: GLEIF, SEC EDGAR" - with sources_unavailable EMPTY.

    A registered company confidently reported as unregistered, by a tool sold
    for company verification. The distinction this exception exists to keep is
    the entire product.
    """


async def _gleif_by_lei(lei: str) -> Optional[dict]:
    """Direct LEI lookup. Returns the raw GLEIF entity dict or None."""
    import httpx
    url = f"{_GLEIF_BASE}/lei-records/{lei.strip().upper()}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            return resp.json().get("data")
        raise RegistryUnavailable(f"GLEIF returned HTTP {resp.status_code}")
    except RegistryUnavailable:
        raise
    except Exception as exc:                    # noqa: BLE001
        raise RegistryUnavailable(f"GLEIF unreachable: {exc}") from exc


async def _gleif_by_name(name: str, country: Optional[str] = None) -> list[dict]:
    """Search GLEIF by legal name. Returns list of matching entity dicts."""
    import httpx
    params: dict = {
        "filter[entity.legalName]": name,
        "page[size]": 5,
    }
    if country:
        params["filter[entity.legalAddress.country]"] = country.upper()
    url = f"{_GLEIF_BASE}/lei-records?" + urllib.parse.urlencode(params)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            return resp.json().get("data", [])
        raise RegistryUnavailable(f"GLEIF returned HTTP {resp.status_code}")
    except RegistryUnavailable:
        raise
    except Exception as exc:                    # noqa: BLE001
        raise RegistryUnavailable(f"GLEIF unreachable: {exc}") from exc


def _parse_gleif_entity(data: dict) -> dict:
    """Extract the fields we care about from a GLEIF entity dict."""
    attrs = data.get("attributes", {})
    entity = attrs.get("entity", {})
    reg = attrs.get("registration", {})

    # Address
    addr_obj = entity.get("legalAddress") or {}
    address_parts = [
        addr_obj.get("addressLines", [""])[0] if addr_obj.get("addressLines") else "",
        addr_obj.get("city", ""),
        addr_obj.get("postalCode", ""),
        addr_obj.get("country", ""),
    ]
    address = _ascii(", ".join(p for p in address_parts if p))

    return {
        "lei": _clean(data.get("id") or attrs.get("lei")),
        "legal_name": _clean(entity.get("legalName", {}).get("name", "")),
        "status": _clean(entity.get("status", "")),
        "jurisdiction": _clean(entity.get("jurisdiction", "") or addr_obj.get("country", "")),
        "registered_address": address or None,
        "registry_authority": _clean(
            reg.get("managingLou", {}).get("name", "") if isinstance(reg.get("managingLou"), dict)
            else str(reg.get("managingLou", "")) if reg.get("managingLou") else ""
        ),
        "registration_status": _clean(reg.get("status", "")),
        "next_renewal": _clean(reg.get("nextRenewalDate", "")),
    }


_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc",
    "lp", "llp", "plc", "ltd", "limited", "sa", "sas", "ag", "gmbh", "nv",
    "bv", "ab", "as", "oy", "spa", "srl", "pte", "pty", "kk", "the", "group",
    "holdings", "holding",
})

# Long forms one registry spells out and the other abbreviates. Applied as
# whole phrases before tokenising - see the note in _same_entity.
_LEGAL_PHRASES = {
    "public limited company": "plc",
    "public limited co": "plc",
    "limited liability company": "llc",
    "limited liability partnership": "llp",
    "societe anonyme": "sa",
    "naamloze vennootschap": "nv",
    "aktiengesellschaft": "ag",
    "gesellschaft mit beschrankter haftung": "gmbh",
}


def _same_entity(a: Optional[str], b: Optional[str]) -> bool:
    """Are these two registry names plausibly the same company?

    Used to decide whether a SEC EDGAR hit may lend its CIK and ticker to a
    GLEIF record. The bar is deliberately asymmetric: a FALSE match invents a
    company that does not exist, while a false NON-match only means we publish
    a real GLEIF record without a ticker, and say so.

    Corporate suffixes are dropped because the two registries disagree about
    them constantly - GLEIF writes "APPLE INC.", SEC writes "Apple Inc." and
    for others "CORPORATION" against "Corp". What is left has to match as a
    set, so "Apple" never merges with "Apple Hospitality REIT".
    """
    def _toks(text: Optional[str]) -> list:
        cleaned = "".join(
            c if (c.isalnum() or c.isspace()) else " " for c in (text or "").lower())
        cleaned = " ".join(cleaned.split())
        # PHRASES, NOT WORDS. "Public limited company" is the expansion of
        # "plc" and GLEIF writes it out in full where SEC abbreviates, so
        # Vodafone did not merge with itself. The word "public" cannot simply
        # join _LEGAL_SUFFIXES to fix that: Public Storage is a real listed
        # company, and dropping the word would let it merge with anything else
        # called Storage. The whole phrase is unambiguous; the word is not.
        for phrase, short in _LEGAL_PHRASES.items():
            if phrase in cleaned:
                cleaned = cleaned.replace(phrase, short)
        return [t for t in cleaned.split() if t]

    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return False
    if set(ta) == set(tb):
        return True                     # identical but for case and punctuation

    # A SUFFIX MAY BE SPELLED DIFFERENTLY. IT MAY NOT BE ABSENT.
    #
    # Ignoring suffixes on both sides bridges "Microsoft Corporation" and
    # "Microsoft Corp", which is the whole point. But it ALSO bridged "Apple"
    # and "Apple Inc." - and an existing test forbids exactly that, with its
    # reasoning written down: a caller who types a bare first word has not
    # named a legal entity, and resolving it to one puts a CIK the caller
    # never asked for onto a compliance receipt.
    #
    # So both names must actually CARRY a corporate form before their forms
    # are allowed to differ. "Corporation" vs "Corp" is a spelling; nothing
    # vs "Inc." is a missing word.
    sa = [t for t in ta if t not in _LEGAL_SUFFIXES]
    sb = [t for t in tb if t not in _LEGAL_SUFFIXES]
    if len(sa) == len(ta) or len(sb) == len(tb):
        return False                    # one side names no corporate form
    # "The Limited" is a real company whose every token is a suffix; an empty
    # remainder identifies nobody.
    return bool(sa) and set(sa) == set(sb)


async def _edgar_search(name: str) -> Optional[dict]:
    """
    Search SEC EDGAR company_tickers.json for US public companies.
    Returns a minimal record dict or None.
    Sends an honest User-Agent per SEC fair-access policy.
    """
    import httpx
    ua = "AgentBroker/1.0 (company-verification; contact support@hatchloop.dev)"  # SEC EDGAR requires a REAL contact; smb-broker.example is not a domain
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _EDGAR_TICKERS_URL,
                headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            )
        if resp.status_code != 200:
            raise RegistryUnavailable(f"SEC EDGAR returned HTTP {resp.status_code}")
        tickers = resp.json()
        # PUNCTUATION-INSENSITIVE, NOT FUZZY.
        #
        # The match was `entry["title"].lower() == name.lower()`, and SEC
        # stores "Apple Inc." with a trailing period. So verify_company_record
        # returned GLEIF data for Apple with sec_cik and ticker BOTH NULL,
        # while listing sec.gov/files/company_tickers.json in sources_queried -
        # a receipt saying we asked SEC about Apple and SEC had nothing. The
        # enrichment only ever worked if the caller typed the name
        # character-for-character as SEC stores it, which nobody does.
        #
        # Deliberately NOT a contains-match: the same file holds "Apple
        # Hospitality REIT, Inc." and "Pineapple Financial Inc.", so a loose
        # match would attach the wrong CIK to a company-verification receipt.
        # Normalising punctuation fixes the real miss without inviting that.
        def _norm(text: str) -> str:
            return " ".join(
                "".join(c for c in text.lower() if c.isalnum() or c.isspace())
                .split())

        name_lower = _norm(name)

        def _hit(entry: dict) -> dict:
            return {
                "legal_name": _clean(entry.get("title", "")),
                "ticker": _clean(str(entry.get("ticker", ""))),
                "cik": str(entry.get("cik_str", "")),
                "registry_authority": "SEC EDGAR (US public companies)",
                "jurisdiction": "US",
                "status": "active",
            }

        # Linear scan -- JSON is ~6 MB, ~15k entries; fast enough in memory
        suffix_matches: list = []
        for _key, entry in tickers.items():
            title = entry.get("title", "")
            if _norm(title) == name_lower:
                return _hit(entry)                  # exact wins outright
            # AND THE ABBREVIATIONS, which punctuation-insensitivity did not
            # reach. SEC stores "Microsoft Corp"; ask about "Microsoft
            # Corporation" - the name GLEIF holds - and the scan above found
            # nothing, so the receipt listed sec.gov as queried and returned
            # sec_cik: null for the third-largest company on the exchange.
            # Same defect as the Apple full stop, one abbreviation further on.
            if _same_entity(title, name):
                suffix_matches.append(entry)

        # AMBIGUITY IS NOT A TIE TO BREAK. If dropping suffixes makes two
        # different filers look alike, picking one attaches a real CIK to the
        # wrong company - the exact invention this file now guards against on
        # the merge.
        if len({str(e.get("cik_str", "")) for e in suffix_matches}) == 1:
            return _hit(suffix_matches[0])
        if suffix_matches:
            # AND IT IS REPORTED, not swallowed. "SEC had nothing" and "SEC
            # had several and we would not guess" are different answers, and
            # returning None for both is how this file's other bugs started.
            return {
                "ambiguous": [
                    {"legal_name": _clean(e.get("title", "")),
                     "ticker": _clean(str(e.get("ticker", ""))),
                     "cik": str(e.get("cik_str", ""))}
                    for e in suffix_matches[:5]
                ],
            }
        return None
    # RegistryUnavailable MUST ESCAPE. It subclasses RuntimeError, so the bare
    # `except Exception` below caught the raise two lines above it - the
    # function raised and then swallowed its own exception, and the commit
    # that added the raise claimed the tool now reports outages honestly.
    # It did not. This is the re-raise that makes the raise mean something.
    except RegistryUnavailable:
        raise
    except Exception as exc:                    # noqa: BLE001
        raise RegistryUnavailable(f"SEC EDGAR unreachable: {exc}") from exc
    return None


async def handle_verify_company_record(
    name: str,
    country: Optional[str] = None,
    lei: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    op_id = str(uuid.uuid4())
    sources_queried: list[str] = []
    sources_unavailable: list[str] = []

    # --- Input validation -------------------------------------------------
    if not name or not name.strip():
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message="name is required -- provide the company legal name to look up.",
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    name = name.strip()
    country_upper = country.strip().upper() if country else None

    record: Optional[dict] = None

    # --- 1. GLEIF lookup (primary) ----------------------------------------
    gleif_url = (
        f"{_GLEIF_BASE}/lei-records/{lei.strip().upper()}"
        if lei
        else (
            f"{_GLEIF_BASE}/lei-records?"
            + urllib.parse.urlencode({
                "filter[entity.legalName]": name,
                **({"filter[entity.legalAddress.country]": country_upper} if country_upper else {}),
                "page[size]": 5,
            })
        )
    )
    sources_queried.append(gleif_url)

    try:
        if lei:
            data = await _gleif_by_lei(lei)
            entities = [data] if data else []
        else:
            entities = await _gleif_by_name(name, country_upper)

        if entities:
            record = _parse_gleif_entity(entities[0])
            record["source"] = "GLEIF"
    except Exception:
        sources_unavailable.append("GLEIF")

    # --- 2. SEC EDGAR enrichment / fallback for US companies --------------
    is_us = (country_upper == "US") if country_upper else True  # probe EDGAR when no country given
    edgar_url = _EDGAR_TICKERS_URL
    edgar_record: Optional[dict] = None

    if is_us:
        sources_queried.append(edgar_url)
        try:
            edgar_record = await _edgar_search(name)
        except Exception:
            sources_unavailable.append("SEC EDGAR")

    # Merge: if GLEIF found something, annotate with EDGAR CIK if available.
    #
    # THE TWO LOOKUPS ARE KEYED ON DIFFERENT THINGS AND WERE NEVER COMPARED.
    #
    # GLEIF is keyed on the LEI when one is supplied; EDGAR is always keyed on
    # `name`. So verify_company_record(name="Apple Inc", lei=<Tesla's LEI>)
    # fetched TESLA from GLEIF, fetched APPLE from SEC, and stapled them into
    # one record: legal_name "TESLA, INC." carrying ticker AAPL and cik 320193.
    # Every field in that receipt is real and the entity it describes does not
    # exist. A verification tool inventing a company is the worst output in
    # this file, worse than any outage, because nothing about it looks wrong.
    #
    # So the enrichment now has to prove the two records are the same company.
    merge_conflict: Optional[dict] = None

    # SEC found several filers whose names differ only by corporate suffix.
    # No CIK is attached and the candidates are handed back, so the caller can
    # disambiguate instead of being told SEC knew nothing.
    if edgar_record and edgar_record.get("ambiguous"):
        merge_conflict = {
            "sec_candidates": edgar_record["ambiguous"],
            "reason": ("SEC EDGAR held more than one filer matching this name "
                       "once corporate suffixes are set aside, so no CIK or "
                       "ticker was attached - supply the exact SEC name to "
                       "pick one"),
        }
        edgar_record = None

    if record and edgar_record:
        if _same_entity(record.get("legal_name"), edgar_record.get("legal_name")):
            record["sec_cik"] = edgar_record.get("cik")
            record["ticker"] = edgar_record.get("ticker")
            record["sources"] = ["GLEIF", "SEC EDGAR"]
        else:
            # Disclosed, not discarded silently: the caller asked about a name
            # SEC does know, and the mismatch is the single most useful fact we
            # learned on this call.
            merge_conflict = {
                "sec_legal_name": edgar_record.get("legal_name"),
                "sec_cik": edgar_record.get("cik"),
                "sec_ticker": edgar_record.get("ticker"),
                "reason": ("SEC EDGAR matched a different company than the "
                           "registry record above, so its identifiers were "
                           "NOT attached to this entity"),
            }
            record["sources"] = ["GLEIF"]
    elif record:
        record["sources"] = ["GLEIF"]
    elif edgar_record:
        record = edgar_record
        record["lei"] = None
        record["sources"] = ["SEC EDGAR"]

    lat = int((time.monotonic() - t0) * 1000)

    # --- Not found --------------------------------------------------------
    if not record:
        # NOTHING FOUND IS NOT THE SAME AS NOTHING SEARCHED.
        #
        # When every registry we consulted was unavailable, "no record found"
        # and "the company may not be a legal entity" are both unsupported -
        # we did not look. Reporting them anyway is how a registered company
        # gets confidently described as unregistered by a tool sold for
        # company verification.
        # COUNT THE REGISTRIES, do not string-match their names against URLs.
        # My first version compared "GLEIF" against "https://api.gleif.org/..."
        # and never matched, so the branch below could not fire - a guard that
        # silently does nothing, which is the defect this whole file is being
        # audited for.
        #
        # THEN THE COUNT ITSELF WAS WRONG, in the direction that matters.
        # `_attempted = 1 + (1 if is_us else 0)` with is_us defaulting True
        # meant a caller who named no country needed BOTH registries dark
        # before we would admit it. GLEIF down and EDGAR up produced the full
        # not_found sentence - "the company may not be a legal entity
        # registered with these free registries" - for, say, a German company,
        # on the strength of its absence from a file of US-listed tickers.
        #
        # The registries are not interchangeable, so counting them was the
        # wrong shape. GLEIF is the only global entity registry we consult;
        # EDGAR covers US public companies alone. If GLEIF did not answer, we
        # have no basis for "not registered" about anyone.
        _all_dark = "GLEIF" in sources_unavailable
        if _all_dark:
            return OutcomeReceipt(
                operation_id=op_id,
                status=OperationStatus.SUCCESS,
                reason_code="partial_lookup",
                human_message=(
                    f"COULD NOT VERIFY '{_ascii(name)}': the GLEIF global "
                    f"entity registry was unavailable on this call ("
                    + ", ".join(_ascii(u) for u in sources_unavailable)
                    + " unreachable). This is NOT evidence that the company is "
                      "unregistered - GLEIF is the only worldwide registry we "
                      "consult, and it did not answer. Retry, or check GLEIF "
                      "and SEC EDGAR directly."
                ),
                result={
                    "status": "unavailable",
                    "queried_name": _ascii(name),
                    "queried_country": country_upper,
                    "queried_lei": _clean(lei) if lei else None,
                    "sources_queried": [_ascii(x) for x in sources_queried],
                    "sources_unavailable": [_ascii(x) for x in sources_unavailable],
                },
                cost=CostRecord(amount=0.0, currency="USD", basis="free"),
                latency_ms=lat,
                retriable=True,
                trace_id=trace_id,
            )
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.SUCCESS,
            reason_code="not_found",
            human_message=(
                f"No registry record found for '{_ascii(name)}'"
                + (f" in {country_upper}" if country_upper else "")
                + ". Searched: GLEIF global entity registry"
                + (", SEC EDGAR" if is_us else "")
                + ". The company may not be a legal entity registered with these free registries."
                + (f" NOTE: {len(sources_unavailable)} source(s) were "
                   f"unavailable, so this is a partial search."
                   if sources_unavailable else "")
            ),
            result={
                "status": "not_found",
                "queried_name": _ascii(name),
                "queried_country": country_upper,
                "queried_lei": _clean(lei) if lei else None,
                "sources_queried": [_ascii(s) for s in sources_queried],
                "sources_unavailable": [_ascii(s) for s in sources_unavailable],
            },
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=lat,
            retriable=False,
            trace_id=trace_id,
        )

    # --- Found -----------------------------------------------------------
    result_payload = {
        "status": "found",
        "legal_name": record.get("legal_name") or _ascii(name),
        "lei": record.get("lei"),
        "entity_status": record.get("status") or record.get("registration_status"),
        "jurisdiction": record.get("jurisdiction"),
        "registered_address": record.get("registered_address"),
        "registry_authority": record.get("registry_authority"),
        "registration_status": record.get("registration_status"),
        "next_renewal": record.get("next_renewal"),
        "ticker": record.get("ticker"),
        "sec_cik": record.get("sec_cik"),
        "sources": record.get("sources", []),
        "sources_queried": [_ascii(s) for s in sources_queried],
    }
    if sources_unavailable:
        result_payload["sources_unavailable"] = [_ascii(s) for s in sources_unavailable]
    if merge_conflict:
        result_payload["unmerged_sec_match"] = {
            k: (_ascii(v) if isinstance(v, str) else v)
            for k, v in merge_conflict.items()
        }

    return OutcomeReceipt(
        operation_id=op_id,
        status=OperationStatus.SUCCESS,
        reason_code="found",
        human_message=(
            f"Found registry record for '{result_payload['legal_name']}'"
            + (f" (LEI: {result_payload['lei']})" if result_payload.get("lei") else "")
            + (f", jurisdiction: {result_payload['jurisdiction']}" if result_payload.get("jurisdiction") else "")
            + (f", status: {result_payload['entity_status']}" if result_payload.get("entity_status") else "")
            + "."
            + (f" NOTE: you asked about '{_ascii(name)}' and SEC EDGAR matched "
               f"'{_ascii(str(merge_conflict.get('sec_legal_name') or ''))}', a "
               f"DIFFERENT company from the registry record above - so no "
               f"ticker or CIK has been attached. The two identifiers you "
               f"supplied may not belong to the same entity."
               if merge_conflict and merge_conflict.get("sec_legal_name") else "")
            + (f" NOTE: SEC EDGAR held "
               f"{len(merge_conflict.get('sec_candidates') or [])} filers "
               f"matching this name once corporate suffixes are set aside, so "
               f"no ticker or CIK has been attached - see sec_candidates."
               if merge_conflict and merge_conflict.get("sec_candidates") else "")
        ),
        result=result_payload,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=lat,
        retriable=False,
        trace_id=trace_id,
    )
