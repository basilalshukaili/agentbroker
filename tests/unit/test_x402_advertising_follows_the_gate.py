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


def test_rail_on_exactly_the_paid_tools_carry_the_mention(gate_on, monkeypatch):
    """With metering on every tool the gate prices is tagged. With metering off the three premium data
    tools are answered free before the x402 branch, so they are paid tools the gate NEVER takes payment
    for in that state and must not be tagged (test_cost_claims_follow_the_switches.py pins that side)."""
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
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


def test_prices_in_the_document_are_the_gates_prices(gate_on, monkeypatch):
    # With metering on every paid tool is priced by the gate. (With it off the three premium data
    # tools are answered free before the x402 branch and are not listed: see the test below.)
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    doc = gate_on.discovery_document()
    assert doc["pricesUsd"] == dict(sorted(gate_on._PRICING_USD.items()))
    assert doc["pricesUsd"], "no prices published"


def test_discovery_does_not_price_the_data_tools_while_metering_is_off(gate_on, monkeypatch):
    """THE FINDING (third review, P2). X402_ENABLED on and DATA_METERING_ENABLED off is the state an
    operator reaches by switching x402 on first. A tools/call for screen_sanctions that carries a payment
    never reaches run_paid_tool there: the data-metering bypass answers it free. Yet the document listed
    all three at $0.02, so an indexer would show free tools as pay-per-call."""
    from billing.data_quota import PREMIUM_DATA_TOOLS
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    doc = gate_on.discovery_document()
    for tool in PREMIUM_DATA_TOOLS:
        assert tool not in doc["pricesUsd"], f"{tool} is priced in /.well-known/x402 but served free"
    assert "send_message" in doc["pricesUsd"], "the tools the gate does charge for stay listed"
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    on = gate_on.discovery_document()
    for tool in PREMIUM_DATA_TOOLS:
        assert on["pricesUsd"][tool] == gate_on.price_usd(tool), tool


def test_discovery_prices_and_tool_tags_name_the_same_tools(gate_on, monkeypatch):
    """The document and tools/list are one claim: a tool is priced in /.well-known/x402 exactly when its
    description offers x402 for it, in both metering states."""
    from agent_interface import mcp_server as ms
    from billing.data_quota import PREMIUM_DATA_TOOLS
    assert PREMIUM_DATA_TOOLS == ms._PREMIUM_DATA_TOOLS, "two copies of the premium-data list must agree"
    for metering in (False, True):
        if metering:
            monkeypatch.setenv("DATA_METERING_ENABLED", "true")
        else:
            monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
        listed = set(gate_on.discovery_document()["pricesUsd"])
        tagged = {t for t in gate_on._PRICING_USD if ms._x402_tag(t)}
        assert listed == tagged, (metering, sorted(listed ^ tagged))


def test_testnet_is_listed_only_when_the_gate_accepts_it(gate_on, monkeypatch):
    import config
    assert [a["network"] for a in gate_on.discovery_document()["accepts"]] == [gate_on.MAINNET]
    monkeypatch.setattr(config, "X402_ENABLE_TESTNET", True)
    nets = [a["network"] for a in gate_on.discovery_document()["accepts"]]
    assert nets == [gate_on.MAINNET, gate_on.TESTNET]


# -------------------------------------------------------------------- buyer-intent alert noise

# A payment payload in the shape the SDK accepts (x402.schemas.PaymentPayload requires `payload` AND `accepted`).
SIGNED = {
    "x402Version": 2,
    "payload": {"signature": "0x01", "authorization": {}},
    "accepted": {"scheme": "exact", "network": "eip155:8453", "asset": "0x" + "cd" * 20,
                 "amount": "20000", "payTo": RECEIVER, "maxTimeoutSeconds": 60},
}


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
    assert f({"x402/payment": SIGNED}) is True


