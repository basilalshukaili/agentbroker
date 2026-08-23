"""
map_trade_restriction -- free, read-only cross-border trade compliance snapshot.

PHILOSOPHY (founder directive): be the MIDDLEMAN.  Do NOT build a giant data
pipeline.  UNIFY EXISTING free/public authoritative sources into one clean call.
Keep it LEAN, zero human-in-the-loop, honest.

Data sources (no stored dataset; cite all sources in output):
  1. Hardcoded OFAC comprehensive-embargo country map (IR, KP, CU, SY + advisory
     notes for Crimea/DNR/LNR regions).  Source: OFAC Program Summaries at
     https://ofac.treasury.gov/countries.  Rarely changes; maintained here.
  2. Party screening via OpenSanctions + OFAC SDN (reused from screen_sanctions):
     covers OFAC SDN, BIS Entity List, EU/UN consolidated lists, 40+ official
     watchlists.  FREE API key required for OpenSanctions (set OPENSANCTIONS_API_KEY);
     OFAC SDN bulk CSV is always free with no key.
  3. Tariff / HS guidance: returns official links only (USITC HTS, EU TARIC,
     Canada CBSA).  Rates are NOT provided here to avoid misinformation -- the
     caller must consult the official authority before any commercial decision.

Design:
  * 10-second timeout per upstream; fail-open to partial results.
  * Never fabricates a tariff rate, a clear, or a restricted status.
  * Honest "consult official source" disclaimer on every response.
  * All string output is ASCII-safe (non-ASCII chars replaced with '?').
  * Cost: 0.00 USD (free read tool; demand probe for compliance positioning).
  * Telemetry: fires via the existing mcp_server dispatch hook (usage_events row).

Input:
  product             str, required  -- product description or name
  hs_code             str, optional  -- Harmonized System code (e.g. '8471.30')
  origin_country      str, optional  -- ISO 3166-1 alpha-2 (e.g. 'US')
  destination_country str, required  -- ISO 3166-1 alpha-2 (e.g. 'IR')
  parties             list[str], opt -- names/entities to screen (exporters,
                                        importers, freight forwarders, end-users)

Output (structured, honest):
  restricted          bool
  restrictions        list[{type, list, entity, detail, source_url}]
  hs_code_hint        str or None
  destination_risk    str  -- comprehensive_embargo|sectoral_sanctions|
                              elevated_scrutiny|standard
  tariff_note         str  -- guidance + official links, NEVER a fabricated rate
  tariff_source       str  -- always 'guidance' (we do not provide live rates)
  parties_screened    list[{party, matched, matches, sources_queried}]
  sources_queried     list[str]
  screened_at         str  -- ISO 8601 UTC
  disclaimer          str
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.models import CostRecord, OperationStatus, OutcomeReceipt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "Informational trade-compliance snapshot only, not legal advice. "
    "Export regulations and sanctions change frequently. "
    "Consult a licensed trade attorney or export compliance specialist before "
    "making commercial decisions. Negative results do not guarantee the "
    "shipment is unrestricted on lists or under regulations not queried here."
)

# Authoritative OFAC comprehensively-embargoed countries.
# Source: https://ofac.treasury.gov/countries
# These are countries where virtually ALL transactions require a licence or are
# prohibited -- i.e. a 'comprehensive' programme, not just targeted/sectoral.
_COMPREHENSIVE_EMBARGOES: dict[str, str] = {
    "IR": (
        "Iran -- OFAC comprehensive sanctions programme (Iranian Transactions "
        "and Sanctions Regulations, 31 CFR 560). Virtually all exports, "
        "re-exports, sales, or supplies to Iran require an OFAC licence."
    ),
    "KP": (
        "North Korea / DPRK -- OFAC comprehensive sanctions programme "
        "(North Korea Sanctions Regulations, 31 CFR 510). "
        "Essentially all transactions prohibited."
    ),
    "CU": (
        "Cuba -- OFAC comprehensive sanctions programme "
        "(Cuban Assets Control Regulations, 31 CFR 515). "
        "Nearly all transactions prohibited; limited licence exceptions exist."
    ),
    "SY": (
        "Syria -- OFAC comprehensive sanctions programme "
        "(Syrian Sanctions Regulations, 31 CFR 542). "
        "Most transactions prohibited."
    ),
}

# Advisory notes for countries/regions with significant export controls or
# sectoral sanctions -- NOT comprehensive embargoes, but high-risk for many
# product categories.  Includes the Crimea/DNR/LNR regional bans.
_SECTORAL_ADVISORY: dict[str, dict] = {
    "RU": {
        "destination_risk": "sectoral_sanctions",
        "detail": (
            "Russia -- extensive US/EU export controls and sectoral sanctions. "
            "BIS 'Russia-Ukraine' rules (EAR Part 746) restrict most dual-use, "
            "advanced-technology, and luxury goods. OFAC EO 14024 and EU "
            "Council Regulation 833/2014 impose broad sectoral restrictions. "
            "Additionally, the Crimea, Donetsk People's Republic (DNR), and "
            "Luhansk People's Republic (LNR) regions of Ukraine are subject "
            "to OFAC comprehensive prohibitions under 31 CFR 589 / EO 13685. "
            "Most commercial exports from the US/EU to Russia require a "
            "licence or are prohibited outright. Consult BIS and OFAC."
        ),
        "source_url": "https://ofac.treasury.gov/sanctions-programs-and-country-information/ukraine-russia-related-sanctions",
    },
    "BY": {
        "destination_risk": "sectoral_sanctions",
        "detail": (
            "Belarus -- significant US and EU sanctions (OFAC Belarus programme, "
            "EU Council Regulation 765/2006). Restrictions on financial sector, "
            "dual-use goods, and luxury items. Not a comprehensive embargo but "
            "licence requirements apply to many export categories."
        ),
        "source_url": "https://ofac.treasury.gov/sanctions-programs-and-country-information/belarus-sanctions",
    },
    "MM": {
        "destination_risk": "elevated_scrutiny",
        "detail": (
            "Myanmar -- OFAC targeted sanctions on defence/military entities "
            "and key government officials (EO 14014). Not a comprehensive "
            "embargo; most civilian trade is permitted but enhanced due "
            "diligence required for any defence or government-linked counterparty."
        ),
        "source_url": "https://ofac.treasury.gov/sanctions-programs-and-country-information/burma-sanctions",
    },
    "VE": {
        "destination_risk": "elevated_scrutiny",
        "detail": (
            "Venezuela -- OFAC sectoral sanctions on oil/gold sector and "
            "government debt (EO 13808/13827). Not a comprehensive embargo; "
            "many transactions permitted with proper due diligence."
        ),
        "source_url": "https://ofac.treasury.gov/sanctions-programs-and-country-information/venezuela-sanctions",
    },
    "CN": {
        "destination_risk": "elevated_scrutiny",
        "detail": (
            "China -- significant BIS export controls on advanced semiconductors, "
            "AI chips, supercomputers, and related items (EAR 'Chips and Science Act' "
            "rules, October 2022 / October 2023 amendments). "
            "Verify against BIS Entity List. Most commercial goods unrestricted."
        ),
        "source_url": "https://www.bis.doc.gov/index.php/policy-guidance/country-guidance/sanctioned-destinations",
    },
    "UA": {
        "destination_risk": "elevated_scrutiny",
        "detail": (
            "Ukraine -- generally open for trade. HOWEVER: the Crimea region, "
            "the Donetsk People's Republic (DNR), and the Luhansk People's "
            "Republic (LNR) regions are subject to OFAC comprehensive prohibitions "
            "under 31 CFR 589 / EO 13685. Ensure the final destination is NOT "
            "within those occupied territories."
        ),
        "source_url": "https://ofac.treasury.gov/sanctions-programs-and-country-information/ukraine-russia-related-sanctions",
    },
}

# Official tariff lookup sources -- always return these links, never fabricate rates.
_TARIFF_LINKS = (
    "US imports: USITC Harmonized Tariff Schedule at https://hts.usitc.gov/ "
    "| EU imports: EU TARIC database at "
    "https://ec.europa.eu/taxation_customs/dds2/taric/taric_consultation.jsp "
    "| Canada imports: Canada Border Services tariff at "
    "https://www.cbsa-asfc.gc.ca/trade-commerce/tariff-tarif/menu-eng.html "
    "| UK imports: Global Tariff Tool at "
    "https://www.trade-tariff.service.gov.uk/"
)

_TIMEOUT = 10  # seconds per upstream (inherited from screen_sanctions)


# ---------------------------------------------------------------------------
# ASCII helper (same as in screen_sanctions / verify_company_record)
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


# ---------------------------------------------------------------------------
# Destination risk assessment (hardcoded, authoritative)
# ---------------------------------------------------------------------------

def _assess_destination(
    destination_country: str,
) -> tuple[bool, str, str, list[dict]]:
    """
    Return (is_embargoed, destination_risk, risk_detail, restrictions_list).
    Uses ONLY the hardcoded authoritative maps -- no network call.
    """
    dest_upper = destination_country.upper()

    # Comprehensive embargo
    if dest_upper in _COMPREHENSIVE_EMBARGOES:
        detail = _COMPREHENSIVE_EMBARGOES[dest_upper]
        return (
            True,
            "comprehensive_embargo",
            detail,
            [{
                "type": "embargo",
                "list": "OFAC-Comprehensive-Sanctions-Programme",
                "entity": None,
                "detail": _ascii(detail),
                "source_url": "https://ofac.treasury.gov/countries",
            }],
        )

    # Sectoral / elevated advisory
    if dest_upper in _SECTORAL_ADVISORY:
        entry = _SECTORAL_ADVISORY[dest_upper]
        detail = entry["detail"]
        risk = entry["destination_risk"]
        return (
            False,  # not restricted by destination alone (not comprehensive)
            risk,
            detail,
            [{
                "type": "export_control",
                "list": "Advisory",
                "entity": None,
                "detail": _ascii(detail),
                "source_url": entry["source_url"],
            }],
        )

    return False, "standard", "", []


# ---------------------------------------------------------------------------
# Party screening (reuses screen_sanctions logic)
# ---------------------------------------------------------------------------

async def _screen_party(party_name: str) -> dict:
    """
    Screen a single party name against sanctions lists.
    Returns a dict: {party, matched, matches, sources_queried, error}.
    Fail-open: errors are captured and returned, never raised.
    """
    try:
        from core.screen_sanctions import handle_screen_sanctions
        receipt = await handle_screen_sanctions(name=party_name)
        res = receipt.result or {}
        return {
            "party": _ascii(party_name),
            "matched": bool(res.get("matched", False)),
            "matches": res.get("matches", []),
            "sources_queried": res.get("sources_queried", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "party": _ascii(party_name),
            "matched": False,
            "matches": [],
            "sources_queried": [],
            "error": _ascii(str(exc)[:120]),
        }


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle_map_trade_restriction(
    product: str,
    destination_country: str,
    hs_code: Optional[str] = None,
    origin_country: Optional[str] = None,
    parties: Optional[list] = None,
    trace_id: Optional[str] = None,
) -> OutcomeReceipt:
    """
    Return a consolidated trade-compliance snapshot for a given product +
    origin/destination country pair.

    Queries:
      1. Hardcoded OFAC comprehensive-embargo map (no network, authoritative).
      2. Party screening via OpenSanctions + OFAC SDN (reused from
         screen_sanctions; covers OFAC SDN, BIS Entity List, EU/UN, 40+ lists).
      3. Tariff guidance links (no network, official sources cited).

    Never fabricates a tariff rate, a clear, or a restricted status.
    """
    t0 = time.monotonic()
    op_id = str(uuid.uuid4())
    screened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Input validation ---------------------------------------------------
    product_clean = product.strip() if product else ""
    if not product_clean:
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message="product is required -- provide a product name or description.",
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    dest_clean = destination_country.strip().upper() if destination_country else ""
    if not dest_clean or len(dest_clean) != 2:
        return OutcomeReceipt(
            operation_id=op_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=(
                "destination_country is required and must be an ISO 3166-1 "
                "alpha-2 code (e.g. 'US', 'IR', 'DE')."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    origin_clean = origin_country.strip().upper() if origin_country else None
    hs_code_clean = hs_code.strip() if hs_code else None
    parties_list: list[str] = [p for p in (parties or []) if p and str(p).strip()]

    # --- 1. Destination risk assessment (hardcoded, no network) -------------
    (
        dest_restricted,
        destination_risk,
        dest_detail,
        dest_restrictions,
    ) = _assess_destination(dest_clean)

    all_sources_queried: list[str] = [
        "OFAC-comprehensive-embargo-map (hardcoded; source: https://ofac.treasury.gov/countries)"
    ]

    # --- 2. Party screening (concurrent, network) ----------------------------
    parties_screened: list[dict] = []
    party_restrictions: list[dict] = []

    if parties_list:
        # Run all screenings concurrently; fail-open per party
        tasks = [asyncio.create_task(_screen_party(p)) for p in parties_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for party_result in results:
            if isinstance(party_result, Exception):
                # _screen_party itself is fail-open, but guard here too
                continue
            parties_screened.append(party_result)
            all_sources_queried.extend(
                s for s in party_result.get("sources_queried", [])
                if s not in all_sources_queried
            )
            # Promote party matches to restrictions
            if party_result.get("matched"):
                for match in party_result.get("matches", []):
                    party_restrictions.append({
                        "type": "sanctions",
                        "list": _ascii(match.get("list", "Unknown")),
                        "entity": _ascii(party_result["party"]),
                        "detail": (
                            f"Party '{_ascii(party_result['party'])}' matched "
                            f"'{_ascii(match.get('name', ''))}' on "
                            f"{_ascii(match.get('list', ''))} "
                            f"(score={match.get('match_score', 0):.2f}"
                            + (
                                f", program={_ascii(match['program'])}"
                                if match.get("program")
                                else ""
                            )
                            + ")"
                        ),
                        "source_url": _ascii(
                            match.get("source_url", "https://www.opensanctions.org/")
                        ),
                    })

    # --- 3. Build merged restrictions list ----------------------------------
    all_restrictions: list[dict] = dest_restrictions + party_restrictions
    restricted = dest_restricted or bool(party_restrictions)

    # --- 4. HS code hint ----------------------------------------------------
    # We do NOT derive HS codes from text (would risk fabrication).
    # If the caller provided hs_code, echo it (caller's responsibility).
    # If not provided, return None with guidance.
    hs_code_hint: Optional[str] = hs_code_clean  # None when not provided

    # --- 5. Tariff note (honest guidance, no fabricated rates) --------------
    origin_note = (
        f" from {origin_clean}" if origin_clean else ""
    )
    hs_note = (
        f" for HS code {hs_code_clean}" if hs_code_clean else ""
    )
    tariff_note = (
        f"Tariff rates for '{_ascii(product_clean[:60])}'{origin_note} "
        f"to {dest_clean}{hs_note} are NOT provided here to avoid "
        "misinformation -- rates vary by HS code, trade agreement status, "
        "rules of origin, and customs valuation. "
        "Consult the official tariff databases: "
        + _TARIFF_LINKS
    )

    # --- 6. Build output payload --------------------------------------------
    lat = int((time.monotonic() - t0) * 1000)

    result_payload: dict = {
        "restricted": restricted,
        "restrictions": all_restrictions,
        "hs_code_hint": hs_code_hint,
        "destination_risk": destination_risk,
        "tariff_note": tariff_note,
        "tariff_source": "guidance",
        "parties_screened": parties_screened,
        "sources_queried": all_sources_queried,
        "screened_at": screened_at,
        "disclaimer": _DISCLAIMER,
    }

    # --- 7. Human-readable summary ------------------------------------------
    if restricted and dest_restricted:
        human_message = (
            f"RESTRICTED: destination country '{dest_clean}' is under a "
            f"comprehensive OFAC embargo -- virtually all trade requires a "
            f"licence or is prohibited. "
            f"Destination risk: {destination_risk}. "
            f"Consult OFAC and a licensed trade attorney before proceeding."
        )
        reason_code = "restricted"
    elif restricted and party_restrictions:
        party_names = list({r["entity"] for r in party_restrictions if r.get("entity")})
        human_message = (
            f"RESTRICTED: {len(party_restrictions)} party match(es) found on "
            f"official sanctions lists. "
            f"Matched parties: {', '.join(party_names[:3])}. "
            f"Destination risk: {destination_risk}. "
            "Halt the transaction and seek legal counsel."
        )
        reason_code = "restricted"
    elif destination_risk in ("sectoral_sanctions", "elevated_scrutiny"):
        human_message = (
            f"ADVISORY: '{dest_clean}' is not comprehensively embargoed but "
            f"carries significant restrictions ({destination_risk}). "
            f"No party matches found. "
            f"Review the restrictions[] field and consult a trade attorney for "
            f"your specific product and HS code."
        )
        reason_code = "advisory"
    else:
        human_message = (
            f"No significant trade restriction found for "
            f"'{_ascii(product_clean[:60])}' to {dest_clean}. "
            f"Destination risk: {destination_risk}. "
            f"Party screenings: {len(parties_screened)} screened, "
            f"{sum(1 for p in parties_screened if p.get('matched'))} matched. "
            "Always verify with official sources before commercial decisions."
        )
        reason_code = "clear"

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
