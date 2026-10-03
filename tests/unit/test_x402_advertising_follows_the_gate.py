"""Everything that tells an agent about the x402 rail must be a function of the gate.

THE DEFECT (measured 2026-10-03, requirement row 512). On the VPS the AgentBroker
container holds the receiver address and both CDP credentials but NOT X402_ENABLED, so
billing.x402_gate.enabled() is False and a payment attached to a call is silently
ignored. Yet every anonymous caller of a key-required tool was told:

    "Option 3 (pay per call, no signup): attach an x402 payment in
     params._meta['x402/payment'] and this call is served without a key - USDC on Base."

That sentence was hard-wired into the `if checkout` branch of the auth_required message.
The discovery descriptor (`rails`) and the `how_to_resolve["x402"]` hint were already
derived from the gate; this one string was not, so the live server contradicted its own
descriptor ("rails": ["credits"]).

The rule, enforced here for every advertising surface in BOTH gate states:
  * rail OFF -> nothing mentions x402/USDC, and /.well-known/x402 is a 404;
  * rail ON  -> the auth_required text, the tool descriptions, the descriptor and the
    discovery document all say so, and the discovery document names the SAME receiver,
    network and prices the 402 offer is built from.
Nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RECEIVER = "0x" + "ab" * 20
FACILITATOR = "https://facilitator.example/x402"
PUBLIC_MCP = "https://api.example/mcp"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gate_off(monkeypatch):
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)
    return x402_gate


@pytest.fixture
def gate_on(monkeypatch):
    """Rail switched ON with a known, fake configuration."""
    import config
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", RECEIVER)
    monkeypatch.setattr(config, "X402_FACILITATOR_URL", FACILITATOR)
    monkeypatch.setattr(config, "X402_PUBLIC_MCP_URL", PUBLIC_MCP)
    monkeypatch.setattr(config, "X402_ENABLE_TESTNET", False)
    return x402_gate


@pytest.fixture
def storefront(monkeypatch):
    """The shape of the auth_required message that carried the hard-wired sentence: write tools need a
    key, the Polar checkout URL is set, and credits are on (so "Option 2" exists and x402 is "Option 3").
    The credits-off shapes are pinned in test_payments_advertising_follows_the_switches.py."""
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.setenv("POLAR_CHECKOUT_URL", "https://polar.example/checkout")
    monkeypatch.setenv("CREDITS_ENABLED", "true")


def _anonymous_auth_required(tool="send_message"):
    from agent_interface.mcp_server import handle_mcp_request
    resp = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": tool, "arguments": {}}},
        headers={"user-agent": "x402-advertising-test"}))
    result = resp["result"]
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["error_code"] == "auth_required", body
    return body


# -------------------------------------------------------------------- auth_required text

def test_rail_off_the_auth_required_message_does_not_offer_x402(gate_off, storefront):
    body = _anonymous_auth_required()
    text = (body["human_message"] + json.dumps(body["how_to_resolve"])).lower()
    assert "x402" not in text, "told an agent to pay by x402 while the gate is off"
    assert "usdc" not in text
    assert "option 3" not in text
    # the two real options must still be there, untouched
    assert "Option 1 (free)" in body["human_message"]
    assert "Option 2 (credits)" in body["human_message"]
    assert "x402" not in body["how_to_resolve"]


def test_rail_on_the_auth_required_message_offers_x402_and_the_hint_agrees(gate_on, storefront):
    body = _anonymous_auth_required()
    assert "Option 3 (pay per call, no signup)" in body["human_message"]
    assert "USDC on Base" in body["human_message"]
    assert "x402" in body["how_to_resolve"], "free text and structured hint disagree"


# -------------------------------------------------------------------- tool descriptions

def _tool_descriptions():
    from agent_interface.mcp_server import _build_tool_list
    return {t["name"]: t["description"] for t in _build_tool_list()}


def test_rail_off_no_tool_description_mentions_x402(gate_off):
    offenders = [n for n, d in _tool_descriptions().items() if "x402" in d.lower() or "usdc" in d.lower()]
    assert offenders == [], f"descriptions advertise a rail that is off: {offenders}"


def test_rail_on_exactly_the_paid_tools_carry_the_mention(gate_on):
    descs = _tool_descriptions()
    tagged = {n for n, d in descs.items() if "x402" in d}
    paid = {n for n in descs if gate_on.is_paid_tool(n)}
    assert paid, "no paid tools found; the test would pass vacuously"
    assert tagged == paid, f"tagged {sorted(tagged)} but gate takes payment for {sorted(paid)}"
    for n in tagged:
        assert descs[n].count("x402") == 1, f"{n}: mention is duplicated"


def test_the_mention_is_one_short_line(gate_on):
    from agent_interface.mcp_server import _X402_TOOL_TAG
    assert "\n" not in _X402_TOOL_TAG and len(_X402_TOOL_TAG) <= 48


# -------------------------------------------------------------------- discovery descriptor

def test_the_descriptor_rails_follow_the_gate(monkeypatch):
    from agent_interface.well_known import get_mcp_descriptor
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)
    assert "x402" not in get_mcp_descriptor()["payments"]["rails"]
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    assert "x402" in get_mcp_descriptor()["payments"]["rails"]


# -------------------------------------------------------------------- /.well-known/x402

def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def test_rail_off_the_discovery_document_is_a_404(gate_off):
    assert gate_off.discovery_document() is None
    r = _client().get("/.well-known/x402")
    assert r.status_code == 404, r.text


def test_rail_on_the_discovery_document_is_served_from_the_gate_config(gate_on):
    r = _client().get("/.well-known/x402")
    assert r.status_code == 200, r.text
    doc = r.json()
    # indexer convention + IETF-draft names + the legacy top-level fields
    assert doc["version"] == 1 and doc["x402Version"] == 2
    assert doc["kind"] == "resource-server"
    assert doc["resources"] == [PUBLIC_MCP]
    assert doc["facilitatorUrl"] == FACILITATOR
    assert doc["payTo"] == RECEIVER and doc["network"] == gate_on.MAINNET
    # the accepted terms are the real ones
    (term,) = doc["accepts"]
    assert term == {"scheme": "exact", "network": gate_on.MAINNET,
                    "asset": gate_on.USDC_BASE, "payTo": RECEIVER}


def test_the_document_cannot_name_a_different_receiver_than_the_offer(gate_on, monkeypatch):
    """The 2026-10-03 finding: a static file's payTo differed from the container's receiver.
    Change the configured receiver and the document must follow it - there is no second copy."""
    import config
    other = "0x" + "cd" * 20
    monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", other)
    doc = gate_on.discovery_document()
    assert doc["payTo"] == other and doc["accepts"][0]["payTo"] == other
    assert RECEIVER not in json.dumps(doc)


def test_prices_in_the_document_are_the_gates_prices(gate_on):
    doc = gate_on.discovery_document()
    assert doc["pricesUsd"] == dict(sorted(gate_on._PRICING_USD.items()))
    assert doc["pricesUsd"], "no prices published"


def test_testnet_is_listed_only_when_the_gate_accepts_it(gate_on, monkeypatch):
    import config
    assert [a["network"] for a in gate_on.discovery_document()["accepts"]] == [gate_on.MAINNET]
    monkeypatch.setattr(config, "X402_ENABLE_TESTNET", True)
    nets = [a["network"] for a in gate_on.discovery_document()["accepts"]]
    assert nets == [gate_on.MAINNET, gate_on.TESTNET]


# -------------------------------------------------------------------- buyer-intent alert noise

def test_a_price_request_is_not_a_buyer_attempt(gate_on):
    """The advertised quote flow sends ANY value to get the priced offer. That must not page
    the founder with 'a real buyer is here'."""
    f = gate_on._is_signed_payment_attempt
    assert f({"x402/payment": "quote-request"}) is False
    assert f({"x402/payment": ""}) is False
    assert f({"x402/payment": None}) is False
    assert f({"x402/payment": {}}) is False
    assert f({"x402/payment": {"payload": {}}}) is False
    assert f({}) is False and f(None) is False
    assert f({"x402/payment": {"x402Version": 2, "payload": {"signature": "0x01"}}}) is True


def test_run_paid_tool_only_alerts_on_a_signed_payment(gate_on, monkeypatch):
    """Drives the real run_paid_tool far enough to reach the alert, with the resource
    server stubbed out so nothing touches a facilitator or the network."""
    alerts = []

    async def fake_alert(tool):
        alerts.append(tool)

    async def stop_here():
        raise RuntimeError("stop after the telemetry block")

    monkeypatch.setattr(gate_on, "_notify_buyer_intent", fake_alert)
    monkeypatch.setattr(gate_on, "_ensure_server", lambda: stop_here())

    async def dispatch():
        return {"status": "success"}

    for meta, expected in (
        ({"x402/payment": "quote-request"}, []),
        ({"x402/payment": {"x402Version": 2, "payload": {"signature": "0x01"}}}, ["screen_sanctions"]),
    ):
        alerts.clear()
        with pytest.raises(RuntimeError):
            _run(gate_on.run_paid_tool("screen_sanctions", {}, meta, dispatch))
        assert alerts == expected, f"{meta!r} -> {alerts}"
