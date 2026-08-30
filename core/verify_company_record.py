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
        name_lower = name.lower()
        # Linear scan -- JSON is ~6 MB, ~15k entries; fast enough in memory
        for _key, entry in tickers.items():
            if entry.get("title", "").lower() == name_lower:
                return {
                    "legal_name": _clean(entry.get("title", "")),
                    "ticker": _clean(str(entry.get("ticker", ""))),
                    "cik": str(entry.get("cik_str", "")),
                    "registry_authority": "SEC EDGAR (US public companies)",
                    "jurisdiction": "US",
                    "status": "active",
                }
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

    # Merge: if GLEIF found something, annotate with EDGAR CIK if available
    if record and edgar_record:
        record["sec_cik"] = edgar_record.get("cik")
        record["ticker"] = edgar_record.get("ticker")
        record["sources"] = ["GLEIF", "SEC EDGAR"]
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
        _attempted = 1 + (1 if is_us else 0)     # GLEIF, plus EDGAR when US
        _all_dark = len(sources_unavailable) >= _attempted
        if _all_dark:
            return OutcomeReceipt(
                operation_id=op_id,
                status=OperationStatus.SUCCESS,
                reason_code="partial_lookup",
                human_message=(
                    f"COULD NOT VERIFY '{_ascii(name)}': every registry we use "
                    f"was unavailable on this call ("
                    + ", ".join(_ascii(u) for u in sources_unavailable)
                    + "). This is NOT evidence that the company is "
                      "unregistered - we did not reach any registry. Retry, "
                      "or check GLEIF and SEC EDGAR directly."
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
        ),
        result=result_payload,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=lat,
        retriable=False,
        trace_id=trace_id,
    )
