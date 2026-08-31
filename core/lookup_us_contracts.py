"""
lookup_us_contracts -- free, read-only US federal contract award lookup.

Data source (free, no API key, live calls):
  USASpending.gov FPDS API  https://api.usaspending.gov/
  -- real federal contract award data, updated daily, no auth required.

Design:
  * Searches by recipient (company) name via the awards/search endpoint.
  * 10-second timeout; fail-open to partial results.
  * If the upstream is unavailable, returns status=unavailable -- never fabricates.
  * All string output is ASCII-safe (non-ASCII chars are replaced with '?').
  * Cost: 0.00 USD (free read tool, probe for demand).
  * Telemetry: fires via the existing mcp_server dispatch hook (usage_events row).

Demand signal: "us import data api", "supplier lookup api", "company trade records",
"who has government contracts", "federal contractor search" -- ImportYeti-adjacent.
"""
from __future__ import annotations

import time
import unicodedata
import uuid
from typing import Optional

from core.models import CostRecord, OperationStatus, OutcomeReceipt

_USASPENDING_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_USASPENDING_RECIPIENT_URL = "https://api.usaspending.gov/api/v2/recipient/duns/"
_TIMEOUT = 10  # seconds


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
    s = _ascii(str(v).strip())
    return s if s else None


def _dollars(v) -> Optional[float]:
    """Convert a value to a rounded dollar float, or None."""
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


async def _search_awards(company_name: str, max_results: int = 5) -> list[dict]:
    """
    Search USASpending.gov for federal contract awards by awardee/recipient name.
    Returns a list of award dicts (may be empty).
    Raises RuntimeError if the upstream is unreachable.
    """
    import httpx

    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],  # procurement contracts only
            "recipient_search_text": [company_name],
            "time_period": [{"start_date": "2020-01-01", "end_date": "2026-12-31"}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Start Date",
            "End Date",
            "Awarding Agency",
            "NAICS Code",
            "NAICS Description",
            "Description",
            "Period of Performance Start Date",
            "Period of Performance Current End Date",
        ],
        "page": 1,
        "limit": max(1, min(max_results, 10)),
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }

    ua = "AgentBroker/1.0 (supplier-lookup; contact support@hatchloop.dev)"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _USASPENDING_SEARCH_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": ua,
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"USASpending.gov returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return data.get("results", [])
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"USASpending.gov unreachable: {exc}") from exc


def _parse_award(raw: dict) -> dict:
    """Extract and normalise the fields we expose from a USASpending award row."""
    return {
        "award_id": _clean(raw.get("Award ID")),
        "recipient_name": _clean(raw.get("Recipient Name")),
        "award_amount_usd": _dollars(raw.get("Award Amount")),
        "awarding_agency": _clean(raw.get("Awarding Agency")),
        "naics_code": _clean(raw.get("NAICS Code")),
        "naics_description": _clean(raw.get("NAICS Description")),
        "description": _clean(raw.get("Description")),
        "period_start": _clean(
            raw.get("Period of Performance Start Date") or raw.get("Start Date")
        ),
        "period_end": _clean(
            raw.get("Period of Performance Current End Date") or raw.get("End Date")
        ),
    }


async def handle_lookup_us_contracts(
    company_name: str,
    max_results: int = 5,
    trace_id: Optional[str] = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    op_id = str(uuid.uuid4())

    # --- Input validation -------------------------------------------------
    if not company_name or not company_name.strip():
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=(
                "company_name is required -- provide the company or recipient name "
                "to search for in US federal contract awards."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    company_name = company_name.strip()
    max_results = max(1, min(int(max_results), 10))

    # --- Call USASpending.gov ---------------------------------------------
    source_url = _USASPENDING_SEARCH_URL
    awards: list[dict] = []
    upstream_error: Optional[str] = None

    try:
        raw_awards = await _search_awards(company_name, max_results)
        awards = [_parse_award(a) for a in raw_awards]
    except RuntimeError as exc:
        upstream_error = str(exc)

    lat = int((time.monotonic() - t0) * 1000)

    # --- Upstream unavailable --------------------------------------------
    if upstream_error is not None:
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.SUCCESS,
            reason_code="partial_lookup",
            human_message=(
                f"COULD NOT RETRIEVE contracts for '{_ascii(company_name)}': "
                f"USASpending.gov was unreachable on this call "
                f"({_ascii(upstream_error[:200])}). "
                f"This is NOT evidence that the company has no federal contracts -- "
                f"the data source did not respond. Retry, or check "
                f"https://usaspending.gov directly."
            ),
            result={
                "status": "unavailable",
                "queried_name": _ascii(company_name),
                "source": source_url,
                "error": _ascii(upstream_error[:300]),
            },
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=lat,
            retriable=True,
            trace_id=trace_id,
        )

    # --- No results -------------------------------------------------------
    if not awards:
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.SUCCESS,
            reason_code="not_found",
            human_message=(
                f"No US federal contract awards found for '{_ascii(company_name)}' "
                f"in the 2020-2026 window on USASpending.gov. "
                f"The company may not be a federal contractor, or the name may not "
                f"match the exact recipient name used in USASpending records "
                f"(try a shorter or alternate name)."
            ),
            result={
                "status": "not_found",
                "queried_name": _ascii(company_name),
                "source": source_url,
                "awards": [],
            },
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=lat,
            retriable=False,
            trace_id=trace_id,
        )

    # --- Found ------------------------------------------------------------
    total_awarded = sum(
        a["award_amount_usd"] for a in awards if a["award_amount_usd"] is not None
    )

    return OutcomeReceipt(
        operation_id=op_id,
        status=OperationStatus.SUCCESS,
        reason_code="found",
        human_message=(
            f"Found {len(awards)} US federal contract award(s) for "
            f"'{_ascii(company_name)}' on USASpending.gov "
            f"(showing top {len(awards)} by award amount, 2020-2026). "
            f"Total awarded in this result set: ${total_awarded:,.2f} USD."
        ),
        result={
            "status": "found",
            "queried_name": _ascii(company_name),
            "source": source_url,
            "total_in_result_set_usd": round(total_awarded, 2),
            "awards": awards,
        },
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=lat,
        retriable=False,
        trace_id=trace_id,
    )
