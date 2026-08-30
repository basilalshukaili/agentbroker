"""
map_trade_restriction -- free, read-only cross-border trade compliance snapshot.

PHILOSOPHY (founder directive): be the MIDDLEMAN.  Do NOT build a giant data
pipeline.  UNIFY EXISTING free/public authoritative sources into one clean call.
Keep it LEAN, zero human-in-the-loop, honest.

Data sources (no stored dataset; cite all sources in output):
  1. Hardcoded OFAC comprehensive-embargo country map (IR, KP, CU, SY + advisory
     notes for Crimea/DNR/LNR regions).  Source: OFAC Program Summaries at
     https://ofac.treasury.gov/countries.  Rarely changes; maintained here.
  2. Party screening via screen_sanctions, which covers OFAC SDN (US
     Treasury), the EU Consolidated list (European Commission) and the UK
     Sanctions List (FCDO). No API key. The UN list is NOT screened: it
     carries no open licence permitting commercial use.
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
        # CARRY THE UNAVAILABILITY FORWARD. screen_sanctions spends real care
        # building sources_unavailable and reason_code="partial_screening" -
        # it is how a caller tells "we screened and found nothing" from "we
        # could not screen this". Both were dropped on the floor here, and
        # `restricted` further down is computed from party_restrictions alone,
        # so every list being dark produced restricted=false and "No party
        # matches found".
        #
        # That defeats the entire defence built into the tool this calls, on a
        # compliance receipt, for the caller least able to notice.
        return {
            "party": _ascii(party_name),
            "matched": bool(res.get("matched", False)),
            "matches": res.get("matches", []),
            "sources_queried": res.get("sources_queried", []),
            "sources_unavailable": res.get("sources_unavailable", []),
            "possible_matches_unverified": res.get(
                "possible_matches_unverified", []),
            # The authoritative one-word answer from screen_sanctions:
            # hit / clean / candidates / not_screened. `matched` alone is
            # false both for "screened and clean" and for "nothing screened",
            # which is why that field exists - so pass it through rather than
            # making every caller re-derive it from sources_unavailable.
            "screening_status": res.get("screening_status"),
            "screening_complete": not res.get("sources_unavailable"),
            "reason_code": getattr(receipt, "reason_code", None),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "party": _ascii(party_name),
            "matched": False,
            "matches": [],
            "sources_queried": [],
            # NOT SCREENED, and it must say so in the same vocabulary as the
            # success path. Without these keys this branch produced a party
            # dict indistinguishable from a clean screen except for `error`,
            # which nothing downstream had to read.
            "screening_status": "not_screened",
            "possible_matches_unverified": [],
            "screening_complete": False,
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
      2. Party screening via screen_sanctions (OFAC SDN, the EU Consolidated
         list and the UK Sanctions List; no API key, no UN list).
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
                        # NO FALLBACK CITATION. This defaulted to
                        # opensanctions.org - a vendor we do not use - so any
                        # match arriving without a source_url was attributed,
                        # on a compliance receipt, to a source that had never
                        # seen it. A wrong citation on the document a customer
                        # relies on is worse than no citation: it is checkable,
                        # and it fails the check.
                        #
                        # Every match from screen_sanctions carries the URL of
                        # the authority that published the list. If one somehow
                        # does not, the honest output is None.
                        "source_url": (_ascii(match["source_url"])
                                       if match.get("source_url") else None),
                    })

    # --- 3. Build merged restrictions list ----------------------------------
    all_restrictions: list[dict] = dest_restrictions + party_restrictions
    restricted = dest_restricted or bool(party_restrictions)

    # WHICH PARTIES WERE ACTUALLY SCREENED. `restricted` is computed from
    # findings alone, so a party we could not screen at all contributes
    # nothing and the receipt reads "no party matches found" - the same
    # answer as a party we screened and cleared.
    party_screening_gaps: list[str] = []
    for pr in parties_screened:
        for u in pr.get("sources_unavailable") or []:
            party_screening_gaps.append(f"{pr.get('party')}: {u}")
        if pr.get("error"):
            party_screening_gaps.append(
                f"{pr.get('party')}: screening failed ({pr['error']})")
    parties_fully_screened = not party_screening_gaps

    # AND THE CANDIDATES WE DID FIND, which are the most actionable thing on
    # the receipt and were reported nowhere in the sentence.
    #
    # Screening the party "GRU" surfaces exact whole-name matches on the EU and
    # UK lists. They are not findings - the name is three characters, so
    # screen_sanctions demotes them on purpose - but this receipt said "No
    # party matches found", then described the screening GAP, and never
    # mentioned that two sanctions lists carry that exact name. A shipper
    # reading it learns that something was incomplete and not what was in it.
    party_candidates: list[str] = []
    for pr in parties_screened:
        n = len(pr.get("possible_matches_unverified") or [])
        if n:
            party_candidates.append(f"{pr.get('party')} ({n})")

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
        # A CLEAR IS ONLY A CLEAR IF EVERY PARTY WAS SCREENED.
        "parties_fully_screened": parties_fully_screened,
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
            # NOT AN UNCONDITIONAL "No party matches found". It was stated
            # flatly even when a party could not be screened, or when exact
            # name candidates HAD been found and demoted.
            + ("No confirmed party matches; unverified name candidate(s) for "
               f"{', '.join(party_candidates[:3])} - see "
               f"parties_screened[].possible_matches_unverified. "
               if party_candidates else
               "No party matches found on the screens that ran. "
               if parties_screened else "")
            + f"Review the restrictions[] field and consult a trade attorney for "
            f"your specific product and HS code."
        )
        reason_code = "advisory"
    else:
        # WE NEVER CLASSIFY THE PRODUCT, SO WE MUST NOT SAY "CLEAR".
        #
        # `product` is required, echoed into this sentence, and used nowhere
        # else in the entire module - the verdict comes from the destination
        # country and the party screen alone. That made this branch answer
        # `clear` / `restricted: false` to:
        #
        #     map_trade_restriction("uranium enrichment centrifuges", "DE")
        #     map_trade_restriction("night vision goggles", "DE")
        #
        # Both are export-controlled almost everywhere. An agent asking a tool
        # that advertises "export-control ... BIS Entity List, EU, UN, UK" and
        # being told "No significant trade restriction found" has been handed a
        # documented false assurance, which is worse than no answer: it is the
        # artefact someone points at afterwards. The seller of record is a named
        # legal entity.
        #
        # The fix is one enum value, not new capability. We genuinely did screen
        # the destination and the parties, and that result is worth having - so
        # report exactly that and name what was NOT done. `partial` also keeps
        # the honest-degradation vocabulary already used by screen_sanctions
        # rather than inventing a fourth word for the same idea.
        human_message = (
            f"PARTIAL: no restriction found from the checks we ran - destination "
            f"'{dest_clean}' (risk: {destination_risk}) and "
            f"{len(parties_screened)} party screening(s), "
            f"{sum(1 for p in parties_screened if p.get('matched'))} matched. "
            f"THE PRODUCT ITSELF WAS NOT CLASSIFIED: '"
            f"{_ascii(product_clean[:60])}' was not checked against any "
            f"export-control list, and dual-use or controlled goods can be "
            f"restricted to an otherwise unrestricted destination. This is not "
            f"an export-control clearance. Classify the item (HS/ECCN) against "
            f"BIS/EU/UK controls before shipping."
        )
        reason_code = "partial"

    # AND SAY IT WHEN A PARTY COULD NOT BE SCREENED AT ALL.
    #
    # This receipt used to drop screen_sanctions' sources_unavailable entirely,
    # so a party whose lists were ALL dark contributed nothing and read exactly
    # like a party that was screened and cleared. The sentence a human reads
    # said "No party matches found" either way.
    #
    # It applies to every branch, not just the partial one: a destination that
    # is merely advisory, with an unscreenable party, is not a cleaner result
    # than a restricted one.
    if party_screening_gaps:
        human_message += (
            f" WARNING: {len(party_screening_gaps)} party screening gap(s) - "
            f"these parties were NOT fully screened, so the absence of a match "
            f"for them means nothing: "
            + "; ".join(party_screening_gaps[:3])
        )
        if reason_code not in ("restricted",):
            reason_code = "partial"

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
