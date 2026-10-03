"""Every tool that is not production-ready says so, in every place a caller or a catalogue reads, and the
words are true.

A tool list that shows 23 tools as equals claims that all 23 work. They do not: verify_business cannot
verify what find_business returns, schedule_appointment completes for one connected Cal.com account,
mint_key refuses everyone without a shared secret, handle_inbound is substring matching. A reviewer that
runs "every tool" (Claude's directory review does) finds the difference for us.

The facts live in manifest/manifest.json (`readiness` on the operation). These tests check that:
  1. the labels are well-formed and are the set we mean;
  2. tools/list, the generated catalogue, the discovery documents and the README all carry the SAME labels
     (a label typed into four places is wrong in three of them by next month);
  3. the delivery-channel tools get their state from the environment, not from a typed value;
  4. the sentences are TRUE: each limit is demonstrated by calling the tool, not by reading its prose.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from agent_interface import mcp_server as ms
from agent_interface import well_known as wk
from agent_interface.manifest_server import get_full_manifest
from core import channel_status as cs
from core import tool_auth, tool_readiness as tr

ROOT = Path(__file__).resolve().parents[2]
OPS = {o["name"]: o for o in get_full_manifest()["operations"]}

BETA = {"find_business", "capture_lead", "handle_inbound", "escalate_to_human"}
LIMITED = {"verify_business", "schedule_appointment", "import_booking_url", "mint_key"}
PRODUCTION = set(OPS) - BETA - LIMITED


def run(coro):
    return asyncio.run(coro)


def _tools() -> dict:
    """tools/list exactly as a caller gets it (channel notices included), keyed by name."""
    res = run(ms._h_tools_list({}))
    return {t["name"]: t for t in res["tools"]}


@pytest.fixture(autouse=True)
def _no_channels(monkeypatch):
    """The channel tools' state comes from the environment; start every test from 'nothing configured'."""
    for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_MESSAGING_SERVICE_SID",
              "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "RESEND_API_KEY", "SENDGRID_API_KEY", "VAPI_API_KEY",
              "VAPI_PHONE_NUMBER_ID", "VAPI_OUTBOUND_VERIFIED", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_ID",
              "ALLOW_STUB_CHANNELS"):
        monkeypatch.delenv(n, raising=False)


# ---------------------------------------------------------------------------
# 1. the labels
# ---------------------------------------------------------------------------

def test_the_labelled_set_is_the_one_we_mean():
    stored = {n: tr.of(op)["state"] for n, op in OPS.items() if tr.of(op)}
    assert {n for n, s in stored.items() if s == "beta"} == BETA
    assert {n for n, s in stored.items() if s == "limited"} == LIMITED
    assert tr.all_labelled(list(OPS.values())) == stored


def test_a_label_is_a_state_and_one_sentence():
    for name, op in OPS.items():
        rd = tr.of(op)
        if not rd:
            continue
        assert rd["state"] in tr.STORED_STATES, name
        assert 40 <= len(rd["summary"]) <= 330, (name, len(rd["summary"]))
        assert rd["summary"].rstrip().endswith("."), name
        assert "\n" not in rd["summary"]


def test_unavailable_is_never_typed_into_the_manifest():
    """It is a fact about a deployment's environment (core/channel_status.py), so it cannot live in a file."""
    bad = dict(OPS["find_business"], readiness={"state": "unavailable", "summary": "No."})
    with pytest.raises(ValueError):
        tr.of(bad)
    with pytest.raises(ValueError):
        tr.of(dict(OPS["find_business"], readiness={"state": "beta", "summary": " "}))
    with pytest.raises(ValueError):
        tr.of(dict(OPS["find_business"], readiness="beta"))


def test_a_production_ready_tool_carries_no_label_anywhere():
    tools = _tools()
    for name in PRODUCTION - set(cs.CHANNEL_TOOLS):
        assert "_meta" not in tools[name], name
        assert not re.search(r"\[(beta|limited|unavailable)\]", tools[name]["description"]), name


# ---------------------------------------------------------------------------
# 2. one set of labels on every surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(BETA | LIMITED))
def test_tools_list_labels_the_tool_in_the_description_and_in_meta(name):
    t = _tools()[name]
    state = tr.of(OPS[name])["state"]
    assert t["description"].endswith(f" [{state}]"), t["description"][-60:]
    assert t["_meta"][tr.META_KEY] == tr.of(OPS[name])


def test_the_label_never_pushes_a_disclosure_past_the_length_cap():
    """The cap truncates a description at a word; the label is appended AFTER the cap so it cannot be what
    gets cut, and the text before it must itself fit (find_business sat at 449 of 450)."""
    for name in BETA | LIMITED:
        before_tags = ms._format_description_for_llm(OPS[name])
        assert "…" not in before_tags, f"{name}'s description was truncated by the cap"


