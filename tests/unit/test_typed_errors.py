"""Typed machine-recoverable error contract (P0 #3, 2026-08-26).

Before: auth failures and quota limits surfaced as raw JSON-RPC -32602 prose -
many MCP clients treat that as a protocol fault and the MODEL never sees the
recovery path. Now: tool-execution failures return isError:true RESULTS with
typed fields (error_code / retriable / retry_after_ms / how_to_resolve) so an
agent can branch: authenticate vs pay vs back off vs fix args.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agent_interface.mcp_server as mcp


def _call(payload, headers=None):
    return asyncio.run(mcp.handle_mcp_request(payload, headers or {}))


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_auth_required_is_typed_iserror_result(monkeypatch):
    import main

    def deny(token, name):
        raise HTTPException(status_code=401, detail="X-Agent-Identity required")

    monkeypatch.setattr(main, "_get_identity", deny)
    resp = _call({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "send_message", "arguments": {}}})
    assert "error" not in resp, "must be a RESULT, not a protocol error"
    assert resp["result"]["isError"] is True
    b = _body(resp)
    assert b["error_code"] == "auth_required"
    assert b["retriable"] is False
    assert b["how_to_resolve"]["free_key"]["url"].endswith("/keys/request")
    assert b["how_to_resolve"]["header"] == "X-Agent-Identity"


def test_free_quota_exceeded_is_rate_limited_with_retry_after(monkeypatch):
    import agent_interface.identity as ident
    import agent_interface.key_request_logic as krl

    monkeypatch.setattr(ident, "validate_token", lambda t: SimpleNamespace(
        valid=True, identity=SimpleNamespace(agent_id="free_testquota")))
    monkeypatch.setattr(krl, "consume_free_daily", lambda k: False)
    monkeypatch.setattr(krl, "get_free_daily_remaining", lambda k: 0)

    resp = _call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "send_message", "arguments": {}}},
                 headers={"x-agent-identity": "some-free-token"})
    assert resp["result"]["isError"] is True
    b = _body(resp)
    assert b["error_code"] == "rate_limited"
    assert b["retriable"] is True
    assert 0 < b["retry_after_ms"] <= 24 * 3600 * 1000
    assert "pricing" in b["how_to_resolve"]["upgrade"]


def test_unknown_tool_protocol_error_carries_typed_data():
    resp = _call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "no_such_tool", "arguments": {}}})
    assert "error" in resp
    d = resp["error"].get("data", {})
    assert d.get("error_code") == "invalid_argument"
    assert d["how_to_resolve"]["call"] == "tools/list"
