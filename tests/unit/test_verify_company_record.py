"""
Unit tests for verify_company_record -- free, read-only company registry probe.

Tests use unittest.mock to patch httpx so no real network calls are made.
Covers:
  1. Happy path: GLEIF returns a match.
  2. Happy path: direct LEI lookup.
  3. Not-found path: both upstreams return empty/404.
  4. EDGAR enrichment: US company found in EDGAR after GLEIF hit.
  5. Bad input: empty name returns FAILURE.
  6. Fail-open: upstream timeout still returns a result (not_found, not exception).
  7. ASCII-only output: non-ASCII chars in registry data are replaced with '?'.
  8. MCP annotations: listed as readOnlyHint=True, idempotentHint=True.
  9. MCP tools/call: callable via the MCP dispatcher.
  10. cost is 0.00 free for preview_cost.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.verify_company_record import handle_verify_company_record, _ascii
from core.models import OperationStatus
from agent_interface.mcp_server import _build_tool_list, handle_mcp_request


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shared mock responses
# ---------------------------------------------------------------------------

_GLEIF_ENTITY = {
    "id": "HWUPKR0MPOU8FGXBT394",
    "attributes": {
        "lei": "HWUPKR0MPOU8FGXBT394",
        "entity": {
            "legalName": {"name": "Apple Inc."},
            "status": "ACTIVE",
            "jurisdiction": "US",
            "legalAddress": {
                "addressLines": ["One Apple Park Way"],
                "city": "Cupertino",
                "postalCode": "95014",
                "country": "US",
            },
        },
        "registration": {
            "status": "ISSUED",
            "nextRenewalDate": "2027-01-01T00:00:00Z",
            "managingLou": "GLEIF LEI Foundation",
        },
    },
}

_GLEIF_SEARCH_RESPONSE = {"data": [_GLEIF_ENTITY]}
_GLEIF_EMPTY_RESPONSE = {"data": []}

_EDGAR_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc"},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}


def _mock_gleif_search_hit():
    """httpx response mock returning GLEIF search results."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _GLEIF_SEARCH_RESPONSE
    return resp


