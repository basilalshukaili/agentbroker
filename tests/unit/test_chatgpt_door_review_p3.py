"""The smaller findings (P3) of the review of the ChatGPT door, fixed in the same pass as the P1/P2 ones.

See test_chatgpt_door_review_fixes.py for the P1/P2 ones. Each test here was run against the unfixed tree and seen to
fail, and each is paired with a control showing the thing it guards is really there.

  P3a  the abuse ceiling: an address that cannot be determined is not counted (the doc says so); the table of addresses
       is bounded; the setting is parsed without crashing; the retry hint has a floor
  P3b  our own phrases are reworded OUTSIDE [UNTRUSTED] fences only, so a sanctions-list name is never altered
  P3c  a tool the door lacks is refused by the door, not first by the idempotency gate (whose text says "charge")
  P3d  an unknown JSON-RPC method is not echoed back on the door
  P3e  the post-deploy check of the door is as strict as the door: exact annotation values, the declared outputSchema
       actually validated, server/discover called, the llms.txt status read, the caller-chosen-door hole probed
  P3f  one exact allow-list for handler prose, shared by the unit test and the CI gate
  P3g  the CI gate covers the dispatcher's own envelope text, not only the projected results
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import mcp_server, no_commerce  # noqa: E402
from agent_interface.mcp_server import handle_mcp_request  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "chatgpt_door")


def _fixture(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _run(coro):
    return asyncio.run(coro)


def _rpc(method, params, profile, headers=None):
    return _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=headers or {}, profile=profile))


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for var in ("DATA_METERING_ENABLED", "CREDITS_ENABLED", "CHATGPT_DOOR_DAILY_CEILING",
                "CHATGPT_DOOR_RATE_BURST", "CHATGPT_DOOR_RATE_PER_S"):
        monkeypatch.delenv(var, raising=False)
    no_commerce.reset_ceiling_for_tests()


# ---------------------------------------------------------------------------------------------------------------
# P3a - the ceiling
# ---------------------------------------------------------------------------------------------------------------

def test_P3a_an_address_that_cannot_be_determined_is_not_counted(monkeypatch):
    """The doc says so. resolve_client_ip returns the literal "unknown" when nothing identifies the caller; counted,
    every such caller shared one bucket and was locked out together at the ceiling."""
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", "3")
    for who in ("unknown", "UNKNOWN", "", None):
        for _ in range(6):
            assert no_commerce.consume_ceiling(who) is None, who
    # control: a real address IS counted, and refused on the fourth call
    assert [no_commerce.consume_ceiling("192.0.2.9") for _ in range(3)] == [None, None, None]
    assert no_commerce.consume_ceiling("192.0.2.9") is not None


def test_P3a2_the_table_of_addresses_cannot_grow_without_bound(monkeypatch):
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", "1")
    monkeypatch.setattr(no_commerce, "_MAX_ADDRESSES", 2)
    assert no_commerce.consume_ceiling("192.0.2.1") is None
    assert no_commerce.consume_ceiling("192.0.2.2") is None
    for _ in range(5):            # the third distinct address is not tracked, so it is never refused
        assert no_commerce.consume_ceiling("192.0.2.3") is None
    assert len(no_commerce._counts) == 2
    assert no_commerce.consume_ceiling("192.0.2.1") is not None      # control: the tracked ones are still limited


@pytest.mark.parametrize("raw,expect_off", [("-5", True), ("0", True), ("abc", False), ("", False)])
def test_P3a3_the_ceiling_setting_is_parsed_without_ever_crashing(monkeypatch, raw, expect_off):
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", raw)
    assert no_commerce.ceiling() == (0 if expect_off else no_commerce.default_ceiling())


def test_P3a4_the_retry_hint_never_drops_below_a_second():
    from datetime import datetime, timedelta, timezone
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    stamp, ms = no_commerce._reset_stamp(yesterday)         # a reset time already in the past
    assert ms == 1000 and stamp.endswith("T00:00:00Z")


def test_P3a5_the_rate_bucket_setting_is_parsed_without_ever_removing_the_limit(monkeypatch):
    assert no_commerce.rate_bucket() == (150.0, 2.0)
    monkeypatch.setenv("CHATGPT_DOOR_RATE_BURST", "-3")
    monkeypatch.setenv("CHATGPT_DOOR_RATE_PER_S", "not-a-number")
    assert no_commerce.rate_bucket() == (150.0, 2.0)
    monkeypatch.setenv("CHATGPT_DOOR_RATE_BURST", "40")
    monkeypatch.setenv("CHATGPT_DOOR_RATE_PER_S", "2.5")
    assert no_commerce.rate_bucket() == (40.0, 2.5)


# ---------------------------------------------------------------------------------------------------------------
# P3b - fenced text is never reworded
# ---------------------------------------------------------------------------------------------------------------

def test_P3b_third_party_text_inside_a_fence_is_never_reworded():
    fenced = ("MATCH: [UNTRUSTED]Entity found in these free registries[/UNTRUSTED] and "
              "[UNTRUSTED]Alpha Ltd and nothing was charged.[/UNTRUSTED]")
    assert no_commerce.clean_text(fenced) == fenced
    # control: the same words in OUR sentence, outside a fence, are reworded
    assert no_commerce.clean_text("Entity found in these free registries.") == "Entity found in these public registries."
    assert no_commerce.clean_text("Nothing ran and nothing was charged.") == "Nothing ran."
    # both at once: ours is reworded, theirs is not
    mixed = ("Not registered with these free registries: [UNTRUSTED]found in these free registries[/UNTRUSTED]. "
             "Nothing was screened and nothing was charged.")
    assert no_commerce.clean_text(mixed) == (
        "Not registered with these public registries: [UNTRUSTED]found in these free registries[/UNTRUSTED]. "
        "Nothing was screened.")
    # an opener with no closer fences the rest: it can leave text alone, never alter it
    assert no_commerce.clean_text("x [UNTRUSTED]found in these free registries") == \
        "x [UNTRUSTED]found in these free registries"


def test_P3b2_a_fence_scan_is_linear_on_many_unclosed_openers():
    text = "[UNTRUSTED]" * 20000
    t0 = time.perf_counter()
    assert no_commerce.clean_text(text) == text
    assert time.perf_counter() - t0 < 0.5


def test_P3b3_through_the_door_a_fenced_name_in_a_message_survives(monkeypatch):
    data = _fixture("screen_hit")
    raw = copy.deepcopy(data["receipt"])
    raw["human_message"] = "MATCH FOUND: [UNTRUSTED]Entity found in these free registries[/UNTRUSTED]"

    async def fake(name, args, headers=None, skip_auth=False):
        return copy.deepcopy(raw)
    monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
    resp = _rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]}, "chatgpt")
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["human_message"] == raw["human_message"]


# ---------------------------------------------------------------------------------------------------------------
# P3c - the idempotency gate does not answer for the door
# ---------------------------------------------------------------------------------------------------------------

def test_P3c_a_tool_the_door_lacks_does_not_reach_the_idempotency_gate(monkeypatch):
    from agent_interface import idempotency_gate
    claimed = []

    async def claim(scope, name, key):
        claimed.append(name)
        return "in_progress", {}
    monkeypatch.setattr(idempotency_gate, "claim", claim)
    params = {"name": "send_message", "arguments": {"idempotency_key": "k1"}}
    door = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
                                   headers={"x-agent-identity": "some-token"}, profile="chatgpt"))
    text = json.dumps(door)
    assert not claimed, "the door's refusal was preceded by the idempotency gate"
    assert "not available on this endpoint" in text and "charge" not in text.lower()
    # control: on a Claude-facing door the same call is unchanged, i.e. it still goes through the gate first.
    # That is the behaviour this fix deliberately leaves alone.
    params = {"name": "send_message", "arguments": {"idempotency_key": "k2"}}
    _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
                            headers={"x-agent-identity": "some-token"}, profile="sanctions-screening"))
    assert claimed == ["send_message"]


# ---------------------------------------------------------------------------------------------------------------
# P3d - no echo of an unknown method
# ---------------------------------------------------------------------------------------------------------------

def test_P3d_an_unknown_method_is_not_echoed_on_the_door():
    method = "preview_cost https://hatchloop.dev/pricing"
    door = _rpc(method, {}, "chatgpt")
    assert door["error"]["message"] == no_commerce.METHOD_NOT_FOUND
    assert not no_commerce.FORBIDDEN_RE.search(json.dumps(door))
    # control: the Claude-facing doors still echo it (this is why the door needed its own wording)
    assert method in _rpc(method, {}, "sanctions-screening")["error"]["message"]


# ---------------------------------------------------------------------------------------------------------------
# P3f / P3g - the gate
# ---------------------------------------------------------------------------------------------------------------

def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_P3f_handler_prose_is_checked_against_one_exact_allow_list():
    """The unit test used to accept any literal that was a SUBSTRING of an allowed sentence, so a new literal
    "nothing was charged" passed it and only the CI gate caught it. There is one list now, matched exactly."""
    gate = _load("check_no_commerce_door_p3", "scripts/check_no_commerce_door.py")
    assert gate.handler_literals() == []
    # control: a literal that is only a SUBSTRING of an allowed sentence is an offender under exact membership
    import ast
    import tempfile
    src = 'MESSAGE = "Nothing was screened on this call and nothing was charged."\n'
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "core"))
        for rel in gate.HANDLERS:
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write(src if rel.endswith("verify_company_record.py") else "X = 1\n")
        old_root = gate.ROOT
        gate.ROOT = d
        try:
            found = gate.handler_literals()
        finally:
            gate.ROOT = old_root
    assert found and "verify_company_record.py" in found[0], found
    ast.parse(src)


def test_P3g_the_gate_covers_the_envelope_surfaces_and_passes():
    gate = _load("check_no_commerce_door_p3g", "scripts/check_no_commerce_door.py")
    surfaces = gate.door_surfaces()
    for needed in ("unknown method (caller's text must not be echoed)", "a bad key (no key-issuance warning)",
                   "an over-long name refused", "tools/list in the 2026-07-28 envelope",
                   "a write tool the door lacks, with a key and an idempotency key"):
        assert needed in surfaces, needed
    assert all(gate.hits(s) == [] for s in surfaces.values())


def test_P3g2_the_gate_fails_when_the_unknown_method_is_echoed(monkeypatch):
    """Teeth: put the old echo back and the gate's new surface must light up."""
    gate = _load("check_no_commerce_door_p3g2", "scripts/check_no_commerce_door.py")
    monkeypatch.setattr(no_commerce, "METHOD_NOT_FOUND", "Method 'preview_cost https://hatchloop.dev/pricing' not found")
    found = gate.hits(gate.door_surfaces()["unknown method (caller's text must not be echoed)"])
    assert found, "the gate did not notice commerce wording in the unknown-method answer"