def test_a_payload_the_sdk_cannot_parse_is_not_a_buyer_attempt(gate_on):
    """Review finding F4(b): {'payload': {'a': 1}} used to count (non-empty dict payload), although the SDK
    parses it to None. An anonymous call with arguments {} reaches run_paid_tool before identity validation,
    so one request with this meta paged the founder 'a real buyer is here' (cooldown 900 s per tool, ten
    paid tools) and inflated the funnel counter."""
    f = gate_on._is_signed_payment_attempt
    assert f({"x402/payment": {"payload": {"a": 1}}}) is False
    assert f({"x402/payment": {"x402Version": 2, "payload": {"signature": "0x01"}}}) is False, "no `accepted`"
    assert f({"x402/payment": {k: v for k, v in SIGNED.items() if k != "accepted"}}) is False


def test_a_json_string_payment_counts_because_the_sdk_accepts_it(gate_on):
    """Review finding F4(a): the SDK's extract_payment_from_meta also accepts the payment as a JSON STRING.
    A fully valid signed payment sent that way was accepted by the SDK (and settled) but returned False
    here, so no attempt was recorded and the founder was never told a buyer had come - a regression, since
    the code before this branch counted any truthy value."""
    f = gate_on._is_signed_payment_attempt
    assert f({"x402/payment": json.dumps(SIGNED)}) is True
    assert f({"x402/payment": "{not json"}) is False
    assert f({"x402/payment": "[1]"}) is False
    assert f({"x402/payment": json.dumps({"payload": {"a": 1}})}) is False


def test_the_predicate_is_the_sdks_own_parse_not_a_copy_of_it(gate_on):
    """The two cannot disagree in either direction because the predicate IS the SDK call: for every shape,
    it equals 'the SDK found a payment'."""
    from x402.mcp.utils import extract_payment_from_meta
    shapes = [SIGNED, json.dumps(SIGNED), "quote-request", "", None, {}, {"payload": {}}, {"payload": {"a": 1}},
              {"x402Version": 2, "payload": {"signature": "0x01"}}, "{}", "[1]", 7, [SIGNED], True]
    for pay in shapes:
        meta = {"x402/payment": pay}
        assert gate_on._is_signed_payment_attempt(meta) is (extract_payment_from_meta({"_meta": meta}) is not None), pay


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
        ({"x402/payment": {"payload": {"a": 1}}}, []),
        ({"x402/payment": SIGNED}, ["screen_sanctions"]),
        ({"x402/payment": json.dumps(SIGNED)}, ["screen_sanctions"]),
    ):
        alerts.clear()
        with pytest.raises(RuntimeError):
            _run(gate_on.run_paid_tool("screen_sanctions", {}, meta, dispatch))
        assert alerts == expected, f"{meta!r} -> {alerts}"


def test_the_buyer_intent_alert_does_not_claim_the_payment_is_real(gate_on, monkeypatch):
    """THE FINDING (third review, P3). The predicate is the SDK's parse, which accepts any structurally valid
    payload: a forged one with a fake signature (the SIGNED fixture above carries signature '0x01') passes.
    The alert used to say 'a real buyer is here' and 'this is a genuine buyer attempt'. It cannot know that
    before verification, and it is anonymous-reachable, so it says what it knows: an unverified attempt."""
    import billing.telegram_revenue_alerts as alerts_mod
    sent = []

    async def fake_send(text, *a, **k):
        sent.append(text)

    monkeypatch.setattr(alerts_mod, "send_telegram_alert", fake_send)
    gate_on._buyer_intent_last_alert.clear()
    _run(gate_on._notify_buyer_intent("screen_sanctions"))
    assert len(sent) == 1, sent
    text = sent[0].lower()
    assert "unverified" in text, "the alert must say the payment has not been checked"
    for claim in ("real buyer", "genuine"):
        assert claim not in text, f"the alert still claims {claim!r} about an unverified payload"
    assert "screen_sanctions" in sent[0], "the tool is still named"
    # the cooldown is unchanged: a second attempt inside it is silent
    _run(gate_on._notify_buyer_intent("screen_sanctions"))
    assert len(sent) == 1
    gate_on._buyer_intent_last_alert.clear()