def test_the_generated_catalogue_carries_the_same_labels():
    cat = {t["name"]: t for t in json.loads((ROOT / "manifest" / "mcp_tools.json").read_text(encoding="utf-8"))}
    for name, op in OPS.items():
        rd = tr.of(op)
        if rd:
            assert cat[name]["description"].endswith(f" [{rd['state']}]"), name
            assert cat[name]["_meta"][tr.META_KEY] == rd, name
        else:
            assert "_meta" not in cat[name], name


def test_the_edge_tools_snapshot_carries_the_same_labels():
    snap = json.loads((ROOT / "edge" / "src" / "snapshots" / "mcp-tools-list.json").read_text(encoding="utf-8"))
    for t in snap["result"]["tools"]:
        rd = tr.of(OPS[t["name"]])
        assert (t.get("_meta", {}).get(tr.META_KEY) == rd), t["name"]


def test_the_mcp_descriptor_lists_the_labelled_tools():
    d = wk.get_mcp_descriptor()
    assert d["tool_readiness"] == {n: tr.of(OPS[n])["state"] for n in OPS if tr.of(OPS[n])}
    assert set(d["tool_readiness"]) == BETA | LIMITED


def _tagged(description: str) -> bool:
    return bool(re.search(r"\[(beta|limited)\]", description))


def test_the_function_calling_catalogues_and_the_agent_cards_carry_the_label():
    for catalogue in (wk.get_openai_tools(), wk.get_anthropic_tools()):
        for t in catalogue["tools"]:
            fn = t.get("function", t)
            rd = tr.of(OPS[fn["name"]])
            assert _tagged(fn["description"]) == (rd is not None), fn["name"]
            if rd:
                assert f"[{rd['state']}]" in fn["description"], fn["name"]
    for card in (wk.get_agents_json(), wk.get_agent_card()):
        for s in card["skills"]:
            rd = tr.of(OPS[s["id"]])
            assert _tagged(s["description"]) == (rd is not None), s["id"]
            if rd:
                assert s["description"].endswith(f"[{rd['state']}]"), s["id"]


def test_llms_txt_states_the_reason_under_each_labelled_tool():
    txt = wk.get_llms_txt()
    for name in BETA | LIMITED:
        rd = tr.of(OPS[name])
        assert f"- **Readiness: {rd['state']}** - {rd['summary']}" in txt, name


def test_the_readme_table_matches_the_manifest():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### Which tools are production-ready"):]
    section = section[:section.index("\n---")]
    rows = {m.group(1): set(re.findall(r"`([a-z_]+)`", m.group(2)))
            for m in re.finditer(r"^\| `(beta|limited)` \|[^|]*\| ([^|]*) \|$", section, re.M)}
    assert rows["beta"] == BETA
    assert rows["limited"] == LIMITED
    assert re.search(r"^\| `unavailable` here \|", section, re.M)


# ---------------------------------------------------------------------------
# 3. delivery channels: the state comes from the environment
# ---------------------------------------------------------------------------

def test_a_tool_whose_channel_is_missing_is_unavailable_with_the_reason():
    t = _tools()["call_business"]
    rd = t["_meta"][tr.META_KEY]
    assert rd["state"] == "unavailable" and rd["summary"] == t["_meta"]["hatchloop/availability"]["reason"]
    assert t["description"].startswith("[UNAVAILABLE on this deployment:")


def test_a_partly_configured_tool_is_beta_not_unavailable(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "1")
    rd = _tools()["send_message"]["_meta"][tr.META_KEY]
    assert rd["state"] == "beta" and "not configured here" in rd["summary"]


def test_a_fully_provisioned_deployment_labels_no_channel_tool(monkeypatch):
    for n, v in {"TWILIO_ACCOUNT_SID": "a", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM_NUMBER": "+1",
                 "RESEND_API_KEY": "k", "VAPI_API_KEY": "k", "VAPI_PHONE_NUMBER_ID": "p",
                 "VAPI_OUTBOUND_VERIFIED": "true", "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_ID": "1"}.items():
        monkeypatch.setenv(n, v)
    tools = _tools()
    assert not any("_meta" in tools[n] for n in cs.CHANNEL_TOOLS)


def test_the_stronger_state_wins_when_two_apply():
    a = {"state": "limited", "summary": "x."}
    assert tr.stronger(a, {"state": "beta", "summary": "y."}) is a
    assert tr.stronger({"state": "beta", "summary": "y."}, {"state": "unavailable", "summary": "z."})["state"] == "unavailable"
    assert tr.stronger(None, a) is a and tr.stronger(a, None) is a


