"""
Unit tests for lookup_us_contracts -- free, read-only US federal contract lookup.

Tests use unittest.mock to patch httpx so no real network calls are made.
Covers:
  1. Happy path: USASpending.gov returns contract awards.
  2. Not-found path: upstream returns empty results.
  3. Bad input: empty company_name returns FAILURE.
  4. Upstream unavailable: RuntimeError -> partial_lookup, retriable=True.
  5. ASCII-only output: non-ASCII chars in award data are replaced with '?'.
  6. max_results is bounded (min 1, max 10).
  7. total_in_result_set_usd is correctly summed.
  8. MCP annotations: readOnlyHint=True, idempotentHint=True.
  9. MCP tools/call: callable via the MCP dispatcher (dispatch route wired).
  10. The tool appears in tools/list with the correct name.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.lookup_us_contracts import handle_lookup_us_contracts, _ascii, _parse_award
from core.models import OperationStatus
from agent_interface.mcp_server import _build_tool_list, handle_mcp_request


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Shared mock award data
# ---------------------------------------------------------------------------

_AWARD_1 = {
    "Award ID": "DAAH04-95-C-0008",
    "Recipient Name": "LOCKHEED MARTIN CORPORATION",
    "Award Amount": 500000000.0,
    "Awarding Agency": "Department of Defense",
    "NAICS Code": "336414",
    "NAICS Description": "Guided Missile and Space Vehicle Manufacturing",
    "Description": "MISSILE DEFENSE SYSTEM",
    "Period of Performance Start Date": "2022-01-15",
    "Period of Performance Current End Date": "2026-09-30",
}

_AWARD_2 = {
    "Award ID": "DAAH04-95-C-0009",
    "Recipient Name": "LOCKHEED MARTIN CORPORATION",
    "Award Amount": 120000000.0,
    "Awarding Agency": "NASA",
    "NAICS Code": "336414",
    "NAICS Description": "Guided Missile and Space Vehicle Manufacturing",
    "Description": "SATELLITE INTEGRATION",
    "Period of Performance Start Date": "2023-06-01",
    "Period of Performance Current End Date": "2025-12-31",
}

_USASPENDING_RESPONSE = {"results": [_AWARD_1, _AWARD_2]}
_USASPENDING_EMPTY = {"results": []}


def _mock_post_hit():
    """httpx response mock returning USASpending award results."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _USASPENDING_RESPONSE
    return resp


def _mock_post_empty():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = _USASPENDING_EMPTY
    return resp


def _mock_post_error():
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "Internal Server Error"
    return resp


# ---------------------------------------------------------------------------
# 1. Happy path: awards found
# ---------------------------------------------------------------------------

def test_happy_path_returns_found_status():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_post_hit())

        result = run(handle_lookup_us_contracts("Lockheed Martin"))

    assert result.status == OperationStatus.SUCCESS
    assert result.reason_code == "found"
    assert result.result["status"] == "found"
    assert len(result.result["awards"]) == 2
    assert result.result["awards"][0]["recipient_name"] == "LOCKHEED MARTIN CORPORATION"
    assert result.result["awards"][0]["award_amount_usd"] == 500000000.0
    assert result.result["awards"][0]["awarding_agency"] == "Department of Defense"
    assert result.result["awards"][0]["naics_code"] == "336414"


def test_happy_path_total_is_summed():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_post_hit())

        result = run(handle_lookup_us_contracts("Lockheed Martin"))

    assert result.result["total_in_result_set_usd"] == 620000000.0


# ---------------------------------------------------------------------------
# 2. Not-found: upstream returns empty
# ---------------------------------------------------------------------------

def test_not_found_returns_not_found_status():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_post_empty())

        result = run(handle_lookup_us_contracts("zzz-nonexistent-company-xyz"))

    assert result.status == OperationStatus.SUCCESS
    assert result.reason_code == "not_found"
    assert result.result["status"] == "not_found"
    assert result.result["awards"] == []
    assert result.retriable is False


# ---------------------------------------------------------------------------
# 3. Bad input: empty company_name
# ---------------------------------------------------------------------------

def test_empty_name_returns_failure():
    result = run(handle_lookup_us_contracts(""))
    assert result.status == OperationStatus.FAILURE
    assert result.reason_code == "bad_input"
    assert result.retriable is False
    assert result.cost.amount == 0.0


def test_whitespace_only_name_returns_failure():
    result = run(handle_lookup_us_contracts("   "))
    assert result.status == OperationStatus.FAILURE
    assert result.reason_code == "bad_input"


# ---------------------------------------------------------------------------
# 4. Upstream unavailable: network error -> partial_lookup, retriable
# ---------------------------------------------------------------------------

