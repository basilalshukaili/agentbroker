"""Regression tests for the tools/call idempotency gate (wired 2026-08-26).

The contract was ADVERTISED (README, well_known.py, api/errors.md) but never
implemented: every write tool minted a fresh uuid4 operation_id, so an agent
that timed out and blindly retried caused a second real side effect and a
second charge. These tests lock in the dispatch-level dedupe.
"""
import asyncio

import pytest

import agent_interface.mcp_server as mcp
import storage.supabase_client as sb
from storage.idempotency_store import get_idempotency_store


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No Supabase network in tests; fresh in-memory store per test."""
    async def _no_rows(*a, **k):
        return []

    async def _no_insert(*a, **k):
        return None

    monkeypatch.setattr(sb, "select_rows", _no_rows)
    monkeypatch.setattr(sb, "insert_row", _no_insert)
    get_idempotency_store()._store.clear()
    yield
    get_idempotency_store()._store.clear()


def _params(key=None, body="hi"):
    args = {"recipient": {"id_value": "+15551230000"}, "content": {"body": body}}
    if key:
        args["idempotency_key"] = key
    return {"name": "send_message", "arguments": args}


HDRS = {"x-agent-identity": "test-bearer-token-abc"}


def test_replay_same_key_returns_original_without_reexecution(monkeypatch):
    calls = {"n": 0}

    async def fake_impl(params, headers=None):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": f"receipt-{calls['n']}"}],
                "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)
    r1 = asyncio.run(mcp._h_tools_call(_params(key="k1"), HDRS))
    r2 = asyncio.run(mcp._h_tools_call(_params(key="k1"), HDRS))
    assert calls["n"] == 1                      # executed exactly once
    assert r1 == r2                             # identical original receipt
    assert "receipt-1" in r2["content"][0]["text"]


def test_same_key_different_args_conflicts(monkeypatch):
    calls = {"n": 0}

    async def fake_impl(params, headers=None):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)
    asyncio.run(mcp._h_tools_call(_params(key="k2", body="first"), HDRS))
    r2 = asyncio.run(mcp._h_tools_call(_params(key="k2", body="DIFFERENT"), HDRS))
    assert calls["n"] == 1                      # second call never dispatched
    assert r2["isError"] is True
    assert "idempotency_conflict" in r2["content"][0]["text"]


def test_failures_are_not_pinned(monkeypatch):
    calls = {"n": 0}

    async def fake_impl(params, headers=None):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "boom"}], "isError": True}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)
    asyncio.run(mcp._h_tools_call(_params(key="k3"), HDRS))
    asyncio.run(mcp._h_tools_call(_params(key="k3"), HDRS))
    assert calls["n"] == 2                      # transient failure -> retry runs


def test_no_key_or_no_identity_bypasses_gate(monkeypatch):
    calls = {"n": 0}

    async def fake_impl(params, headers=None):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)
    # no idempotency_key at all
    asyncio.run(mcp._h_tools_call(_params(), HDRS))
    asyncio.run(mcp._h_tools_call(_params(), HDRS))
    # key but no identity header -> no scope -> no dedupe
    asyncio.run(mcp._h_tools_call(_params(key="k4"), {}))
    asyncio.run(mcp._h_tools_call(_params(key="k4"), {}))
    assert calls["n"] == 4


def test_key_is_popped_before_handler(monkeypatch):
    seen = {}

    async def fake_impl(params, headers=None):
        seen["args"] = params.get("arguments", {})
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)
    asyncio.run(mcp._h_tools_call(_params(key="k5"), HDRS))
    assert "idempotency_key" not in seen["args"]


def test_write_tools_advertise_the_param():
    tools = {t["name"]: t for t in mcp._build_tool_list()}
    for name in mcp._WRITE_TOOLS_REQUIRING_AUTH:
        props = tools[name]["inputSchema"].get("properties", {})
        assert "idempotency_key" in props, f"{name} missing idempotency_key in schema"
    # read tools must NOT get it
    assert "idempotency_key" not in tools["find_business"]["inputSchema"].get("properties", {})