# ---------------------------------------------------------------------------
# 4. the sentences are true: each limit, demonstrated
# ---------------------------------------------------------------------------

def _call(name, args, headers=None):
    resp = run(ms.handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}}, headers or {}))
    assert "result" in resp, resp
    return json.loads(resp["result"]["content"][0]["text"]), resp["result"]["isError"]


def test_verify_business_does_not_know_an_openstreetmap_id():
    body, is_error = _call("verify_business", {"smb_id": "osm:node/1001"})
    assert is_error is True and body["reason_code"] == "supply_unreachable"


def test_capture_lead_refuses_an_openstreetmap_id_and_writes_nothing_for_it():
    body, is_error = _call("capture_lead", {"smb_id": "osm:node/1001", "prospect": {"name": "A", "phone": "+15551230001"}})
    assert is_error is True and body["reason_code"] == "supply_unreachable"


def test_handle_inbound_is_substring_matching_not_understanding():
    """The label says 'a hint, never a decision', because 'stop' anywhere sets the intent."""
    from core.handle_inbound import _classify_intent
    assert _classify_intent("Please STOP by the shop on your way") == "opt_out"
    assert _classify_intent("we had a layover yesterday") == "confirmation", "'yes' inside 'yesterday'"
    assert _classify_intent("kindly cancel") == "cancellation"


def test_mint_key_is_refused_without_a_valid_signature():
    body, is_error = _call("mint_key", {"agent_id": "probe", "timestamp": 1, "nonce": "n", "signature": "00"})
    assert is_error is True
    assert body.get("error") in ("invalid_request", "not_configured")


def test_escalate_to_human_is_a_ticket_write_and_notifies_no_one():
    src = (ROOT / "core" / "escalate_to_human.py").read_text(encoding="utf-8")
    for notifier in ("send_message", "send_email", "telegram", "sms", "webhook", "notify"):
        assert notifier not in src.lower().replace("notification", ""), notifier


def test_capture_lead_notifies_no_one_either():
    src = (ROOT / "core" / "capture_lead.py").read_text(encoding="utf-8")
    for notifier in ("send_message", "send_email", "telegram", "webhook", "notify"):
        assert notifier not in src.lower(), notifier


def test_schedule_appointment_and_import_booking_url_state_the_cal_com_limit():
    for name in ("schedule_appointment", "import_booking_url"):
        d = OPS[name]["description"]
        assert "Cal.com" in d and ("ONE connected" in d or "one connected" in d), name


# ---------------------------------------------------------------------------
# the keyless-count contradiction: mcp.json used to call a tool both free and paid
# ---------------------------------------------------------------------------

def test_mcp_json_puts_every_tool_in_exactly_one_access_list():
    p = wk.get_mcp_descriptor()["payments"]
    lists = {k: set(p[k]) for k in ("free_tools", "quota_free_tools", "free_with_key_tools", "paid_tools")}
    seen: dict = {}
    for k, names in lists.items():
        for n in names:
            assert n not in seen, f"{n} is in both {seen[n]} and {k}"
            seen[n] = k
    assert set(seen) == set(OPS), set(OPS) ^ set(seen)


def test_the_three_quota_tools_are_not_listed_as_paid():
    p = wk.get_mcp_descriptor()["payments"]
    for n in ("screen_sanctions", "verify_company_record", "map_trade_restriction"):
        assert n in p["quota_free_tools"] and n not in p["paid_tools"]
        assert n in p["spends_credits_once_past_quota"], "the other question is still answerable"


def test_the_lists_agree_with_core_tool_auth():
    p = wk.get_mcp_descriptor()["payments"]
    assert len(p["free_tools"]) == tool_auth.keyless()
    assert len(p["quota_free_tools"]) == tool_auth.quota_free()
    assert len(p["free_tools"]) + len(p["quota_free_tools"]) == tool_auth.usable_without_key()
    assert len(p["free_with_key_tools"]) + len(p["paid_tools"]) == tool_auth.needs_key()
    assert len(p["free_tools"]) + len(p["free_with_key_tools"]) == tool_auth.costs_nothing()


def test_the_note_numbers_are_the_list_lengths():
    p = wk.get_mcp_descriptor()["payments"]
    note = p["note"]
    assert f"{len(p['free_tools'])} tools are callable with NO key" in note
    assert f"{len(p['quota_free_tools'])} more are callable with no key up to a daily quota" in note
    assert f"That is {tool_auth.usable_without_key()} usable without signing up" in note
    assert f"The remaining {tool_auth.needs_key()} need a free key" in note
    assert f"{len(p['spends_credits_once_past_quota'])} spend credits once past any quota" in note