def test_upstream_error_returns_partial_lookup():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))

        result = run(handle_lookup_us_contracts("Booz Allen Hamilton"))

    assert result.status == OperationStatus.SUCCESS
    assert result.reason_code == "partial_lookup"
    assert result.result["status"] == "unavailable"
    assert result.retriable is True
    assert result.cost.amount == 0.0


def test_upstream_http_500_returns_partial_lookup():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_post_error())

        result = run(handle_lookup_us_contracts("SAIC"))

    assert result.reason_code == "partial_lookup"
    assert result.retriable is True


# ---------------------------------------------------------------------------
# 5. ASCII-only output
# ---------------------------------------------------------------------------

def test_ascii_helper_replaces_non_ascii():
    assert _ascii("Café") == "Cafe?"  # accent dropped as '?'
    assert _ascii("ABC") == "ABC"
    assert _ascii("") == ""


def test_non_ascii_in_award_data_is_replaced():
    raw_with_unicode = dict(_AWARD_1)
    raw_with_unicode["Recipient Name"] = "Müller GmbH"

    parsed = _parse_award(raw_with_unicode)
    # All chars in output must be ASCII
    assert all(ord(c) < 128 for c in parsed["recipient_name"])


# ---------------------------------------------------------------------------
# 6. max_results is bounded
# ---------------------------------------------------------------------------

def test_max_results_is_capped_at_10():
    """The handler sends max(1, min(x, 10)) to the API call.
    We verify no crash and correct capping with a real invocation path."""
    payload_sent = {}

    async def capture_post(url, json=None, headers=None):
        payload_sent["limit"] = (json or {}).get("limit")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _USASPENDING_EMPTY
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=capture_post)

        run(handle_lookup_us_contracts("Boeing", max_results=99))

    assert payload_sent["limit"] == 10  # capped


def test_max_results_minimum_is_1():
    payload_sent = {}

    async def capture_post(url, json=None, headers=None):
        payload_sent["limit"] = (json or {}).get("limit")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _USASPENDING_EMPTY
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=capture_post)

        run(handle_lookup_us_contracts("Boeing", max_results=0))

    assert payload_sent["limit"] == 1  # floored to 1


# ---------------------------------------------------------------------------
# 7. total_in_result_set_usd handles None amounts gracefully
# ---------------------------------------------------------------------------

def test_total_excludes_none_amounts():
    award_with_null = dict(_AWARD_1)
    award_with_null["Award Amount"] = None
    usaspending_response = {"results": [award_with_null, _AWARD_2]}

    async def mock_post(url, json=None, headers=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = usaspending_response
        return resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=mock_post)

        result = run(handle_lookup_us_contracts("Test Corp"))

    # Only _AWARD_2's amount (120,000,000) should be summed
    assert result.result["total_in_result_set_usd"] == 120000000.0


# ---------------------------------------------------------------------------
# 8. MCP annotations: readOnlyHint, idempotentHint
# ---------------------------------------------------------------------------

def test_mcp_annotations_read_only_and_idempotent():
    tools = _build_tool_list()
    tool = next((t for t in tools if t["name"] == "lookup_us_contracts"), None)
    assert tool is not None, "lookup_us_contracts not found in MCP tool list"
    annotations = tool.get("annotations", {})
    assert annotations.get("readOnlyHint") is True
    assert annotations.get("idempotentHint") is True


# ---------------------------------------------------------------------------
# 9. MCP dispatcher: tools/call routes correctly
# ---------------------------------------------------------------------------

def test_mcp_dispatch_routes_to_handler():
    """The MCP dispatcher must route tools/call for lookup_us_contracts."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_post_empty())

        resp = run(handle_mcp_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "lookup_us_contracts",
                "arguments": {"company_name": "Palantir Technologies"},
            },
        }))

    assert "error" not in resp
    content = resp.get("result", {}).get("content", [])
    assert len(content) > 0
    payload = json.loads(content[0]["text"])
    # The OutcomeReceipt status field is the operation status ("success"/"failure").
    # The inner result.status carries "found"/"not_found"/"unavailable".
    # Either confirms the dispatch reached the handler (not a _ParamError).
    assert payload.get("status") in ("success", "failure", "partial")
    # And the reason_code distinguishes found/not_found/error
    assert payload.get("reason_code") in ("found", "not_found", "partial_lookup", "bad_input")


# ---------------------------------------------------------------------------
# 10. Tool appears in tools/list
# ---------------------------------------------------------------------------

def test_tool_in_tools_list():
    resp = run(handle_mcp_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }))
    tools = resp.get("result", {}).get("tools", [])
    names = [t["name"] for t in tools]
    assert "lookup_us_contracts" in names


def test_tool_list_reaches_22():
    """Tool count must be 22 now that lookup_us_contracts is wired."""
    resp = run(handle_mcp_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }))
    tools = resp.get("result", {}).get("tools", [])
    assert len(tools) == 22