# ---------------------------------------------------------------------------------------------------------------
# P3e - the post-deploy check, run offline against the in-process app
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    """scripts/live_verify_release.check_chatgpt_door with its HTTP helper pointed at the in-process app and the one
    upstream call replaced by a captured real receipt."""
    from fastapi.testclient import TestClient
    import main
    main._rl_buckets.clear()
    data = _fixture("screen_partial")
    real_dispatch = mcp_server._dispatch_and_label

    async def fake(name, args, headers=None, skip_auth=False):
        if len(str(args.get("name", ""))) > 300:       # the over-long-name probe: the REAL handler refuses it, offline
            return await real_dispatch(name, args, headers, skip_auth)
        return copy.deepcopy(data["receipt"])
    monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
    lv = _load("live_verify_release_p3", "scripts/live_verify_release.py")
    client = TestClient(main.app)

    def fake_http(method, url, *, body=None, headers=None, timeout=40.0, raw=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        r = client.request(method, "/" + path, json=body, headers=headers or {}, content=raw)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.text
    monkeypatch.setattr(lv, "http", fake_http)
    return lv


def test_P3e_the_post_deploy_check_passes_on_the_door_as_built(live):
    res = live.check_chatgpt_door({"base": "http://testserver"})
    assert res["ok"], res.get("problems")


def test_P3e2_the_post_deploy_check_catches_wrong_annotations(live, monkeypatch):
    """readOnlyHint false with destructiveHint true used to pass ("is a bool")."""
    monkeypatch.setitem(no_commerce._ANNOTATIONS, "readOnlyHint", False)
    monkeypatch.setitem(no_commerce._ANNOTATIONS, "destructiveHint", True)
    res = live.check_chatgpt_door({"base": "http://testserver"})
    assert not res["ok"] and any("exact annotations" in p for p in res["problems"]), res.get("problems")


def test_P3e3_the_post_deploy_check_catches_a_result_that_breaks_its_own_schema(live, monkeypatch):
    real = no_commerce.output_schema

    def strict(op):
        schema = real(op)
        schema["properties"]["status"] = {"type": "integer"}      # the real status is a string
        return schema
    monkeypatch.setattr(no_commerce, "output_schema", strict)
    res = live.check_chatgpt_door({"base": "http://testserver"})
    assert not res["ok"] and any("outputSchema" in p for p in res["problems"]), res.get("problems")


def test_P3e4_the_post_deploy_check_catches_a_door_chosen_by_payload(live, monkeypatch):
    """Put the F1 hole back and the live check must say so."""
    original = mcp_server._handle_mcp_request_core

    async def reopened(payload, headers, profile, obs, single=True):
        if profile is None and isinstance(payload, dict) and isinstance(payload.get("params"), dict) \
                and payload["params"].get("_profile") == "chatgpt":
            profile = "chatgpt"          # what the unfixed code effectively did
        return await original(payload, headers, profile, obs, single)
    monkeypatch.setattr(mcp_server, "_handle_mcp_request_core", reopened)
    res = live.check_chatgpt_door({"base": "http://testserver"})
    assert not res["ok"] and any("choose the ChatGPT door" in p for p in res["problems"]), res.get("problems")


def test_P3e5_the_post_deploy_check_does_not_trust_an_llms_txt_error(live, monkeypatch):
    real = live.http

    def broken_llms(method, url, **kw):
        if url.endswith("/llms.txt"):
            return 500, {}, "boom"
        return real(method, url, **kw)
    monkeypatch.setattr(live, "http", broken_llms)
    res = live.check_chatgpt_door({"base": "http://testserver"})
    assert not res["ok"] and any("llms.txt" in p for p in res["problems"]), res.get("problems")
