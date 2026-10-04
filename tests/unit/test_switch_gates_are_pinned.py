"""The gate side of the one-expression guarantee: each place that ACTS on a money switch is pinned both ways.

billing/switches.py is the one reader of CREDITS_ENABLED and DATA_METERING_ENABLED, and the advertisers call
the same functions the gates do. The advertising side is pinned in test_payments_advertising_follows_the_switches.py.
This file pins the gate side, found by mutation (review of feat/x402-honesty-20261004, F7): of 19 single-line
mutations of production code, three SURVIVED the full suite - the data-tool bypass in the MCP dispatcher, the
REST credits middleware, and the data block of check_quota - because nothing exercised them with the switch in
BOTH positions through the real code. Each test below does, and was proven to fail against its mutation:

    mcp_server  _data_metering_on = False / True          (data-tool bypass)
    main        `if not credits_enabled():` -> `if False:` / `if True:`   (REST credits gate)
    mcp_server  check_quota's `if data_metering_enabled():` -> `if True:` / `if False:`

Nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _every_switch_off(monkeypatch):
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)


# ---------------------------------------------------------------- the data-tool bypass (MCP dispatcher)

@pytest.fixture
def data_call(monkeypatch):
    """Drive tools/call for a premium data tool through the real dispatcher, recording which billing step ran.
    The tool itself is stubbed (nothing is screened); the QUOTA gate is replaced by a recorder."""
    import agent_interface.mcp_server as ms
    from billing import data_quota
    seen = {"quota": 0, "dispatched": 0}

    async def fake_quota(**kw):
        seen["quota"] += 1
        return {"allowed": True, "remaining": 1}

    async def fake_dispatch(name, args, headers=None, skip_auth=False):
        seen["dispatched"] += 1
        return {"status": "success"}

    monkeypatch.setattr(data_quota, "consume_data_quota", fake_quota)
    monkeypatch.setattr(ms, "_dispatch_and_label", fake_dispatch)

    def call(name="screen_sanctions"):
        resp = _run(ms.handle_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": {"name": "Jane Roe"}}},
            headers={"user-agent": "switch-gate-test"}))
        assert resp["result"]["isError"] is False, resp
        return seen
    return call


@pytest.mark.parametrize("tool", ["screen_sanctions", "verify_company_record", "map_trade_restriction"])
def test_metering_off_the_quota_gate_never_runs(data_call, tool):
    seen = data_call(tool)
    assert seen == {"quota": 0, "dispatched": 1}, "the bypass must answer a data tool free, with no quota consumed"


@pytest.mark.parametrize("tool", ["screen_sanctions", "verify_company_record", "map_trade_restriction"])
def test_metering_on_the_quota_gate_runs_once(monkeypatch, data_call, tool):
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    seen = data_call(tool)
    assert seen == {"quota": 1, "dispatched": 1}, "with metering on the same call must consume the quota"


def test_a_non_data_tool_never_touches_the_quota_gate_either_way(monkeypatch, data_call):
    for value in (None, "true"):
        if value:
            monkeypatch.setenv("DATA_METERING_ENABLED", value)
        seen = data_call("find_business")
        assert seen["quota"] == 0


# ---------------------------------------------------------------- the REST credits middleware

@pytest.fixture
def rest_gate(monkeypatch):
    """POST a paid /ops write with no key and count how many times the credits gate resolved an account."""
    from fastapi.testclient import TestClient
    import main
    from billing import credits
    seen = {"resolved": 0}

    def resolve(headers):
        seen["resolved"] += 1
        return None     # anonymous: the gate then falls through to the existing auth path

    monkeypatch.setattr(credits, "resolve_account", resolve)
    client = TestClient(main.app, raise_server_exceptions=False)

    def post():
        client.post("/ops/capture_lead", json={})
        return seen["resolved"]
    return post


def test_credits_off_the_rest_gate_is_transparent(rest_gate):
    assert rest_gate() == 0, "the credits gate ran while CREDITS_ENABLED is off"


def test_credits_on_the_rest_gate_runs(monkeypatch, rest_gate):
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    assert rest_gate() == 1, "the credits gate did not run while CREDITS_ENABLED is on"


def test_credits_on_but_x402_on_the_rest_gate_stands_aside(monkeypatch, rest_gate):
    """ONE rail: the x402 middleware owns the REST path when it is live."""
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    assert rest_gate() == 0


# ---------------------------------------------------------------- check_quota's data block

def _free_token(key_id):
    from agent_interface.identity import issue_token, TokenRequest
    return issue_token(TokenRequest(agent_id=key_id, principal_id="test_user_001", principal_type="human",
                                    allowed_operations=["*"], budget_cap_usd=0.0)).token


def test_check_quota_omits_the_data_block_while_metering_is_off():
    from agent_interface.mcp_server import _handle_check_quota
    out = _handle_check_quota(_free_token("free_gate_off"))
    assert out["tier"] == "free" and "data_quota" not in out


def test_check_quota_includes_the_data_block_while_metering_is_on(monkeypatch):
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    from agent_interface.mcp_server import _handle_check_quota
    out = _handle_check_quota(_free_token("free_gate_on"))
    assert out["tier"] == "free"
    assert set(out["data_quota"]) == {"daily_limit", "remaining_today", "resets"}
    assert out["data_quota"]["daily_limit"] > 0


# ---------------------------------------------------------------- the flag tests that re-implemented the parse

@pytest.mark.parametrize("value,expected", [("true", True), ("1", True), ("yes", True), ("false", False), ("", False)])
def test_the_data_metering_flag_is_parsed_by_production_code(monkeypatch, value, expected):
    """test_data_metering.py's cfg_* tests evaluated `os.getenv(...).lower() in (...)` inline, so they would
    pass whatever the production parser did. This calls the production function."""
    from billing import switches
    monkeypatch.setenv("DATA_METERING_ENABLED", value)
    assert switches.data_metering_enabled() is expected