def _mock_gleif_lei_hit():
    """httpx response mock returning a direct LEI record."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": _GLEIF_ENTITY}
    return resp


def _mock_gleif_empty():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _GLEIF_EMPTY_RESPONSE
    return resp


def _mock_gleif_404():
    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {}
    return resp


def _mock_edgar_hit():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _EDGAR_TICKERS
    return resp


def _mock_edgar_empty():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    return resp


# ---------------------------------------------------------------------------
# Test 1: GLEIF name search returns a match
# ---------------------------------------------------------------------------

class TestGleifNameSearch:
    def test_found_via_gleif_name(self):
        with patch("httpx.AsyncClient") as mock_cls:
            async def side_effect(resp):
                return resp

            client_instance = AsyncMock()
            client_instance.get = AsyncMock(return_value=_mock_gleif_search_hit())
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client_instance

            # Also mock EDGAR to return empty (no US enrichment needed for this test)
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(name="Apple Inc", country="US"))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "found"
        assert result.result["status"] == "found"
        assert result.result["lei"] == "HWUPKR0MPOU8FGXBT394"
        assert result.result["legal_name"] == "Apple Inc."
        assert result.result["entity_status"] == "ACTIVE"
        assert result.result["jurisdiction"] == "US"
        assert result.cost.basis == "free"
        assert result.cost.amount == 0.0
        assert "GLEIF" in result.result["sources"]


# ---------------------------------------------------------------------------
# Test 2: Direct LEI lookup
# ---------------------------------------------------------------------------

class TestDirectLeiLookup:
    def test_lei_lookup_returns_record(self):
        with patch("core.verify_company_record._gleif_by_lei", new=AsyncMock(return_value=_GLEIF_ENTITY)):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(
                    name="Apple Inc", lei="HWUPKR0MPOU8FGXBT394"
                ))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "found"
        assert result.result["lei"] == "HWUPKR0MPOU8FGXBT394"

    def test_lei_lookup_not_found_returns_not_found(self):
        with patch("core.verify_company_record._gleif_by_lei", new=AsyncMock(return_value=None)):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(
                    name="Fake Corp", lei="AAAAAAAAAAAAAAAAAAAA"
                ))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "not_found"
        assert result.result["status"] == "not_found"


# ---------------------------------------------------------------------------
# Test 3: Not found -- both upstreams empty
# ---------------------------------------------------------------------------

class TestNotFound:
    def test_not_found_when_registries_empty(self):
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(name="zzz-nonexistent-company-xyz"))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "not_found"
        assert result.result["status"] == "not_found"
        assert "zzz-nonexistent-company-xyz" in result.result["queried_name"]
        assert len(result.result["sources_queried"]) > 0

    def test_not_found_message_is_honest(self):
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(
                    name="zzz-nonexistent-company-xyz", country="US"
                ))

        assert "not" in result.human_message.lower() or "no" in result.human_message.lower()
        # Must name the company it searched for
        assert "zzz-nonexistent-company-xyz" in result.human_message


# ---------------------------------------------------------------------------
# Test 4: SEC EDGAR enrichment
# ---------------------------------------------------------------------------

class TestEdgarEnrichment:
    def test_gleif_hit_enriched_with_edgar_ticker(self):
        edgar_record = {
            "legal_name": "Apple Inc",
            "ticker": "AAPL",
            "cik": "320193",
            "registry_authority": "SEC EDGAR (US public companies)",
            "jurisdiction": "US",
            "status": "active",
        }
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[_GLEIF_ENTITY])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=edgar_record)):
                result = run(handle_verify_company_record(name="Apple Inc", country="US"))

        assert result.status == OperationStatus.SUCCESS
        assert result.result["ticker"] == "AAPL"
        assert result.result["sec_cik"] == "320193"
        assert "SEC EDGAR" in result.result["sources"]
        assert "GLEIF" in result.result["sources"]

    def test_edgar_only_when_gleif_misses(self):
        edgar_record = {
            "legal_name": "Apple Inc",
            "ticker": "AAPL",
            "cik": "320193",
            "registry_authority": "SEC EDGAR (US public companies)",
            "jurisdiction": "US",
            "status": "active",
        }
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=edgar_record)):
                result = run(handle_verify_company_record(name="Apple Inc", country="US"))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "found"
        assert result.result["sources"] == ["SEC EDGAR"]
        assert result.result["lei"] is None  # GLEIF found nothing


# ---------------------------------------------------------------------------
# Test 5: Bad input
# ---------------------------------------------------------------------------

class TestBadInput:
    def test_empty_name_returns_failure(self):
        result = run(handle_verify_company_record(name=""))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"
        assert result.cost.amount == 0.0

    def test_whitespace_only_name_returns_failure(self):
        result = run(handle_verify_company_record(name="   "))
        assert result.status == OperationStatus.FAILURE
        assert result.reason_code == "bad_input"


# ---------------------------------------------------------------------------
# Test 6: Fail-open on upstream timeout
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_gleif_timeout_returns_not_found_not_exception(self):
        import httpx

        async def _raise(*args, **kwargs):
            raise httpx.TimeoutException("timed out")

        with patch("core.verify_company_record._gleif_by_name", side_effect=_raise):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(name="Some Corp", country="DE"))

        # Must not raise; must return a valid OutcomeReceipt
        assert result.status in (OperationStatus.SUCCESS, OperationStatus.FAILURE)
        # sources_unavailable or not_found is acceptable -- never a crash

    def test_both_upstreams_timeout_returns_not_found(self):
        import httpx

        async def _raise(*args, **kwargs):
            raise httpx.TimeoutException("timed out")

        with patch("core.verify_company_record._gleif_by_name", side_effect=_raise):
            with patch("core.verify_company_record._edgar_search", side_effect=_raise):
                result = run(handle_verify_company_record(name="Some Corp"))

        assert result.status == OperationStatus.SUCCESS
        assert result.reason_code == "not_found"


# ---------------------------------------------------------------------------
# Test 7: ASCII-only output
# ---------------------------------------------------------------------------

class TestAsciiOutput:
    def test_ascii_helper_strips_non_ascii(self):
        assert _ascii("Café") == "Cafe?"  # e with accent -> '?'
        assert _ascii("Apple Inc.") == "Apple Inc."
        assert _ascii("") == ""

    def test_non_ascii_in_registry_name_is_replaced(self):
        entity_with_unicode = {
            "id": "TESTLEI0000000000001",
            "attributes": {
                "lei": "TESTLEI0000000000001",
                "entity": {
                    "legalName": {"name": "Société Générale"},
                    "status": "ACTIVE",
                    "jurisdiction": "FR",
                    "legalAddress": {
                        "addressLines": ["29 Boulevard Haussmann"],
                        "city": "Paris",
                        "postalCode": "75009",
                        "country": "FR",
                    },
                },
                "registration": {
                    "status": "ISSUED",
                    "managingLou": "GLEIF",
                },
            },
        }
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[entity_with_unicode])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                result = run(handle_verify_company_record(name="Societe Generale", country="FR"))

        assert result.status == OperationStatus.SUCCESS
        legal_name = result.result.get("legal_name", "")
        # All chars must be ASCII (ord < 128)
        assert all(ord(c) < 128 for c in legal_name), f"Non-ASCII in legal_name: {legal_name!r}"


# ---------------------------------------------------------------------------
# Test 8: MCP annotations
# ---------------------------------------------------------------------------

class TestMcpAnnotations:
    def test_listed_as_read_only_and_idempotent(self):
        tools = {t["name"]: t for t in _build_tool_list()}
        assert "verify_company_record" in tools, "tool not in tools/list"
        ann = tools["verify_company_record"]["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["idempotentHint"] is True
        assert ann["destructiveHint"] is False

    def test_not_in_write_tools(self):
        from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH
        assert "verify_company_record" not in _WRITE_TOOLS_REQUIRING_AUTH


# ---------------------------------------------------------------------------
# Test 9: MCP tools/call dispatch
# ---------------------------------------------------------------------------

class TestMcpDispatch:
    def test_callable_via_mcp_found(self):
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[_GLEIF_ENTITY])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                r = run(handle_mcp_request({
                    "jsonrpc": "2.0", "id": 100, "method": "tools/call",
                    "params": {
                        "name": "verify_company_record",
                        "arguments": {"name": "Apple Inc", "country": "US"},
                    },
                }))

        assert "result" in r, f"expected result, got {r}"
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["status"] == "success"
        assert data["result"]["status"] == "found"
        assert data["result"]["lei"] == "HWUPKR0MPOU8FGXBT394"

    def test_callable_via_mcp_not_found(self):
        with patch("core.verify_company_record._gleif_by_name", new=AsyncMock(return_value=[])):
            with patch("core.verify_company_record._edgar_search", new=AsyncMock(return_value=None)):
                r = run(handle_mcp_request({
                    "jsonrpc": "2.0", "id": 101, "method": "tools/call",
                    "params": {
                        "name": "verify_company_record",
                        "arguments": {"name": "zzz-nonexistent-company-xyz"},
                    },
                }))

        assert "result" in r
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["result"]["status"] == "not_found"

    def test_missing_name_arg_returns_param_error(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 102, "method": "tools/call",
            "params": {"name": "verify_company_record", "arguments": {}},
        }))
        assert "error" in r
        assert r["error"]["code"] == -32602
        assert "name" in r["error"]["message"]


# ---------------------------------------------------------------------------
# Test 10: preview_cost reports free
# ---------------------------------------------------------------------------

class TestPreviewCost:
    def test_preview_cost_reports_free(self):
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        resp = run(handle_preview_cost(PreviewCostRequest(
            operation="verify_company_record",
            params={"name": "Apple Inc"},
        )))
        assert resp.estimated_cost_usd == 0.0
