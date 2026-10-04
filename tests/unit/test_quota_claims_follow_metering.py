"""A quota promise is made only while the quota is enforced, and what happens past it is told truthfully.

THE DEFECTS (third review of feat/x402-honesty-20261004, findings P2 #1 and P2 #5). The second pass made
the descriptor say `premium_data_quota_enforced: false` and cleaned the tool tags, but four other
surfaces kept promising a quota while DATA_METERING_ENABLED is off (which is what the VPS runs):

  * GET /keys/request and the 503 text, the MCP `initialize` instructions and the
    `/.well-known/agent-service` text all called the three premium data tools "free within a daily
    quota" (core.tool_auth.free_tier_sentence);
  * the sanctions-screening door's description, served inside /.well-known/mcp.json, in `initialize`
    for that door and in llms.txt, said "free within a daily quota";
  * the descriptor's own `payments.note` said "3 more are callable with no key up to a daily quota,
    then cost credits" and then, two sentences later, that the quota is not enforced.

And in the first-flip state (metering on, credits off, x402 off) the new wording read "free in quota,
then $0.02/call, not charged while no payment rail is on": it tells an agent that calls past the quota
carry on, uncharged. They do not. consume_data_quota refuses them (status failure, reason_code
free_quota_exceeded) and dispatches nothing.

billing/switches.py is the one place the words are chosen; every surface calls it. Nothing here touches
the network.
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

RECEIVER = "0x" + "ab" * 20
DATA_TOOLS = ("verify_company_record", "screen_sanctions", "map_trade_restriction")
# Any of these on a page, while the quota is not enforced, is a promise of a quota that does not exist.
QUOTA_PROMISES = ("daily quota", "free in quota", "free within", "up to a daily", "within the daily")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _every_switch_off(monkeypatch):
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)


def _set(monkeypatch, *, credits=False, metering=False, x402=False):
    # Each call describes the WHOLE state: a switch an earlier call set must not leak into this one.
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    if credits:
        monkeypatch.setenv("CREDITS_ENABLED", "true")
    if metering:
        monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    from billing import x402_gate
    import config
    monkeypatch.setattr(x402_gate, "enabled", lambda: x402)
    if x402:
        monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", RECEIVER)


def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def _rpc(method, params=None, headers=None):
    from agent_interface.mcp_server import handle_mcp_request
    return _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                   headers=headers or {"user-agent": "quota-claims-test"}))


def _promises(text: str) -> list[str]:
    low = text.lower()
    return [p for p in QUOTA_PROMISES if p in low]


def _instructions(**params) -> str:
    base = {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}
    base.update(params)
    return _rpc("initialize", base)["result"]["instructions"]


# ---------------------------------------------------------------- P2 #1: the free-tier sentence

def test_free_tier_sentence_promises_a_quota_only_while_metering_is_on(monkeypatch):
    from core.tool_auth import auth_note, free_tier_sentence
    for fn in (free_tier_sentence, auth_note):
        assert _promises(fn()) == [], f"{fn.__name__} promises a quota while metering is off: {fn()!r}"
    _set(monkeypatch, metering=True)
    for fn in (free_tier_sentence, auth_note):
        assert "free within a daily quota" in fn(), fn.__name__


def test_free_tier_sentence_keeps_its_numbers_in_both_states(monkeypatch):
    from core import tool_auth
    for metering in (False, True):
        if metering:
            _set(monkeypatch, metering=True)
        s = tool_auth.free_tier_sentence()
        assert f"{tool_auth.usable_without_key()} of the {tool_auth.total_tools()} tools" in s
        assert f"{tool_auth.keyless()} always" in s and f"{tool_auth.quota_free()} " in s


def test_keys_request_guidance_follows_metering(monkeypatch):
    off = _client().get("/keys/request")
    assert off.status_code == 200
    assert _promises(off.text) == [], "GET /keys/request promises a quota that is not enforced"
    _set(monkeypatch, metering=True)
    assert "free within a daily quota" in _client().get("/keys/request").text


def test_the_onboarding_503_follows_metering(monkeypatch):
    import agent_interface.key_requests as KR

    async def stored(*a, **k):
        return True

    async def not_sent(email, url):
        return False
    monkeypatch.setattr(KR, "store_pending", stored)
    monkeypatch.setattr(KR, "send_verification_email", not_sent)
    off = _client().post("/keys/request", json={"email": "agent@example.com"})
    assert off.status_code == 503
    assert _promises(off.json()["detail"]) == []
    _set(monkeypatch, metering=True)
    on = _client().post("/keys/request", json={"email": "agent@example.com"})
    assert "free within a daily quota" in on.json()["detail"]


def test_initialize_instructions_follow_metering(monkeypatch):
    assert _promises(_instructions()) == [], "initialize promises a quota that is not enforced"
    _set(monkeypatch, metering=True)
    assert "free within a daily quota" in _instructions()


def test_the_agent_service_text_follows_metering(monkeypatch):
    from agent_interface.well_known import get_llms_txt
    assert _promises(get_llms_txt()) == []
    _set(monkeypatch, metering=True)
    assert "free within" in get_llms_txt()


# ---------------------------------------------------------------- P2 #1: the sanctions door's description

def test_the_profile_description_makes_no_quota_promise_in_any_state(monkeypatch):
    """A static description cannot follow a runtime switch, so it says nothing that depends on one."""
    from agent_interface import profiles
    for pid, spec in profiles.PROFILES.items():
        assert _promises(spec["description"]) == [], pid
        assert len(spec["description"]) <= 100, "the publisher caps a description at 100 characters"
        assert _promises(profiles.describe(pid)["description"]) == [], pid
    for metering in (False, True):
        if metering:
            _set(monkeypatch, metering=True)
        door = _instructions(_profile="sanctions-screening")
        assert _promises(door) == [], (metering, door)
        d = _client().get("/.well-known/mcp.json").json()
        for ep in d["capability_endpoints"]:
            assert _promises(ep["description"]) == [], (metering, ep["name"])


def test_the_descriptor_has_no_quota_promise_while_metering_is_off():
    """The whole descriptor, not one field: it carries premium_data_quota_enforced=false, so no string
    inside the same document may promise the quota."""
    d = _client().get("/.well-known/mcp.json").json()
    assert d["payments"]["premium_data_quota_enforced"] is False
    leaks = [(p, s) for p, s in _strings(d) if _promises(s)]
    assert leaks == [], leaks[:3]


def _strings(node, path="$"):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


# ---------------------------------------------------------------- P2 #1: payments.note

def test_payments_note_makes_no_quota_or_credit_promise_that_is_not_live():
    p = _client().get("/.well-known/mcp.json").json()["payments"]
    note = p["note"].lower()
    assert p["rails"] == [] and p["premium_data_quota_enforced"] is False
    assert _promises(note) == [], _promises(note)
    assert "not enforced" in note, "the note still says, in words, that the quota is not enforced"
    for phrase in ("then cost credits", "spend credits once past", "up to a daily quota, then"):
        assert phrase not in note, phrase
    # the numbers are still there and still the list lengths
    assert f"{len(p['free_tools'])} tools are callable with no key" in note


def test_payments_note_credits_wording_follows_the_credits_rail(monkeypatch):
    _set(monkeypatch, metering=True, credits=True)
    p = _client().get("/.well-known/mcp.json").json()["payments"]
    assert p["rails"] == ["credits"]
    assert "up to a daily quota, then" in p["note"] and "charged" in p["note"]
    _set(monkeypatch, metering=True)
    p = _client().get("/.well-known/mcp.json").json()["payments"]
    assert p["rails"] == []
    assert "refused until" in p["note"], "metering on and no rail: past the quota the call is refused"
    assert "then cost credits" not in p["note"]


# ---------------------------------------------------------------- P2 #5: past the quota with no rail

def _quota_tag(desc: str) -> str:
    m = re.search(r"(\[free in quota[^\]]*\])", desc)
    return m.group(1) if m else ""


def _descriptions():
    resp = _rpc("tools/list")
    return {t["name"]: t["description"] for t in resp["result"]["tools"]}


def test_metering_on_no_rail_the_tag_says_the_call_is_refused_past_the_quota(monkeypatch):
    _set(monkeypatch, metering=True)
    for name, desc in _descriptions().items():
        if name in DATA_TOOLS:
            tag = _quota_tag(desc)
            assert tag == "[free in quota, then refused until the quota resets]", (name, tag)
            assert "not charged" not in tag, "'not charged' reads as 'the call carries on, free'"


def test_metering_on_a_rail_on_the_tag_quotes_the_price(monkeypatch):
    for combo in ({"credits": True}, {"x402": True}, {"credits": True, "x402": True}):
        _set(monkeypatch, metering=True, **combo)
        for name, desc in _descriptions().items():
            if name in DATA_TOOLS:
                assert re.search(r"\[free in quota, then \$0\.02/call", desc), (combo, name)
                assert "refused" not in _quota_tag(desc), (combo, name)


def test_describe_cost_past_the_quota_follows_the_rails(monkeypatch):
    from agent_interface.well_known import describe_cost
    cost = {"basis": "freemium_daily_quota", "unit_price_usd": 0.02}
    _set(monkeypatch, metering=True)
    s = describe_cost(cost)
    assert s == "Cost: free within the daily quota; past it the call is refused until the quota resets.", s
    assert "not charged" not in s
    _set(monkeypatch, metering=True, credits=True)
    assert describe_cost(cost) == "Cost: free within the daily quota, then $0.02 per call."


def test_preview_cost_past_the_quota_follows_the_rails(monkeypatch):
    from core.models import PreviewCostRequest
    from core.preview_cost import handle_preview_cost

    def slo(op="screen_sanctions"):
        return _run(handle_preview_cost(PreviewCostRequest(operation=op, params={}))).cost_accuracy_slo

    _set(monkeypatch, metering=True)
    s = slo()
    assert "refused until the quota resets" in s and "not charged" not in s, s
    _set(monkeypatch, metering=True, credits=True)
    assert slo() == "exact"


def test_the_wording_is_what_the_gate_does_past_the_quota(monkeypatch):
    """Not just text. Past the quota the tool is NOT dispatched and the refusal says free_quota_exceeded,
    which is what 'refused until the quota resets' claims."""
    import agent_interface.mcp_server as ms
    from billing import data_quota as dq
    _set(monkeypatch, metering=True)
    dispatched = []

    async def spent(ip):
        return False, 0

    async def fake_dispatch(name, args, headers=None, skip_auth=False):
        dispatched.append(name)
        return {"status": "success"}

    monkeypatch.setattr(dq, "_consume_anon_data", spent)
    monkeypatch.setattr(ms, "_dispatch_and_label", fake_dispatch)
    resp = _rpc("tools/call", {"name": "screen_sanctions", "arguments": {"name": "Jane Roe"}})
    result = resp["result"]
    body = json.loads(result["content"][0]["text"])
    assert result["isError"] is True and body["reason_code"] == "free_quota_exceeded"
    assert dispatched == [], "a refused call must not run the tool"
    assert body["cost"]["amount"] == 0.0
    assert "refused" in _quota_tag(_descriptions()["screen_sanctions"])


# ---------------------------------------------------------------- the helpers are the only source of the words

def test_switch_helpers_say_the_same_thing_everywhere(monkeypatch):
    from billing import switches
    assert switches.free_quota_clause() == "free and unmetered at this time"
    assert switches.past_quota_tag(0.02) == ""   # no quota enforced: nothing follows a quota that is not there
    _set(monkeypatch, metering=True)
    assert switches.free_quota_clause() == "free within a daily quota"
    assert switches.past_quota_tag(0.02) == "then refused until the quota resets"
    _set(monkeypatch, metering=True, credits=True)
    assert switches.past_quota_tag(0.02) == "then $0.02/call"
    _set(monkeypatch, metering=True, x402=True)
    assert switches.past_quota_tag(0.02) == "then $0.02/call"


# ---------------------------------------------------------------- two invariants only the live check pinned

def test_preview_cost_prices_the_data_tools_only_while_metering_is_on(monkeypatch):
    """THE GAP (mutation testing, third review). `_data_metering_on = True` in core/preview_cost.py made
    preview_cost quote $0.02 for the three premium data tools while metering is off, and no unit test noticed:
    the invariant 'preview_cost == the real charge' was pinned only by scripts/live_verify_release.py."""
    from core.models import PreviewCostRequest
    from core.preview_cost import handle_preview_cost

    def preview(op):
        return _run(handle_preview_cost(PreviewCostRequest(operation=op, params={})))

    for op in DATA_TOOLS:
        off = preview(op)
        assert off.estimated_cost_usd == 0.0 and off.cost_range == {"min_usd": 0.0, "max_usd": 0.0}, op
        assert "not charged" not in off.cost_accuracy_slo and "refused" not in off.cost_accuracy_slo, (
            "a free tool is not labelled with a charge or a refusal")
    _set(monkeypatch, metering=True, credits=True)
    for op in DATA_TOOLS:
        on = preview(op)
        assert on.estimated_cost_usd == 0.02 and on.cost_range["max_usd"] == 0.02, op


def test_the_checkout_write_note_follows_the_rails(monkeypatch):
    """THE GAP. Forcing the write-tool note down its 'beyond that, <rails>.' branch with no rail on printed
    'beyond that, .' on /checkout. Every rail combination is pinned, with its exact tail."""
    def note():
        text = _client().get("/checkout").text
        i = text.index("need a free email-verified key")
        return text[i:i + 220]

    off = note()
    assert "Nothing is charged while no payment rail is switched on." in off, off
    assert "beyond that" not in off
    for combo, tail in (({"credits": True}, "beyond that, credits."), ({"x402": True}, "beyond that, x402."),
                        ({"credits": True, "x402": True}, "beyond that, credits or x402.")):
        _set(monkeypatch, **combo)
        n = note()
        assert tail in n, (combo, n)
        assert "beyond that, ." not in n and "Nothing is charged while no payment rail" not in n


# ---------------------------------------------------------------- the crawl: every page the app serves

# Pages that state history or law, not the current terms. /releases is a dated changelog (an entry from the
# release that introduced the quota says "free within a daily quota"); /refund is the refund policy, which names
# the credit-package window. Neither describes how a call is charged today.
HISTORY_AND_LAW = {"/releases", "/refund"}
_QUOTA_RX = re.compile(r"(?i)free in quota|free within (?:a|the) daily quota|up to (?:a|the) daily quota|"
                       r"within (?:a|the) daily quota")
_CREDITS_RX = re.compile(r"(?i)\(credits\)|buy credits|top up credits|credit packages?|billed per call via credits")


def _every_parameterless_get_page(client):
    import main
    paths = sorted({r.path for r in main.app.routes if "GET" in getattr(r, "methods", set()) and "{" not in r.path})
    pages = {}
    for p in paths:
        resp = client.get(p, follow_redirects=False)
        if resp.status_code == 200:
            pages[p] = resp.text
    return pages


def test_no_served_page_promises_a_quota_or_sells_credits_while_every_switch_is_off():
    """The previous crawl listed thirteen paths and four phrasings, so 'free within a daily quota' (with 'a') on the
    sanctions door's description was never seen. This one walks every parameterless GET route the app has and
    judges them with the same patterns the live check uses."""
    pages = _every_parameterless_get_page(_client())
    assert len(pages) >= 25, f"the crawl reached only {len(pages)} pages; it would pass vacuously"
    quota = sorted(p for p, t in pages.items() if p not in HISTORY_AND_LAW and _QUOTA_RX.search(t))
    credits = sorted(p for p, t in pages.items() if p not in HISTORY_AND_LAW and _CREDITS_RX.search(t))
    assert quota == [], f"these pages promise a quota that is not enforced: {quota}"
    assert credits == [], f"these pages offer credits while credits is not a rail: {credits}"


def test_the_crawl_does_see_the_quota_when_metering_is_on(monkeypatch):
    """The crawl is not blind: switch metering on and the same patterns find the quota on the pages that carry it."""
    _set(monkeypatch, metering=True)
    pages = _every_parameterless_get_page(_client())
    found = sorted(p for p, t in pages.items() if _QUOTA_RX.search(t))
    assert "/llms.txt" in found, found
    assert len(found) >= 2, found
