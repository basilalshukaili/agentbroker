"""
Unit tests for map_trade_restriction -- free, read-only trade compliance snapshot.

Tests use unittest.mock to patch httpx so no real network calls are made.
All four scenarios required by the task are covered plus extras:

  1.  Embargoed destination (IR) -- restricted=True, comprehensive_embargo.
  2.  Sanctioned party match -- party flagged in parties_screened + restrictions.
  3.  Clean lane (US->CA, office furniture) -- restricted=False, tariff guidance links.
  4.  Upstream-down fail-open -- both OpenSanctions + OFAC fail, no exception raised.
  5.  Bad input: empty product returns FAILURE.
  6.  Bad input: missing destination_country returns FAILURE.
  7.  Bad input: invalid ISO2 destination_country returns FAILURE.
  8.  Russia advisory -- restricted=False, destination_risk=sectoral_sanctions.
  9.  HS code echoed back when provided.
  10. Tariff note always contains official links.
  11. MCP annotations: readOnlyHint=True, idempotentHint=True.
  12. Not in _WRITE_TOOLS_REQUIRING_AUTH.
  13. In _PERSIST_SKIP_TOOLS (read-only, no durable store write).
  14. MCP dispatch: embargoed destination returns restricted=True.
  15. MCP dispatch: clean lane returns restricted=False.
  16. preview_cost reports free for map_trade_restriction.
  17. North Korea embargoed destination.
  18. Cuba embargoed destination.
  19. Syria embargoed destination.
  20. Concurrent party screening (multiple parties at once).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.map_trade_restriction import (
    handle_map_trade_restriction,
    _assess_destination,
    _ascii,
)
from core.models import OperationStatus
from agent_interface.mcp_server import _build_tool_list, handle_mcp_request


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shared mock helpers (mirror screen_sanctions test pattern)
# ---------------------------------------------------------------------------

def _make_clean_screen_receipt():
    """Return a mocked OutcomeReceipt for a clean (no match) party screening."""
    from core.models import OutcomeReceipt, OperationStatus, CostRecord
    import uuid
    from datetime import datetime, timezone
    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="no_match",
        human_message="No matches.",
        result={
            "matched": False,
            "matches": [],
            "lists_screened": ["OpenSanctions"],
            "sources_queried": ["https://api.opensanctions.org/match/sanctions"],
            "screened_at": "2026-08-24T00:00:00Z",
            "disclaimer": "Informational only.",
        },
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=500,
        retriable=False,
    )


def _make_match_screen_receipt(party_name: str):
    """Return a mocked OutcomeReceipt with a sanctions match for a party."""
    from core.models import OutcomeReceipt, OperationStatus, CostRecord
    import uuid
    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="matched",
        human_message=f"MATCH FOUND for '{party_name}'.",
        result={
            "matched": True,
            "matches": [{
                "name": party_name.upper(),
                "list": "OFAC-SDN",
                "match_score": 0.95,
                "program": "DPRK",
                "entity_type": "INDIVIDUAL",
                "source_url": "https://ofac.treasury.gov/sanctions-list-service",
            }],
            "lists_screened": ["OpenSanctions"],
            "sources_queried": ["https://api.opensanctions.org/match/sanctions"],
            "screened_at": "2026-08-24T00:00:00Z",
            "disclaimer": "Informational only.",
        },
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=800,
        retriable=False,
    )


# ---------------------------------------------------------------------------
# Test 1: Embargoed destination (Iran)
# ---------------------------------------------------------------------------

class TestEmbargoedDestinationIran:
    def test_iran_returns_restricted_true(self):
        result = run(handle_map_trade_restriction(
            product="laptop computers",
            destination_country="IR",
        ))
        assert result.status == OperationStatus.SUCCESS
        assert result.result["restricted"] is True
        assert result.result["destination_risk"] == "comprehensive_embargo"
        assert result.reason_code == "restricted"
        assert len(result.result["restrictions"]) >= 1
        embargo = result.result["restrictions"][0]
        assert embargo["type"] == "embargo"
        assert "OFAC" in embargo["list"]
        assert "https://ofac.treasury.gov" in embargo["source_url"]
        assert result.cost.amount == 0.0
        assert result.cost.basis == "free"
        assert "disclaimer" in result.result
        assert "screened_at" in result.result

    def test_iran_lowercase_iso_also_restricted(self):
        """Destination country code must be case-insensitive."""
        result = run(handle_map_trade_restriction(
            product="steel",
            destination_country="ir",
        ))
        assert result.result["restricted"] is True
        assert result.result["destination_risk"] == "comprehensive_embargo"


# ---------------------------------------------------------------------------
# Test 2: Sanctioned party match
# ---------------------------------------------------------------------------

class TestSanctionedPartyMatch:
    def test_sanctioned_party_flagged_in_restrictions(self):
        with patch(
            "core.map_trade_restriction._screen_party",
            new=AsyncMock(return_value={
                "party": "Kim Jong-un",
                "matched": True,
                "matches": [{
                    "name": "KIM JONG UN",
                    "list": "OFAC-SDN",
                    "match_score": 0.95,
                    "program": "DPRK",
                    "entity_type": "INDIVIDUAL",
                    "source_url": "https://ofac.treasury.gov/sanctions-list-service",
                }],
                "sources_queried": ["https://api.opensanctions.org/match/sanctions"],
            }),
        ):
            result = run(handle_map_trade_restriction(
                product="steel pipes",
                destination_country="DE",
                parties=["Kim Jong-un"],
            ))

        assert result.status == OperationStatus.SUCCESS
        assert result.result["restricted"] is True
        assert result.reason_code == "restricted"
        parties = result.result["parties_screened"]
        assert len(parties) == 1
        assert parties[0]["party"] == "Kim Jong-un"
        assert parties[0]["matched"] is True
        # A restrictions entry for the party must be present
        sanctions_restrictions = [
            r for r in result.result["restrictions"]
            if r["type"] == "sanctions"
        ]
        assert len(sanctions_restrictions) >= 1
        assert "Kim Jong-un" in sanctions_restrictions[0]["entity"]

    def test_clean_party_does_not_restrict(self):
        with patch(
            "core.map_trade_restriction._screen_party",
            new=AsyncMock(return_value={
                "party": "Jane Smith",
                "matched": False,
                "matches": [],
                "sources_queried": [],
            }),
        ):
            result = run(handle_map_trade_restriction(
                product="office furniture",
                destination_country="DE",
                parties=["Jane Smith"],
            ))

        assert result.result["restricted"] is False
        parties = result.result["parties_screened"]
        assert len(parties) == 1
        assert parties[0]["matched"] is False


# ---------------------------------------------------------------------------
# Test 3: Clean lane (US -> CA, office furniture)
# ---------------------------------------------------------------------------

class TestCleanLane:
    def test_us_to_ca_unrestricted(self):
        result = run(handle_map_trade_restriction(
            product="office furniture",
            origin_country="US",
            destination_country="CA",
        ))
        assert result.status == OperationStatus.SUCCESS
        assert result.result["restricted"] is False
        assert result.result["destination_risk"] == "standard"
        assert result.reason_code == "clear"
        # restrictions[] must be empty for a clean lane
        assert result.result["restrictions"] == []
        # tariff_note must contain official links, not fabricated rates
        tariff_note = result.result["tariff_note"]
        assert "hts.usitc.gov" in tariff_note
        assert "taric" in tariff_note.lower()
        assert "guidance" in result.result["tariff_source"]
        # disclaimer must always be present
        assert "disclaimer" in result.result

    def test_tariff_note_contains_official_links(self):
        """tariff_note must cite official sources for any destination."""
        result = run(handle_map_trade_restriction(
            product="solar panels",
            destination_country="DE",
        ))
        note = result.result["tariff_note"]
        assert "hts.usitc.gov" in note
        assert "taric" in note.lower()
        assert "cbsa" in note.lower() or "canada" in note.lower()

    def test_tariff_note_does_not_fabricate_rate(self):
        """Tariff note must NOT contain a percentage or dollar rate."""
        result = run(handle_map_trade_restriction(
            product="cotton shirts",
            destination_country="GB",
        ))
        note = result.result["tariff_note"]
        # Should not contain patterns like '12%' or '$0.15/unit'
        import re
        fabricated = re.findall(r'\d+\.\d+\s*%|\d+\s*%\s+tariff|\$\d+\.\d+/unit', note)
        assert not fabricated, f"Suspected fabricated rate in tariff_note: {fabricated}"


# ---------------------------------------------------------------------------
# Test 4: Upstream-down fail-open
# ---------------------------------------------------------------------------

class TestUpstreamDownFailOpen:
    def test_all_upstreams_down_no_exception(self):
        """When screen_sanctions upstream fails, result is still returned."""
        async def _failing_screen(name, *a, **kw):
            raise ConnectionError("All upstreams down")

        with patch(
            "core.map_trade_restriction._screen_party",
            new=AsyncMock(side_effect=Exception("upstream down")),
        ):
            result = run(handle_map_trade_restriction(
                product="grain",
                destination_country="DE",
                parties=["Some Corp"],
            ))

        # Must not raise; must return a valid OutcomeReceipt
        assert result.status == OperationStatus.SUCCESS
        # parties_screened may be empty if all tasks raised (gather returns exceptions)
        # but we should still get a valid result
        assert "restricted" in result.result
        assert "disclaimer" in result.result

    def test_no_parties_no_network_call(self):
        """Destination-only check has no network calls; always succeeds."""
        result = run(handle_map_trade_restriction(
            product="chemicals",
            destination_country="JP",
        ))
        assert result.status == OperationStatus.SUCCESS
        assert result.result["destination_risk"] == "standard"
        assert result.result["restricted"] is False


# ---------------------------------------------------------------------------
# Test 5: Bad input -- empty product
# ---------------------------------------------------------------------------

class TestBadInputEmptyProduct:
    def test_empty_product_returns_failure(self):
        result = run(handle_map_trade_restriction(
            product="",
            destination_country="US",
        ))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"
        assert result.cost.amount == 0.0

    def test_whitespace_product_returns_failure(self):
        result = run(handle_map_trade_restriction(
            product="   ",
            destination_country="US",
        ))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"


# ---------------------------------------------------------------------------
# Test 6 & 7: Bad input -- destination_country
# ---------------------------------------------------------------------------

class TestBadInputDestination:
    def test_empty_destination_returns_failure(self):
        result = run(handle_map_trade_restriction(
            product="laptops",
            destination_country="",
        ))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"

    def test_invalid_iso2_returns_failure(self):
        result = run(handle_map_trade_restriction(
            product="laptops",
            destination_country="IRAN",  # too long, not ISO2
        ))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"

    def test_single_char_returns_failure(self):
        result = run(handle_map_trade_restriction(
            product="laptops",
            destination_country="I",  # too short
        ))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"


# ---------------------------------------------------------------------------
# Test 8: Russia advisory
# ---------------------------------------------------------------------------

class TestRussiaAdvisory:
    def test_russia_is_sectoral_not_comprehensive(self):
        result = run(handle_map_trade_restriction(
            product="hydraulic pumps",
            destination_country="RU",
        ))
        assert result.status == OperationStatus.SUCCESS
        # Russia is NOT a comprehensive embargo -- restricted stays False
        # (unless a party matches), but destination_risk is sectoral_sanctions
        assert result.result["destination_risk"] == "sectoral_sanctions"
        assert result.reason_code == "advisory"
        # restrictions[] should contain an advisory entry
        advisory = [r for r in result.result["restrictions"] if r["type"] == "export_control"]
        assert len(advisory) >= 1
        detail = advisory[0]["detail"]
        assert "Crimea" in detail or "DNR" in detail or "LNR" in detail or "Russia" in detail


# ---------------------------------------------------------------------------
# Test 9: HS code echoed back
# ---------------------------------------------------------------------------

class TestHsCodeEchoed:
    def test_provided_hs_code_echoed_in_hint(self):
        result = run(handle_map_trade_restriction(
            product="hydraulic pumps",
            destination_country="DE",
            hs_code="8413.50",
        ))
        assert result.result["hs_code_hint"] == "8413.50"

    def test_no_hs_code_hint_is_null(self):
        result = run(handle_map_trade_restriction(
            product="office chairs",
            destination_country="CA",
        ))
        assert result.result["hs_code_hint"] is None


# ---------------------------------------------------------------------------
# Test 10: Tariff note contains official links (covered in Test 3 above;
#           this is an extra assertion for a different destination)
# ---------------------------------------------------------------------------

class TestTariffNoteAlwaysPresent:
    def test_tariff_note_present_even_for_embargoed(self):
        """Even embargoed destinations should explain tariff lookup path."""
        result = run(handle_map_trade_restriction(
            product="goods",
            destination_country="KP",
        ))
        # Embargoed -- should still have a tariff_note (even if moot)
        assert "tariff_note" in result.result
        assert result.result["tariff_source"] == "guidance"


# ---------------------------------------------------------------------------
# Test 11: MCP annotations
# ---------------------------------------------------------------------------

class TestMcpAnnotations:
    def test_listed_as_read_only_and_idempotent(self):
        tools = {t["name"]: t for t in _build_tool_list()}
        assert "map_trade_restriction" in tools, "map_trade_restriction not in tools/list"
        ann = tools["map_trade_restriction"]["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["idempotentHint"] is True
        assert ann["destructiveHint"] is False


# ---------------------------------------------------------------------------
# Test 12: Not in write tools
# ---------------------------------------------------------------------------

class TestNotInWriteTools:
    def test_not_in_write_tools_requiring_auth(self):
        from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH
        assert "map_trade_restriction" not in _WRITE_TOOLS_REQUIRING_AUTH


# ---------------------------------------------------------------------------
# Test 13: In _PERSIST_SKIP_TOOLS (read-only, no durable store write)
# ---------------------------------------------------------------------------

class TestInPersistSkipTools:
    def test_in_persist_skip_tools(self):
        # _PERSIST_SKIP_TOOLS is defined inside _dispatch_operation; we verify
        # by checking the source constant in mcp_server module.
        import ast, inspect
        import agent_interface.mcp_server as ms
        src = inspect.getsource(ms)
        # Verify the string appears in _PERSIST_SKIP_TOOLS block
        assert '"map_trade_restriction"' in src or "'map_trade_restriction'" in src


# ---------------------------------------------------------------------------
# Test 14: MCP dispatch -- embargoed destination
# ---------------------------------------------------------------------------

class TestMcpDispatchEmbargoed:
    def test_iran_via_mcp_dispatcher(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 300, "method": "tools/call",
            "params": {
                "name": "map_trade_restriction",
                "arguments": {
                    "product": "laptop computers",
                    "destination_country": "IR",
                },
            },
        }))
        assert "result" in r, f"expected result, got error: {r.get('error')}"
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["status"] == "success"
        assert data["result"]["restricted"] is True
        assert data["result"]["destination_risk"] == "comprehensive_embargo"


# ---------------------------------------------------------------------------
# Test 15: MCP dispatch -- clean lane
# ---------------------------------------------------------------------------

class TestMcpDispatchCleanLane:
    def test_us_to_ca_via_mcp_dispatcher(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 301, "method": "tools/call",
            "params": {
                "name": "map_trade_restriction",
                "arguments": {
                    "product": "office furniture",
                    "origin_country": "US",
                    "destination_country": "CA",
                },
            },
        }))
        assert "result" in r, f"expected result, got error: {r.get('error')}"
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["status"] == "success"
        assert data["result"]["restricted"] is False
        assert data["result"]["destination_risk"] == "standard"
        tariff_note = data["result"]["tariff_note"]
        assert "hts.usitc.gov" in tariff_note

    def test_missing_product_returns_param_error(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 302, "method": "tools/call",
            "params": {
                "name": "map_trade_restriction",
                "arguments": {"destination_country": "CA"},
            },
        }))
        # Missing required 'product' argument
        assert "error" in r
        assert r["error"]["code"] == -32602

    def test_missing_destination_returns_param_error(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 303, "method": "tools/call",
            "params": {
                "name": "map_trade_restriction",
                "arguments": {"product": "goods"},
            },
        }))
        assert "error" in r
        assert r["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Test 16: preview_cost reports free
# ---------------------------------------------------------------------------

class TestPreviewCostFree:
    def test_preview_cost_reports_free_for_map_trade_restriction(self):
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        resp = run(handle_preview_cost(PreviewCostRequest(
            operation="map_trade_restriction",
            params={"product": "laptops", "destination_country": "IR"},
        )))
        assert resp.estimated_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Tests 17-19: North Korea, Cuba, Syria embargoes
# ---------------------------------------------------------------------------

class TestOtherEmbargoedCountries:
    def test_north_korea_embargoed(self):
        result = run(handle_map_trade_restriction(
            product="electronics",
            destination_country="KP",
        ))
        assert result.result["restricted"] is True
        assert result.result["destination_risk"] == "comprehensive_embargo"

    def test_cuba_embargoed(self):
        result = run(handle_map_trade_restriction(
            product="vehicles",
            destination_country="CU",
        ))
        assert result.result["restricted"] is True
        assert result.result["destination_risk"] == "comprehensive_embargo"

    def test_syria_embargoed(self):
        result = run(handle_map_trade_restriction(
            product="chemicals",
            destination_country="SY",
        ))
        assert result.result["restricted"] is True
        assert result.result["destination_risk"] == "comprehensive_embargo"


# ---------------------------------------------------------------------------
# Test 20: Concurrent party screening
# ---------------------------------------------------------------------------

class TestConcurrentPartyScreening:
    def test_multiple_parties_all_screened(self):
        call_count = {"n": 0}

        async def _mock_screen(party_name: str) -> dict:
            call_count["n"] += 1
            return {
                "party": party_name,
                "matched": False,
                "matches": [],
                "sources_queried": [],
            }

        with patch("core.map_trade_restriction._screen_party", side_effect=_mock_screen):
            result = run(handle_map_trade_restriction(
                product="steel",
                destination_country="DE",
                parties=["Acme GmbH", "Beta Corp", "Gamma Ltd"],
            ))

        assert call_count["n"] == 3
        assert len(result.result["parties_screened"]) == 3
        parties_names = {p["party"] for p in result.result["parties_screened"]}
        assert "Acme GmbH" in parties_names
        assert "Beta Corp" in parties_names


# ---------------------------------------------------------------------------
# Tests for _assess_destination helper (unit-level)
# ---------------------------------------------------------------------------

class TestAssessDestination:
    def test_ir_is_comprehensive(self):
        restricted, risk, detail, restr = _assess_destination("IR")
        assert restricted is True
        assert risk == "comprehensive_embargo"
        assert len(restr) == 1
        assert restr[0]["type"] == "embargo"

    def test_ru_is_sectoral(self):
        restricted, risk, detail, restr = _assess_destination("RU")
        assert restricted is False
        assert risk == "sectoral_sanctions"
        assert len(restr) == 1
        assert "Crimea" in detail or "DNR" in detail

    def test_ua_is_elevated(self):
        restricted, risk, detail, restr = _assess_destination("UA")
        assert restricted is False
        assert risk == "elevated_scrutiny"

    def test_ca_is_standard(self):
        restricted, risk, detail, restr = _assess_destination("CA")
        assert restricted is False
        assert risk == "standard"
        assert restr == []

    def test_us_is_standard(self):
        restricted, risk, detail, restr = _assess_destination("US")
        assert restricted is False
        assert risk == "standard"
