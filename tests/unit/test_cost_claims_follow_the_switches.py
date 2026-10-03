"""What a tool says it COSTS is a function of the money switches, on every surface that says it.

THE DEFECT (review of feat/x402-honesty-20261004, finding F1/F3). With every switch off, which is what
runs on the VPS, the same server contradicted its own discovery descriptor:

  * /.well-known/mcp.json said premium_data_quota_enforced = false (the three premium data tools run free
    and unmetered), while tools/list tagged them "[free in quota, then $0.02/call]" and llms.txt /
    llms-full.txt said "free within the daily quota, then $0.02 per call";
  * the other priced tools were tagged "[$0.05/per_call]" and preview_cost called $0.05 "exact" although
    no rail exists and nothing is charged;
  * with x402 on and metering off, the three data tools were tagged "[or pay per call: x402, ...]" while
    the data-tool bypass returns before the x402 branch, so a payment attached to them is never read.

billing/switches.py is the one place the switches are read; these surfaces call it. Nothing here touches
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
# "free up to the daily quota" is the manifest's cost_model.free_quota_note, which /manifest and llms-full.txt
# serve verbatim; a static note must be true in every state, so it may never state the quota flatly.
QUOTA_PHRASES = ("free in quota", "free within the daily quota", "within the daily quota, then",
                 "free up to the daily quota")
NOT_CHARGED = "not charged while no payment rail is on"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _every_switch_off(monkeypatch):
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


def _descriptions():
    """tools/list through the real JSON-RPC handler, so the test sees what an agent sees."""
    from agent_interface.mcp_server import handle_mcp_request
    resp = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                                   headers={"user-agent": "cost-claims-test"}))
    return {t["name"]: t["description"] for t in resp["result"]["tools"]}


def _cost_tag(desc: str) -> str:
    m = re.search(r"(\[(?:free|from \$|\$|see preview_cost)[^\]]*\])", desc)
    return m.group(1) if m else ""


def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


# ---------------------------------------------------------------- the three premium data tools

def test_metering_off_the_data_tools_are_not_tagged_with_a_quota():
    """THE FINDING. The descriptor says the quota is not enforced; the tag must not promise one."""
    descs = _descriptions()
    for name in DATA_TOOLS:
        tag = _cost_tag(descs[name])
        assert tag == "[free, no key]", f"{name} says {tag!r} while its quota does not exist"


def test_metering_on_the_data_tools_carry_the_quota_tag(monkeypatch):
    _set(monkeypatch, metering=True)
    descs = _descriptions()
    for name in DATA_TOOLS:
        assert re.search(r"\[free in quota, then \$0\.0\d/call[,\]]", descs[name]), (name, descs[name][-80:])


def test_metering_on_with_no_rail_the_quota_tag_still_says_nothing_is_charged(monkeypatch):
    """Past the quota the call is refused, not charged, until a rail exists: the schedule is a schedule."""
    _set(monkeypatch, metering=True)
    assert NOT_CHARGED in _cost_tag(_descriptions()["screen_sanctions"])
    _set(monkeypatch, metering=True, credits=True)
    assert _cost_tag(_descriptions()["screen_sanctions"]) == "[free in quota, then $0.02/call]"


def test_the_tag_and_the_descriptor_cannot_disagree(monkeypatch):
    """Every metering state: quota tag present exactly when the descriptor says the quota is enforced."""
    from agent_interface.well_known import get_mcp_descriptor
    for metering in (False, True):
        _set(monkeypatch, metering=metering)
        enforced = get_mcp_descriptor()["payments"]["premium_data_quota_enforced"]
        descs = _descriptions()
        for name in DATA_TOOLS:
            assert ("free in quota" in descs[name]) is enforced, (name, metering)
        monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)


def test_describe_cost_follows_metering(monkeypatch):
    from agent_interface.well_known import describe_cost
    quota_cost = {"basis": "freemium_daily_quota", "unit_price_usd": 0.02}
    assert describe_cost(quota_cost) == "Cost: free (no key required)."
    _set(monkeypatch, metering=True)
    assert describe_cost(quota_cost) == f"Cost: free within the daily quota, then $0.02 per call ({NOT_CHARGED})."
    _set(monkeypatch, metering=True, credits=True)
    assert describe_cost(quota_cost) == "Cost: free within the daily quota, then $0.02 per call."


def _surfaces_text(client):
    texts = {}
    for path in ("/llms.txt", "/llms-full.txt", "/.well-known/openai-tools.json",
                 "/.well-known/anthropic-tools.json", "/.well-known/agents.json",
                 "/.well-known/agent-card.json", "/.well-known/mcp.json", "/manifest", "/manifest/ops",
                 "/openapi.json", "/openapi.yaml", "/.well-known/agent-service", "/.well-known/agent.json"):
        r = client.get(path)
        if r.status_code == 200:
            texts[path] = r.text
    texts["tools/list"] = json.dumps(_descriptions())
    return texts


def test_metering_off_no_surface_promises_a_quota_on_the_data_tools():
    """The crawl: tools/list, llms.txt, llms-full.txt and every other discovery document."""
    texts = _surfaces_text(_client())
    assert len(texts) >= 8, "too few surfaces answered; the crawl would pass vacuously"
    for path, text in texts.items():
        low = text.lower()
        for phrase in QUOTA_PHRASES:
            assert phrase not in low, f"{path} says {phrase!r} while the quota is not enforced"


def test_metering_on_the_quota_sentence_is_back(monkeypatch):
    _set(monkeypatch, metering=True)
    texts = _surfaces_text(_client())
    assert "free within the daily quota, then $" in texts["/llms.txt"]
    assert "free in quota, then $" in texts["tools/list"]


# ---------------------------------------------------------------- the other priced tools

PRICED = ("capture_lead", "send_message", "schedule_appointment", "escalate_to_human", "call_business")


def test_no_rail_on_a_priced_tool_says_it_is_not_charged():
    descs = _descriptions()
    for name in PRICED:
        tag = _cost_tag(descs[name])
        assert NOT_CHARGED in tag, f"{name}: {tag!r} quotes a price as if it were charged"
        assert "$" in tag, f"{name}: the list price must still be shown"


@pytest.mark.parametrize("combo", [{"credits": True}, {"x402": True}, {"credits": True, "x402": True}])
def test_a_rail_on_the_tag_is_the_plain_price(monkeypatch, combo):
    _set(monkeypatch, **combo)
    descs = _descriptions()
    for name in PRICED:
        assert NOT_CHARGED not in descs[name], (name, combo)
    assert _cost_tag(descs["capture_lead"]) == "[$0.05/per_call]"
    assert _cost_tag(descs["send_message"]).startswith("[from $0.02/call, variable")


def test_the_price_schedule_is_unchanged_by_the_label(monkeypatch):
    """Only the qualifier moves. The dollar figures are the schedule other surfaces and tests rely on."""
    off = _descriptions()["capture_lead"].replace(", " + NOT_CHARGED, "")
    _set(monkeypatch, credits=True)
    on = _descriptions()["capture_lead"]
    assert off == on


def test_describe_cost_says_not_charged_only_while_no_rail_is_on(monkeypatch):
    from agent_interface.well_known import describe_cost
    per_call = {"basis": "per_call", "unit_price_usd": 0.05}
    assert describe_cost(per_call) == f"Cost: $0.05 per call ({NOT_CHARGED})."
    variable = describe_cost({"basis": "per_call_variable", "unit_price_usd": 0.02, "max_price_usd": 0.22})
    assert "up to $0.22" in variable and NOT_CHARGED in variable
    assert describe_cost({"basis": "free", "unit_price_usd": 0.0}).startswith("Cost: free")
    assert NOT_CHARGED not in describe_cost({"basis": "free", "unit_price_usd": 0.0})
    _set(monkeypatch, credits=True)
    assert describe_cost(per_call) == "Cost: $0.05 per call."


def test_preview_cost_keeps_the_schedule_and_says_it_is_not_charged(monkeypatch):
    from core.models import PreviewCostRequest
    from core.preview_cost import handle_preview_cost

    def preview(op):
        return _run(handle_preview_cost(PreviewCostRequest(operation=op, params={})))

    off = preview("capture_lead")
    assert off.estimated_cost_usd == 0.05, "the quoted schedule must not move"
    assert NOT_CHARGED in off.cost_accuracy_slo and off.cost_accuracy_slo.startswith("exact")
    ranged = preview("send_message")
    assert ranged.cost_range["max_usd"] > ranged.cost_range["min_usd"]
    assert NOT_CHARGED in ranged.cost_accuracy_slo
    _set(monkeypatch, credits=True)
    on = preview("capture_lead")
    assert on.estimated_cost_usd == 0.05 and on.cost_accuracy_slo == "exact"
    # a free tool is never labelled
    assert NOT_CHARGED not in preview("self_test").cost_accuracy_slo


# ---------------------------------------------------------------- x402 on, metering off

def test_x402_on_metering_off_the_data_tools_do_not_offer_the_rail(monkeypatch):
    """The gate's bypass answers a premium data tool free BEFORE the x402 branch, so a payment attached
    to one is never read. The tag must not invite it."""
    _set(monkeypatch, x402=True)
    descs = _descriptions()
    for name in DATA_TOOLS:
        assert "x402" not in descs[name].lower(), f"{name} advertises x402 while metering is off"
    assert "x402" in descs["send_message"], "the other paid tools still carry the mention"


def test_x402_on_metering_on_the_data_tools_do_offer_it(monkeypatch):
    _set(monkeypatch, x402=True, metering=True)
    descs = _descriptions()
    for name in DATA_TOOLS:
        assert "x402" in descs[name], name


def test_the_tag_matches_what_the_call_path_does_with_a_payment(monkeypatch):
    """Not just the text: with x402 on and metering off, a call carrying a payment is dispatched free
    and run_paid_tool is never entered; with metering on it is."""
    import agent_interface.mcp_server as ms
    from billing import x402_gate
    _set(monkeypatch, x402=True)
    entered = []
    dispatched = []

    async def fake_run_paid_tool(tool, arguments, meta, dispatch):
        entered.append(tool)
        return {"content": [{"type": "text", "text": "{}"}], "isError": False}

    async def fake_dispatch(name, args, headers=None, skip_auth=False):
        dispatched.append(name)
        return {"status": "success"}

    monkeypatch.setattr(x402_gate, "run_paid_tool", fake_run_paid_tool)
    monkeypatch.setattr(ms, "_dispatch_and_label", fake_dispatch)

    def call():
        return _run(ms.handle_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "screen_sanctions", "arguments": {"name": "Jane Roe"},
                        "_meta": {"x402/payment": {"x402Version": 2, "payload": {"signature": "0x01"}}}}},
            headers={"user-agent": "cost-claims-test"}))

    call()
    assert entered == [] and dispatched == ["screen_sanctions"], (entered, dispatched)
    assert "x402" not in _descriptions()["screen_sanctions"].lower()

    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    entered.clear()
    call()
    assert entered == ["screen_sanctions"]
    assert "x402" in _descriptions()["screen_sanctions"]
