"""Everything that tells an agent how it can pay is a pure function of the money switches.

THE DEFECT (measured 2026-10-03/04). In the running container CREDITS_ENABLED and
DATA_METERING_ENABLED are both "false" and X402_ENABLED is not set. Yet:

  * /.well-known/mcp.json said payments.status = "active" and rails = ["credits"], literals
    in agent_interface/well_known.py;
  * the auth_required message, the free-tier-limit message and the key-request guidance told
    callers to buy credit packages or pay by x402;
  * the premium-data note promised "a daily quota, then cost credits" for tools that run
    free and unmetered while metering is off.

billing/switches.py is the one place the switches are read; the gates and the advertisers
both call it. This file pins both directions (switch off -> not advertised, switch on ->
advertised) for every surface, plus the shape of the descriptor in every combination.
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


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _every_switch_off(monkeypatch):
    """Start from the live configuration: credits off, metering off, x402 off."""
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)


def _set(monkeypatch, *, credits=False, metering=False, x402=False):
    if credits:
        monkeypatch.setenv("CREDITS_ENABLED", "true")
    if metering:
        monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    from billing import x402_gate
    import config
    monkeypatch.setattr(x402_gate, "enabled", lambda: x402)
    if x402:
        monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", RECEIVER)


def _payments():
    from agent_interface.well_known import get_mcp_descriptor
    return get_mcp_descriptor()["payments"]


# ---------------------------------------------------------------------------- the module

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("True", True), ("TRUE", True), ("yes", True), ("YES", True),
    ("", False), ("0", False), ("false", False), ("no", False), ("on", False), ("2", False),
])
def test_switch_truthiness_is_the_one_the_gates_always_used(monkeypatch, value, expected):
    from billing import switches
    monkeypatch.setenv("CREDITS_ENABLED", value)
    monkeypatch.setenv("DATA_METERING_ENABLED", value)
    assert switches.credits_enabled() is expected
    assert switches.data_metering_enabled() is expected


def test_an_absent_switch_is_off(monkeypatch):
    from billing import switches
    assert switches.credits_enabled() is False
    assert switches.data_metering_enabled() is False


def test_the_switch_is_read_at_call_time_not_import_time(monkeypatch):
    from billing import switches
    assert switches.credits_enabled() is False
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    assert switches.credits_enabled() is True
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    assert switches.credits_enabled() is False


@pytest.mark.parametrize("credits,x402,rails,status", [
    (False, False, [], "not_enabled"),
    (True, False, ["credits"], "active"),
    (False, True, ["x402"], "active"),
    (True, True, ["credits", "x402"], "active"),
])
def test_rails_and_status_follow_the_switches(monkeypatch, credits, x402, rails, status):
    from billing import switches
    _set(monkeypatch, credits=credits, x402=x402)
    assert switches.live_rails() == rails
    assert switches.payments_status() == status


def test_the_x402_answer_is_the_gates_own(monkeypatch):
    """The gate needs a receiver and CDP credentials as well as the flag; this must say what it says."""
    from billing import switches, x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    assert switches.x402_enabled() is True
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)
    assert switches.x402_enabled() is False

    def boom():
        raise RuntimeError("gate import failed")
    monkeypatch.setattr(x402_gate, "enabled", boom)
    assert switches.x402_enabled() is False, "a status read must never raise"


# ---------------------------------------------------------------------------- the descriptor

def test_the_live_configuration_is_not_advertised_as_active(monkeypatch):
    """THE FINDING. All three switches off (what runs on the VPS) -> not 'active', no rails."""
    p = _payments()
    assert p["status"] != "active", "payments advertised as active with every rail off"
    assert p["status"] == "not_enabled"
    assert p["rails"] == []
    assert "x402" not in json.dumps(p).lower()
    assert p["premium_data_quota_enforced"] is False
    # said in words too, because an agent may read only the note
    assert "No payment rail is switched on" in p["note"]
    assert "not enforced" in p["note"]


def test_credits_on_makes_the_descriptor_active_with_the_credits_rail(monkeypatch):
    _set(monkeypatch, credits=True)
    p = _payments()
    assert p["status"] == "active" and p["rails"] == ["credits"]
    assert "No payment rail is switched on" not in p["note"]


def test_x402_alone_is_active_and_says_credits_are_off(monkeypatch):
    _set(monkeypatch, x402=True)
    p = _payments()
    assert p["status"] == "active" and p["rails"] == ["x402"]
    assert "Credits are not switched on" in p["note"]
    assert "x402 payment" in p["note"]


def test_both_rails_on(monkeypatch):
    _set(monkeypatch, credits=True, x402=True)
    p = _payments()
    assert p["status"] == "active" and p["rails"] == ["credits", "x402"]
    assert "Credits are not switched on" not in p["note"]


def test_metering_is_reported_as_what_it_is(monkeypatch):
    off = _payments()
    assert off["premium_data_quota_enforced"] is False and "not enforced" in off["note"]
    _set(monkeypatch, metering=True)
    on = _payments()
    assert on["premium_data_quota_enforced"] is True and "not enforced" not in on["note"]


def test_the_access_lists_do_not_move_with_the_switches(monkeypatch):
    """The partition of tools is a classification other surfaces and tests rely on. The switches change
    what is TRUE about charging, not which list a tool is in."""
    keys = ("free_tools", "quota_free_tools", "free_with_key_tools", "paid_tools",
            "spends_credits_once_past_quota", "unit")
    base = {k: _payments()[k] for k in keys}
    for combo in ({"credits": True}, {"metering": True}, {"x402": True},
                  {"credits": True, "metering": True, "x402": True}):
        _set(monkeypatch, **combo)
        assert {k: _payments()[k] for k in keys} == base, combo


def test_the_agent_card_carries_the_same_block(monkeypatch):
    from agent_interface import well_known as wk
    for combo in ({}, {"credits": True}, {"x402": True}):
        _set(monkeypatch, **combo)
        assert wk.get_agent_card()["_meta"]["payments"] == _payments()


# ---------------------------------------------------------------------------- the auth_required text

@pytest.fixture
def storefront(monkeypatch):
    """The production shape: write tools need a key and the Polar checkout URL is set."""
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.setenv("POLAR_CHECKOUT_URL", "https://polar.example/checkout")


def _auth_required():
    from agent_interface.mcp_server import handle_mcp_request
    resp = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "send_message", "arguments": {}}},
        headers={"user-agent": "payments-advertising-test"}))
    result = resp["result"]
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["error_code"] == "auth_required", body
    return body


def _everything_said(body):
    return (body["human_message"] + json.dumps(body["how_to_resolve"])).lower()


def test_no_rail_on_the_auth_required_message_offers_only_the_free_key(storefront):
    body = _auth_required()
    said = _everything_said(body)
    assert "Option 1 (free)" in body["human_message"]
    assert "credit" not in said, "told an agent to buy credits while the credits gate is off"
    assert "x402" not in said and "usdc" not in said
    assert "option 2" not in said
    assert list(body["how_to_resolve"]) == ["free_key", "header"]
    # the sentence about emailed tokens must name only the option that exists
    assert "Option 1 emails you" in body["human_message"]


def test_credits_on_the_message_offers_credits_as_option_two(monkeypatch, storefront):
    _set(monkeypatch, credits=True)
    body = _auth_required()
    assert "Option 2 (credits)" in body["human_message"]
    assert "Option 3" not in body["human_message"]
    assert "credits" in body["how_to_resolve"] and "x402" not in body["how_to_resolve"]
    assert "Options 1 and 2 email you" in body["human_message"]


def test_x402_alone_is_option_two_and_credits_are_absent(monkeypatch, storefront):
    _set(monkeypatch, x402=True)
    body = _auth_required()
    assert "Option 2 (pay per call, no signup)" in body["human_message"]
    assert "(credits)" not in body["human_message"]
    assert "credits" not in body["how_to_resolve"] and "x402" in body["how_to_resolve"]
    assert "Option 1 emails you" in body["human_message"]


def test_both_rails_on_the_options_are_numbered_one_two_three(monkeypatch, storefront):
    _set(monkeypatch, credits=True, x402=True)
    body = _auth_required()
    text = body["human_message"]
    assert text.index("Option 1 (free)") < text.index("Option 2 (credits)") < text.index(
        "Option 3 (pay per call, no signup)")
    assert "credits" in body["how_to_resolve"] and "x402" in body["how_to_resolve"]


def test_without_a_checkout_url_credits_are_still_only_named_when_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.delenv("POLAR_CHECKOUT_URL", raising=False)
    off = _auth_required()
    assert "credit" not in _everything_said(off)
    _set(monkeypatch, credits=True)
    on = _auth_required()
    assert "Credit packages from $9" in on["human_message"]


# ---------------------------------------------------------------------------- the free-tier limit text

def _limit_body(key_id):
    """A REAL free key whose real in-memory daily allowance is used up (nothing is stubbed)."""
    from agent_interface.identity import issue_token, TokenRequest
    from agent_interface.key_request_logic import FREE_TIER_DAILY_LIMIT, consume_free_daily
    from agent_interface.mcp_server import handle_mcp_request
    token = issue_token(TokenRequest(
        agent_id=key_id, principal_id="test_user_001", principal_type="human",
        allowed_operations=["*"], budget_cap_usd=0.0)).token
    for _ in range(FREE_TIER_DAILY_LIMIT):
        assert consume_free_daily(key_id)
    resp = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "send_message", "arguments": {}}},
        headers={"x-agent-identity": token}))
    assert resp["result"]["isError"] is True
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["error_code"] == "rate_limited"
    return body


def test_the_daily_limit_message_points_at_credits_only_when_they_exist(monkeypatch):
    off = _limit_body("free_switchtest_off")
    said = (off["human_message"] + json.dumps(off["how_to_resolve"])).lower()
    assert "credit" not in said and "pricing" not in said and "upgrade" not in said
    assert "wait_until" in off["how_to_resolve"], "the reset time is still given"
    assert off["retry_after_ms"] > 0
    _set(monkeypatch, credits=True)
    on = _limit_body("free_switchtest_on")
    assert "Buy credits at https://hatchloop.dev/pricing" in on["human_message"]
    assert on["how_to_resolve"]["upgrade"] == "https://hatchloop.dev/pricing"


# ---------------------------------------------------------------------------- the key-request guidance

def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def test_key_guidance_names_x402_only_when_the_rail_is_on(monkeypatch):
    r = _client().get("/keys/request")
    assert r.status_code == 200
    text = r.text.lower()
    assert "x402" not in text and "usdc" not in text
    alt = r.json()["no_email_available"]["alternative"]
    assert alt and "hello@hatchloop.dev" in alt, "an inbox-less agent is still told what to do"
    _set(monkeypatch, x402=True)
    on = _client().get("/keys/request").json()["no_email_available"]["alternative"]
    assert "x402" in on and "USDC on Base" in on


def test_the_onboarding_503_names_x402_only_when_the_rail_is_on(monkeypatch):
    import agent_interface.key_requests as KR

    async def stored(*a, **k):
        return True

    async def not_sent(email, url):
        return False
    monkeypatch.setattr(KR, "store_pending", stored)
    monkeypatch.setattr(KR, "send_verification_email", not_sent)

    def post():
        return _client().post("/keys/request", json={"email": "agent@example.com"})

    off = post()
    assert off.status_code == 503
    assert "x402" not in off.text.lower() and "usdc" not in off.text.lower()
    assert "hello@hatchloop.dev" in off.json()["detail"]
    _set(monkeypatch, x402=True)
    on = post()
    assert on.status_code == 503 and "x402" in on.json()["detail"]


# ---------------------------------------------------------------------------- the /checkout page

def test_checkout_page_names_x402_only_when_the_rail_is_on(monkeypatch):
    off = _client().get("/checkout")
    assert off.status_code == 200
    low = off.text.lower()
    assert "x402" not in low and "usdc" not in low
    assert "Two rails" not in off.text, "one rail is not two"
    assert "Credit packages" in off.text, "the card checkout is untouched"
    _set(monkeypatch, x402=True)
    on = _client().get("/checkout").text
    assert "x402" in on and "USDC on Base" in on and "Two rails" in on


# ---------------------------------------------------------------------------- the crawl

# Every discovery document and public page this service serves that is meant to tell an agent or a buyer
# how to pay. Legal text (/terms /privacy /refund) is excluded on purpose: a term of service may name a rail
# conditionally and is replaced by its own review; /releases is a changelog of what was true when written.
_SURFACES = (
    "/.well-known/agent-card.json", "/.well-known/agent-service", "/.well-known/agent.json",
    "/.well-known/agents.json", "/.well-known/ai-plugin.json", "/.well-known/anthropic-tools.json",
    "/.well-known/mcp.json", "/.well-known/openai-tools.json", "/checkout", "/llms-full.txt",
    "/llms.txt", "/manifest", "/manifest/ops", "/openapi.json", "/openapi.yaml", "/status",
    "/supply/platforms", "/compliance/jurisdictions",
)


def test_with_the_rail_off_no_served_surface_mentions_x402(monkeypatch):
    c = _client()
    served = []
    for path in _SURFACES:
        r = c.get(path)
        if r.status_code != 200:
            continue
        served.append(path)
        low = r.text.lower()
        assert "x402" not in low, f"{path} advertises x402 while the gate is off"
        assert "usdc" not in low, f"{path} advertises USDC while the gate is off"
    assert len(served) >= 12, f"only {len(served)} surfaces answered; the crawl would pass vacuously"
    assert c.get("/.well-known/x402").status_code == 404


def test_with_the_rail_off_the_mcp_surface_does_not_mention_it_either(monkeypatch):
    from agent_interface.mcp_server import handle_mcp_request
    for method in ("initialize", "tools/list"):
        params = {"protocolVersion": "2025-06-18", "capabilities": {},
                  "clientInfo": {"name": "t", "version": "1"}} if method == "initialize" else {}
        resp = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                       headers={"user-agent": "payments-advertising-test"}))
        low = json.dumps(resp).lower()
        assert "x402" not in low and "usdc" not in low, method
