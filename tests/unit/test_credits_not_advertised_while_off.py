"""No surface offers credits while the credits gate is off (review of feat/x402-honesty-20261004, F5).

The first pass fixed the descriptor, the auth_required text, /keys/request and /checkout. These still sold
credits with CREDITS_ENABLED off, found by reading every place a "buy / top up credits" sentence is written:

  * the page a new free-key holder is shown on GET /keys/verify ("Need more? Buy credits - Starter $9 ...");
  * the past-quota messages of the three premium data tools ("or top up credits at /pricing"), reachable
    in the state the config comments call the first flip (metering on, credits still off);
  * the OAuth consent page ("Spend credits on your account ... Credits are bought on hatchloop.dev");
  * smithery.yaml and glama.json, which registries scrape and republish ("or buy credits at ...").

The first three are functions of billing.switches.credits_enabled(). The last two are static files: a file
cannot follow a switch, so they say nothing about credits at all and point at the live descriptor, which can.
Nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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


CREDIT_SELLING = re.compile(r"(?i)\b(buy|top[ -]?up|purchase)\s+(more\s+)?credits?\b|credit packages?|starter \$9")


# ---------------------------------------------------------------- the free-key success page

def _success():
    from agent_interface.key_request_logic import html_success
    return html_success("tok-123", "2027-01-01T00:00:00Z", "free_abc", "https://hatchloop.dev/pricing")


def test_the_free_key_page_does_not_sell_credits_while_they_are_off():
    page = _success()
    assert not CREDIT_SELLING.search(page), CREDIT_SELLING.search(page).group(0)
    assert "Your free API key is ready" in page and "tok-123" in page, "the key itself is still shown"
    assert "100 gated operations per day" in page


def test_the_free_key_page_offers_credits_when_they_are_on(monkeypatch):
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    page = _success()
    assert "Buy credits" in page and "Starter $9/1,000 ops" in page


# ---------------------------------------------------------------- the premium-data past-quota messages

@pytest.fixture
def exhausted(monkeypatch):
    """Both callers' quotas are used up; nothing else is stubbed that matters to the wording."""
    from billing import data_quota as dq
    state = {"free": True}
    monkeypatch.setattr(dq, "_resolve_key_id", lambda token: "free_k" if state["free"] else None)
    monkeypatch.setattr(dq, "_is_free_tier_key", lambda key: state["free"])
    monkeypatch.setattr(dq, "_consume_free_key_data", lambda key: (False, 0))

    async def anon(ip):
        return False, 0
    monkeypatch.setattr(dq, "_consume_anon_data", anon)
    return state


def _refusal(state, *, free):
    from billing import data_quota as dq
    state["free"] = free
    out = _run(dq.consume_data_quota("screen_sanctions", token="tok" if free else "", ip="1.2.3.4"))
    assert out["allowed"] is False
    return out["response"]


@pytest.mark.parametrize("free", [True, False])
def test_the_past_quota_message_names_credits_only_while_they_are_on(monkeypatch, exhausted, free):
    off = _refusal(exhausted, free=free)
    assert off["reason_code"] == "free_quota_exceeded" and off["cost"]["amount"] == 0.0
    assert "credit" not in off["human_message"].lower(), off["human_message"]
    assert "https://hatchloop.dev/agent-broker" in off["human_message"], "the free-key pointer remains"
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    on = _refusal(exhausted, free=free)
    assert "top up credits at https://hatchloop.dev/pricing" in on["human_message"]


# ---------------------------------------------------------------- the OAuth consent page

def test_the_consent_page_mentions_credits_only_while_they_are_on(monkeypatch):
    from agent_interface.oauth import pages
    off = pages.what_it_allows()
    assert "credit" not in off.lower()
    assert "free tools" in off and "does not see your email" in off
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    on = pages.what_it_allows()
    assert "Spend credits on your account" in on and "never inside the assistant" in on


# ---------------------------------------------------------------- the static registry files

def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("rel", ["smithery.yaml", "glama.json", "server.json", "registry/servers.yaml"])
def test_static_registry_copy_does_not_offer_credits_or_a_quota(rel):
    """A static file cannot follow a switch, so it must be true in every state: no credits offer, no promise
    of a quota on the premium data tools (it does not exist while metering is off)."""
    text = _read(rel)
    assert not CREDIT_SELLING.search(text), f"{rel}: {CREDIT_SELLING.search(text).group(0)!r}"
    assert "free within a daily quota" not in text, f"{rel} promises a quota that exists only while metering is on"


@pytest.mark.parametrize("rel", ["smithery.yaml", "glama.json"])
def test_the_registry_auth_note_points_at_the_live_descriptor(rel):
    text = _read(rel)
    assert "https://api.hatchloop.dev/.well-known/mcp.json" in text
    assert "a free email-verified key gives 100" in text.replace("\n", " ").replace("  ", " ")
    if rel == "glama.json":
        doc = json.loads(text)
        assert "payments" in doc["auth"]["note"]


# ---------------------------------------------------------------- the crawl: every served surface, switches off

def test_no_served_surface_sells_credits_while_the_gate_is_off(monkeypatch):
    """The crawl that would have found all of the above: GET every discovery document and public page, and
    call the MCP surface, with every switch off."""
    from fastapi.testclient import TestClient
    import main
    from agent_interface.mcp_server import handle_mcp_request
    c = TestClient(main.app, raise_server_exceptions=False)
    paths = ("/.well-known/agent-card.json", "/.well-known/agent-service", "/.well-known/agent.json",
             "/.well-known/agents.json", "/.well-known/ai-plugin.json", "/.well-known/anthropic-tools.json",
             "/.well-known/mcp.json", "/.well-known/openai-tools.json", "/checkout", "/llms-full.txt",
             "/llms.txt", "/manifest", "/manifest/ops", "/openapi.json", "/openapi.yaml", "/status",
             "/keys/request", "/supply/platforms", "/compliance/jurisdictions")
    texts = {}
    for p in paths:
        r = c.get(p)
        if r.status_code == 200:
            texts[p] = r.text
    for method in ("initialize", "tools/list"):
        params = ({"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}} if method == "initialize" else {})
        resp = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                       headers={"user-agent": "credits-crawl-test"}))
        texts["mcp:" + method] = json.dumps(resp)
    assert len(texts) >= 12, f"only {len(texts)} surfaces answered; the crawl would pass vacuously"
    # The descriptor is allowed to NAME credits as the unit of the price schedule and to say they are off;
    # what no surface may do is OFFER them for sale. Offers are the sell-verbs and the package names.
    offenders = {}
    for path, text in texts.items():
        m = CREDIT_SELLING.search(text)
        if m:
            offenders[path] = text[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
    assert not offenders, offenders
