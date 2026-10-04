"""The ChatGPT-only door: a surface where nothing sells, prices or links to credits.

WHY IT EXISTS (docs/directory-kit-2026-10/ISSUES-BEFORE-SUBMITTING.md section 13, read from OpenAI's own pages on
2026-10-03): a plugin listed in ChatGPT may not sell, price or promote digital goods, and credits are digital
goods, sold directly or through a freemium upsell. Everything our three Claude-facing doors do around their
data tools is exactly that: a "[free in quota, then $0.02/call]" tag in every description, "call preview_cost
first" in the handshake, and an over-quota answer that links to a page with Buy buttons. Pricing text is
legitimate in Claude's directory, so those doors stay as they are and ChatGPT gets its own.

WHAT THESE TESTS PIN, in the order a reviewer meets it:

  * the door exists, serves only the three tools that work fully free and keyless, and opts out of the four
    orientation tools (one of which, preview_cost, IS a price list);
  * everything that can be read before a call - tools/list, the handshake, resources, prompts, refusals - has
    no pricing, credit, payment-rail or purchase wording, and names no tool the door does not have;
  * every tool carries what OpenAI's review asks for: a title, explicit annotations, an outputSchema that the
    real results satisfy, and `securitySchemes: noauth` (and never oauth2: the door has no sign-in);
  * a result carries what the question needs and no session, trace, request or timing metadata;
  * x402 is refused, the daily data quota and the credits rail are never entered, a bad key is not scolded
    with a link to a key page, and the one limit that does exist answers with the limit and the reset time and
    no link;
  * the existing doors are byte-for-byte what they were (each guard below is paired with the unchanged door
    it would have broken).

A guard that cannot fail is decoration, so most assertions below are paired with a control on an existing door
showing the thing the guard removes is really there.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import mcp_server, no_commerce, profiles  # noqa: E402
from agent_interface.mcp_server import handle_mcp_request  # noqa: E402

DOOR = "chatgpt"
OLD_DOOR = "sanctions-screening"
THREE = {"screen_sanctions", "verify_company_record", "map_trade_restriction"}
ORIENTATION = {"get_status", "get_outcome", "preview_cost", "self_test"}
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "chatgpt_door")


def run(coro):
    return asyncio.run(coro)


def rpc(method, params=None, profile=DOOR, headers=None, rid=1):
    return run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
        headers=headers or {}, profile=profile))


def tools_list(profile=DOOR):
    return rpc("tools/list", profile=profile)["result"]["tools"]


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def body_of(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def every_string(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from every_string(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from every_string(v)


def assert_clean(obj, what):
    """No selling, pricing, credit, payment-rail or purchase wording anywhere in `obj`."""
    hits = []
    for s in every_string(obj):
        for m in no_commerce.FORBIDDEN_RE.finditer(s):
            hits.append(f"{m.group(0)!r} in {s[max(0, m.start() - 40):m.end() + 40]!r}")
    assert not hits, f"{what} carries commerce wording: " + "; ".join(hits[:6])


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Make the engine answer with a captured REAL receipt, and record what it was asked."""
    calls = []

    def install(fixture_name):
        data = fixture(fixture_name)

        async def fake(name, args, headers=None, skip_auth=False):
            calls.append((name, args))
            return copy.deepcopy(data["receipt"])
        monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
        return data
    install.calls = calls
    return install


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    """Nothing here may depend on the machine's own billing switches or a stray ceiling."""
    for var in ("DATA_METERING_ENABLED", "CREDITS_ENABLED", "CHATGPT_DOOR_DAILY_CEILING"):
        monkeypatch.delenv(var, raising=False)
    no_commerce_reset = getattr(no_commerce, "reset_ceiling_for_tests", None)
    if no_commerce_reset:
        no_commerce_reset()
    yield


# ---------------------------------------------------------------------------
# The door exists and is the right size
# ---------------------------------------------------------------------------

def test_the_door_is_a_profile_serving_exactly_the_three_free_data_tools():
    assert DOOR in profiles.PROFILES
    assert profiles.tools_for(DOOR) == frozenset(THREE)


def test_the_door_opts_out_of_the_orientation_tools_and_the_others_do_not():
    """preview_cost IS a price list; self_test, get_status and get_outcome have no use for tools that never
    run async. The opt-out must not leak: every other door keeps all four."""
    assert not (profiles.tools_for(DOOR) & ORIENTATION)
    for other in sorted(set(profiles.PROFILES) - {DOOR}):
        assert ORIENTATION <= profiles.tools_for(other), other


def test_the_door_refuses_the_tools_it_does_not_have_without_naming_a_shop():
    for tool in ("send_message", "call_business", "preview_cost", "self_test", "get_status", "check_compliance",
                 "find_business", "check_quota"):
        r = rpc("tools/call", {"name": tool, "arguments": {}})
        assert "error" in r, f"{tool} executed through the ChatGPT door"
        msg = r["error"]["message"]
        assert "not available on this endpoint" in msg and tool not in msg
        assert "http" not in msg and "agent-broker" not in msg, msg
        assert_clean(r["error"], f"the refusal of {tool}")


def test_the_door_is_a_real_route_and_an_unknown_door_still_404s():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app, base_url="https://api.hatchloop.dev")
    r = c.post(f"/mcp/{DOOR}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 200, r.text
    assert {t["name"] for t in r.json()["result"]["tools"]} == THREE
    assert c.post("/mcp/chatgpt-typo", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404
    assert c.get(f"/mcp/{DOOR}").status_code == 405


def test_the_door_is_not_advertised_in_public_discovery_and_is_not_an_oauth_resource():
    """It is a door for one directory. Listing it in llms.txt or the MCP descriptor would present a second
    server under a second name (what the registry's spam rule describes); and it has no sign-in, so it must
    not publish OAuth protected-resource metadata that would invite one."""
    from agent_interface.well_known import get_llms_txt, get_mcp_descriptor
    assert DOOR not in {e["name"] for e in get_mcp_descriptor()["capability_endpoints"]}
    assert f"/mcp/{DOOR}" not in get_llms_txt()
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app, base_url="https://api.hatchloop.dev")
    assert c.get(f"/.well-known/oauth-protected-resource/mcp/{DOOR}").status_code == 404
    assert c.get(f"/.well-known/oauth-protected-resource/mcp/{OLD_DOOR}").status_code == 200   # control


# ---------------------------------------------------------------------------
# What is read before a call: tools/list
# ---------------------------------------------------------------------------

def test_every_tool_carries_title_output_schema_explicit_annotations_and_noauth():
    tools = tools_list()
    assert {t["name"] for t in tools} == THREE
    for t in tools:
        assert isinstance(t.get("title"), str) and len(t["title"]) > 8, t["name"]
        assert t["title"] != t["name"] and "_" not in t["title"], t["title"]
        assert t["annotations"]["title"] == t["title"]
        a = t["annotations"]
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(a[hint], bool), f"{t['name']}.{hint} is not an explicit boolean"
        assert a["readOnlyHint"] is True and a["destructiveHint"] is False and a["idempotentHint"] is True
        # OpenAI: true for public or open-ended entities, false for a bounded private account or catalogue.
        # These read live public registries about arbitrary third parties, with the caller's text sent on.
        assert a["openWorldHint"] is True, t["name"]
        assert t["securitySchemes"] == [{"type": "noauth"}], t["name"]
        assert t["outputSchema"]["type"] == "object" and "result" in t["outputSchema"]["properties"]
        assert t["inputSchema"]["type"] == "object"


def test_the_existing_doors_still_lack_these_fields_so_the_guard_is_measuring_something():
    """Control: the same three tools on the Claude-facing door have no top-level title, no outputSchema and
    openWorldHint false. If this ever flips, the door-specific work above is no longer door-specific."""
    for t in tools_list(OLD_DOOR):
        if t["name"] in THREE:
            assert "title" not in t and "outputSchema" not in t
            assert t["annotations"]["openWorldHint"] is False


def test_input_descriptions_are_whole_sentences_not_cut_at_eighty_characters():
    manifest = {o["name"]: o for o in mcp_server.get_full_manifest()["operations"]}
    for t in tools_list():
        want = manifest[t["name"]]["input_schema"]["properties"]
        got = t["inputSchema"]["properties"]
        assert set(got) == set(want)
        for k, v in want.items():
            assert got[k].get("description") == no_commerce.clean_text(v.get("description")), (t["name"], k)
            assert not got[k]["description"].endswith("\u2026"), (t["name"], k)
    # control: the old door still cuts them
    cut = [p["description"] for t in tools_list(OLD_DOOR) if t["name"] == "screen_sanctions"
           for p in t["inputSchema"]["properties"].values() if p.get("description", "").endswith("\u2026")]
    assert cut, "the existing door no longer truncates; update this control"


def test_descriptions_say_what_each_tool_does_and_nothing_is_priced():
    manifest = {o["name"]: o for o in mcp_server.get_full_manifest()["operations"]}
    for t in tools_list():
        d = t["description"]
        assert d and not d.rstrip().endswith("\u2026"), t["name"]
        assert not d.startswith("Free"), "pricing-style opener survived: " + d[:30]
        assert not re.search(r"\[[^\]]*\]\s*$", d), "a bracketed cost tag is still appended: " + d[-40:]
        # the manifest's own sentences minus the pricing words: nothing was invented for the door
        assert d == no_commerce.clean_description(manifest[t["name"]]["description"]), t["name"]
    # control: the existing door's descriptions DO carry the tag section 13 names
    assert any("[free in quota, then $0.02/call]" in t["description"] for t in tools_list(OLD_DOOR)
               if t["name"] in THREE)


def test_nothing_in_tools_list_sells_prices_or_names_a_tool_the_door_lacks():
    tools = tools_list()
    assert_clean(tools, "tools/list on the ChatGPT door")
    others = {o["name"] for o in mcp_server.get_full_manifest()["operations"]} - THREE
    text = json.dumps(tools)
    leaked = sorted(n for n in others if re.search(rf"\b{re.escape(n)}\b", text))
    assert not leaked, f"descriptions point at tools this door does not have: {leaked}"


def test_the_door_never_offers_a_sign_in_even_when_the_connect_flow_is_ready(monkeypatch):
    """The Connect annotation adds an oauth2 scheme to quota-free tools whenever the sign-in can complete.
    On this door that would put a Connect button in front of a ChatGPT user for a tool that needs no account."""
    from agent_interface.oauth import challenge, settings

    async def ready():
        return True
    monkeypatch.setattr(challenge, "ready", ready)
    monkeypatch.setattr(settings, "enabled", lambda: True)
    monkeypatch.setattr(settings, "challenge_style", lambda: "auto")
    for t in tools_list():
        assert t["securitySchemes"] == [{"type": "noauth"}], t["name"]
    # control: with the same switches the existing door DOES offer it, so the line above could have failed
    assert any({"type": "noauth"} in t.get("securitySchemes", []) and len(t["securitySchemes"]) == 2
               for t in tools_list(OLD_DOOR) if t["name"] in THREE)


# ---------------------------------------------------------------------------
# Handshake, discovery, resources and prompts
# ---------------------------------------------------------------------------

def test_the_handshake_carries_no_pricing_no_preview_cost_and_no_key_talk():
    r = rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert r["serverInfo"]["name"] == DOOR
    text = r["instructions"]
    assert_clean(r, "initialize on the ChatGPT door")
    for banned in ("preview_cost", "X-Agent-Identity", "agent-broker", "Write operations", "write", "key"):
        assert banned.lower() not in text.lower(), banned
    assert "3 " in text and "read-only" in text
    assert "[UNTRUSTED]" in text            # the fencing rule still travels with the door
    # control: the existing door's handshake carries every one of them
    old = rpc("initialize", {}, profile=OLD_DOOR)["result"]["instructions"]
    assert "preview_cost" in old and "free within a daily quota" in old


def test_the_door_declares_tools_only_and_the_modern_handshake_agrees():
    caps = rpc("initialize", {})["result"]["capabilities"]
    assert set(caps) == {"tools"}, caps
    disc = rpc("server/discover", {})["result"]
    assert set(disc["capabilities"]) == {"tools"}, disc["capabilities"]
    assert disc["instructions"] == rpc("initialize", {})["result"]["instructions"]
    assert_clean(disc, "server/discover on the ChatGPT door")


def test_resources_and_prompts_are_empty_and_cannot_hand_over_the_price_list():
    """resources/read of agent-broker://manifest returns every operation's cost_model, 0.02 dollars a call
    included, and prompts/get cost_estimation tells the model to call preview_cost. Neither is filtered by
    door today. On this one both are shut."""
    assert rpc("resources/list")["result"] == {"resources": []}
    assert rpc("prompts/list")["result"] == {"prompts": []}
    assert rpc("resources/templates/list")["result"] == {"resourceTemplates": []}
    r = rpc("resources/read", {"uri": "agent-broker://manifest"})
    assert "error" in r and "cost_model" not in json.dumps(r)
    r = rpc("prompts/get", {"name": "cost_estimation"})
    assert "error" in r and "preview_cost" not in json.dumps(r)
    # control: the existing door still serves them
    assert "cost_model" in rpc("resources/read", {"uri": "agent-broker://manifest"},
                               profile=OLD_DOOR)["result"]["contents"][0]["text"]
    assert rpc("prompts/list", profile=OLD_DOOR)["result"]["prompts"]


# ---------------------------------------------------------------------------
# What comes back from a call
# ---------------------------------------------------------------------------

LEAKY_KEYS = {"operation_id", "trace_id", "latency_ms", "channel_used", "channel_fallback_chain",
              "estimated_completion_time", "next_actions", "cost", "compliance_receipt", "policy_sha256",
              "policy_version", "service_version", "issued_at"}


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


CASES = [
    ("screen_hit", "screen_sanctions"),
    ("screen_partial", "screen_sanctions"),
    ("verify_found", "verify_company_record"),
    ("verify_not_found", "verify_company_record"),
    ("trade_partial", "map_trade_restriction"),
    ("trade_embargo", "map_trade_restriction"),
]


@pytest.mark.parametrize("name,tool", CASES)
def test_a_result_carries_the_answer_and_no_session_trace_or_timing_metadata(fake_dispatch, name, tool):
    data = fake_dispatch(name)
    resp = rpc("tools/call", {"name": tool, "arguments": data["arguments"]})
    res = resp["result"]
    assert res["isError"] is False
    body = body_of(resp)
    assert not (set(walk_keys(body)) & LEAKY_KEYS), sorted(set(walk_keys(body)) & LEAKY_KEYS)
    assert not [k for k in walk_keys(body) if k.startswith("_")], "internal underscore fields survived"
    assert set(body) <= {"status", "reason_code", "human_message", "result", "retriable", "untrusted_content"}
    assert body["human_message"] and body["result"]
    assert res["structuredContent"] == body              # one answer, stated once in each place MCP expects
    assert_clean(body, f"the {name} result")


def test_the_substance_survives_the_trim(fake_dispatch):
    """What a user asked about must still be there: matches, the lists that were screened with their dates, when it
    was screened, the disclaimer and what could not be screened."""
    data = fake_dispatch("screen_hit")
    src = data["receipt"]["result"]
    body = body_of(rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]}))
    r = body["result"]
    for keep in ("matched", "screening_status", "lists_screened", "sources_queried", "screened_at", "disclaimer",
                 "sources_unavailable", "matching_method"):
        assert r[keep] == src[keep], keep
    for keep in ("matches", "possible_matches_unverified"):
        assert r[keep] == [_strip_internal(x) for x in src[keep]], keep
    assert any(k.startswith("_") for k in src["matches"][0]), "precondition: the source carries an internal field"
    assert r["matches"][0]["list"] == "OFAC-SDN"
    assert "[UNTRUSTED]" in r["matches"][0]["name"]            # the fence on third-party text is untouched
    assert "MATCH FOUND" in body["human_message"]


def _strip_internal(entry):
    return {k: v for k, v in entry.items() if not k.startswith("_")} if isinstance(entry, dict) else entry


def test_the_known_commerce_phrases_in_our_own_prose_are_reworded(fake_dispatch):
    data = fake_dispatch("verify_not_found")
    assert "free registries" in data["receipt"]["human_message"]          # precondition: the source says it
    body = body_of(rpc("tools/call", {"name": "verify_company_record", "arguments": data["arguments"]}))
    assert "free" not in body["human_message"].lower() and "public registries" in body["human_message"]
    data = fake_dispatch("trade_too_many")
    assert "nothing was charged" in data["receipt"]["human_message"]
    resp = rpc("tools/call", {"name": "map_trade_restriction", "arguments": data["arguments"]})
    assert resp["result"]["isError"] is True
    body = body_of(resp)
    assert "charged" not in body["human_message"] and body["human_message"].rstrip().endswith("on this call.")
    assert body["reason_code"] == "bad_input"
    assert set(body) <= {"status", "reason_code", "human_message", "retriable"}
    assert "structuredContent" not in resp["result"]       # a failure is not what the outputSchema describes
    assert_clean(body, "a failure result")


def test_the_untrusted_notice_is_the_doors_own_and_names_no_other_tool(fake_dispatch):
    data = fake_dispatch("verify_found")
    assert "send_message" in data["receipt"]["untrusted_content"]["notice"]      # precondition
    body = body_of(rpc("tools/call", {"name": "verify_company_record", "arguments": data["arguments"]}))
    u = body["untrusted_content"]
    assert u["marker"] == ["[UNTRUSTED]", "[/UNTRUSTED]"] and u["fields"]
    # only the paths that were fenced: the registry's list of paths this call did not return is noise
    assert all(f.get("fenced") for f in u["fields"]), u["fields"]
    assert any(not f.get("fenced") for f in data["receipt"]["untrusted_content"]["fields"]), "precondition"
    assert "data" in u["notice"].lower() and "instruction" in u["notice"].lower()
    text = json.dumps(u)
    for banned in ("send_message", "call_business", "send_transactional_confirmation", "policy_sha256"):
        assert banned not in text, banned
    assert_clean(u, "the untrusted_content block")


def _validate(schema, value, path="$"):
    """The subset of JSON Schema the door uses: type (or list of types), properties, items, required."""
    errs = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        ok = any({"object": isinstance(value, dict), "array": isinstance(value, list),
                  "string": isinstance(value, str),
                  "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                  "integer": isinstance(value, int) and not isinstance(value, bool),
                  "boolean": isinstance(value, bool), "null": value is None}[x] for x in types)
        if not ok:
            return [f"{path}: {value!r:.60} is not {types}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing {req}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs += _validate(sub, value[k], f"{path}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            errs += _validate(schema["items"], v, f"{path}[{i}]")
    return errs


@pytest.mark.parametrize("name,tool", CASES)
def test_the_real_results_satisfy_the_declared_output_schema(fake_dispatch, name, tool):
    """A client built on the official SDK checks structuredContent against outputSchema and throws when they
    disagree. The manifest's own result schema says hs_code_hint is a string where the real value is null, and
    lists two statuses where a third occurs, so the door's schema is its own, loosened to what really comes back."""
    data = fake_dispatch(name)
    resp = rpc("tools/call", {"name": tool, "arguments": data["arguments"]})
    schema = next(t["outputSchema"] for t in tools_list() if t["name"] == tool)
    errs = _validate(schema, resp["result"]["structuredContent"])
    assert not errs, errs


def test_the_schema_checker_in_this_file_can_fail():
    schema = next(t["outputSchema"] for t in tools_list() if t["name"] == "verify_company_record")
    assert _validate(schema, {"status": "success", "result": "not an object"})
    assert _validate(schema, {"result": {}})            # missing required status


def test_the_unavailable_status_the_manifest_enum_forgets_is_accepted():
    schema = next(t["outputSchema"] for t in tools_list() if t["name"] == "verify_company_record")
    ok = {"status": "success", "reason_code": "partial_lookup", "human_message": "x", "retriable": True,
          "result": {"status": "unavailable", "queried_name": "x", "queried_country": None, "queried_lei": None,
                     "sources_queried": [], "sources_unavailable": ["GLEIF"]}}
    assert _validate(schema, ok) == []


# ---------------------------------------------------------------------------
# Payment, quota, credits and keys: the door enters none of them
# ---------------------------------------------------------------------------

def test_x402_is_refused_before_anything_runs_and_no_offer_is_made(monkeypatch, fake_dispatch):
    from billing import x402_gate
    data = fake_dispatch("screen_partial")
    # With metering off the existing doors bypass x402 for these tools entirely; production has it on (the
    # dispatcher's own comment), and that is the state in which the control below reaches the gate.
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    monkeypatch.setattr(x402_gate, "is_paid_tool", lambda n: True)
    offered = []

    async def spy(*a, **k):
        offered.append(a)
        return {"content": [{"type": "text", "text": "{}"}], "isError": False}
    monkeypatch.setattr(x402_gate, "run_paid_tool", spy)
    params = {"name": "screen_sanctions", "arguments": data["arguments"],
              "_meta": {"x402/payment": "anything"}}
    resp = rpc("tools/call", params)
    assert resp["result"]["isError"] is True
    body = body_of(resp)
    assert body["reason_code"] == "request_metadata_not_used" and body["retriable"] is False
    assert not offered, "the x402 gate was entered from the ChatGPT door"
    assert not fake_dispatch.calls, "the tool ran although the request carried a payment attachment"
    assert_clean(body, "the x402 refusal")
    assert "x402" not in json.dumps(body).lower() and "usdc" not in json.dumps(body).lower()
    # control: the existing door hands the same request to the x402 gate
    rpc("tools/call", params, profile=OLD_DOOR)
    assert offered, "the control door no longer reaches the x402 gate; this test has stopped measuring anything"


def test_the_daily_data_quota_is_never_consulted_and_never_links_anywhere(monkeypatch, fake_dispatch):
    from billing import data_quota
    data = fake_dispatch("screen_partial")
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    consulted = []

    async def over_quota(**kw):
        consulted.append(kw["name"])
        return {"allowed": False, "response": {
            "status": "failure", "reason_code": "free_quota_exceeded",
            "human_message": "Free daily limit reached (100/day for anonymous callers). top up credits at "
                             "https://hatchloop.dev/pricing."}}
    monkeypatch.setattr(data_quota, "consume_data_quota", over_quota)
    resp = rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]})
    assert resp["result"]["isError"] is False and not consulted
    # control: the existing door, same switches, answers with the pricing link section 13 forbids
    old = rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]}, profile=OLD_DOOR)
    assert consulted and "hatchloop.dev/pricing" in json.dumps(old)


def test_the_credits_rail_is_never_entered(monkeypatch, fake_dispatch):
    from billing import credits
    data = fake_dispatch("screen_partial")
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    seen = []
    monkeypatch.setattr(credits, "is_credit_paid_tool", lambda n: seen.append(n) or True)
    monkeypatch.setattr(credits, "resolve_account", lambda h: seen.append("resolve") or "acct_x")
    resp = rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]},
               headers={"x-agent-identity": "whatever"})
    assert resp["result"]["isError"] is False
    assert not seen, f"the credits rail was consulted from the ChatGPT door: {seen}"


def test_a_key_that_does_not_work_is_not_scolded_with_a_link_to_a_key_page(fake_dispatch):
    data = fake_dispatch("screen_partial")
    headers = {"x-agent-identity": "not-a-real-key"}
    door = rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]}, headers=headers)
    assert "hatchloop/auth_warning" not in door["result"].get("_meta", {})
    assert "auth_warning" not in body_of(door)
    assert_clean(door, "a ChatGPT-door result for a caller that sent a bad key")
    old = rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]},
              headers=headers, profile=OLD_DOOR)
    assert "hatchloop/auth_warning" in old["result"]["_meta"]            # control


def test_a_bad_key_on_tools_list_gets_no_warning_either():
    r = rpc("tools/list", headers={"x-agent-identity": "not-a-real-key"})["result"]
    assert "auth_warning" not in r
    assert "auth_warning" in rpc("tools/list", headers={"x-agent-identity": "not-a-real-key"},
                                 profile=OLD_DOOR)["result"]          # control


# ---------------------------------------------------------------------------
# The one limit that exists: an abuse ceiling per address, answered neutrally
# ---------------------------------------------------------------------------

def _call_from(ip, data):
    return rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"]},
               headers={"x-forwarded-for": ip})


def test_over_the_ceiling_the_answer_names_the_limit_and_the_reset_and_links_nowhere(monkeypatch, fake_dispatch):
    data = fake_dispatch("screen_partial")
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", "3")
    monkeypatch.setattr(no_commerce, "_today_utc", lambda: "2026-10-04")
    for _ in range(3):
        assert _call_from("203.0.113.7", data)["result"]["isError"] is False
    n_before = len(fake_dispatch.calls)
    over = _call_from("203.0.113.7", data)
    assert over["result"]["isError"] is True
    body = body_of(over)
    assert body["reason_code"] == "rate_limited" and body["retriable"] is True
    msg = body["human_message"]
    assert "3" in msg and "2026-10-05T00:00:00Z" in msg, msg
    assert "http" not in msg.lower() and "@" not in msg, msg
    assert_clean(body, "the over-limit answer")
    assert body.get("retry_after_ms", 0) > 0
    assert len(fake_dispatch.calls) == n_before, "the tool ran after the ceiling was reached"
    # another address is untouched, and the next UTC day starts a new count
    assert _call_from("203.0.113.8", data)["result"]["isError"] is False
    monkeypatch.setattr(no_commerce, "_today_utc", lambda: "2026-10-05")
    assert _call_from("203.0.113.7", data)["result"]["isError"] is False


def test_the_ceiling_is_high_by_default_and_zero_turns_it_off(monkeypatch, fake_dispatch):
    """The anonymous allowance is 100 a day per address, and ChatGPT's calls may share a few addresses, so the
    default is an abuse ceiling, not a user limit. Its size is pinned loosely: a typo to 100 must fail."""
    assert no_commerce.default_ceiling() >= 5000
    data = fake_dispatch("screen_partial")
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", "0")
    for _ in range(30):
        assert _call_from("203.0.113.9", data)["result"]["isError"] is False


def test_the_ceiling_does_not_count_calls_it_refused_at_the_door_boundary(fake_dispatch, monkeypatch):
    """A refused tool name or a refused payment attachment never ran, so it must not spend the caller's day."""
    data = fake_dispatch("screen_partial")
    monkeypatch.setenv("CHATGPT_DOOR_DAILY_CEILING", "2")
    for _ in range(5):
        rpc("tools/call", {"name": "send_message", "arguments": {}}, headers={"x-forwarded-for": "198.51.100.1"})
        rpc("tools/call", {"name": "screen_sanctions", "arguments": data["arguments"],
                           "_meta": {"x402/payment": "x"}}, headers={"x-forwarded-for": "198.51.100.1"})
    assert _call_from("198.51.100.1", data)["result"]["isError"] is False


# ---------------------------------------------------------------------------
# The source of our own prose, and the existing doors
# ---------------------------------------------------------------------------

def test_the_regex_catches_the_strings_section_13_names_and_spares_ordinary_prose():
    for bad in ("[free in quota, then $0.02/call]", "top up credits at https://hatchloop.dev/pricing",
                "Starter $9/1,000 credits", "pay per call in USDC on Base via x402", "Buy credits",
                "call preview_cost first", "upgrade your plan", "your free trial", "nothing was charged",
                "Free screening of", "per-call pricing", "subscription"):
        assert no_commerce.FORBIDDEN_RE.search(bad), bad
    for fine in ("Screening of a name against sanctions lists", "At most 20 parties per call",
                 "Informational screening only, not legal advice", "Never fabricates a match or clear",
                 "Nothing was screened on this call.", "registered with these public registries"):
        assert not no_commerce.FORBIDDEN_RE.search(fine), fine


def test_handler_prose_cannot_grow_new_commerce_wording_unnoticed():
    """The door passes our own sentences through, so a new 'free' or 'credits' in one of the three handlers
    would reach ChatGPT. Every string literal in them that is not a docstring is scanned; the three allowed
    ones are exactly the ones the door rewords, plus the cost block the door drops."""
    allowed = {"USD", "free", ". The company may not be a legal entity registered with these free registries.",
               " per call so one request cannot occupy the service. Split the list across calls - each call "
               "screens every party it is given, completely. Nothing was screened on this call and nothing was "
               "charged."}
    offenders = []
    for rel in ("core/verify_company_record.py", "core/map_trade_restriction.py", "core/screen_sanctions.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                    docs.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if no_commerce.FORBIDDEN_RE.search(node.value) and node.value not in allowed \
                        and not any(node.value in a for a in allowed):
                    offenders.append(f"{rel}:{node.lineno}: {node.value[:90]!r}")
    assert not offenders, offenders


def test_third_party_text_is_never_rewritten(monkeypatch):
    """A registry can hold 'Credit Suisse AG' and a sanctions list 'FREE ZONE TRADING LLC', and a caller can type
    'Free Credits Ltd'. The door rewords only OUR phrases and never touches those."""
    data = fixture("screen_hit")
    receipt = data["receipt"]
    receipt["result"]["matches"][0]["name"] = "[UNTRUSTED]FREE ZONE CREDIT TRADING LLC[/UNTRUSTED]"
    receipt["human_message"] = ("MATCH FOUND for 'Free Credits Ltd': [UNTRUSTED]FREE ZONE CREDIT TRADING LLC"
                                "[/UNTRUSTED] on OFAC-SDN.")

    async def fake(name, args, headers=None, skip_auth=False):
        return copy.deepcopy(receipt)
    monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
    body = body_of(rpc("tools/call", {"name": "screen_sanctions", "arguments": {"name": "Free Credits Ltd"}}))
    assert body["result"]["matches"][0]["name"] == "[UNTRUSTED]FREE ZONE CREDIT TRADING LLC[/UNTRUSTED]"
    assert "Free Credits Ltd" in body["human_message"] and "FREE ZONE CREDIT" in body["human_message"]


def test_existing_doors_tools_list_is_unchanged_by_the_new_door():
    """The three Claude-facing doors are what 4e46f8e served: same names, same four orientation tools, the pricing
    tag still on the data tools (the directory listing that uses it is unchanged)."""
    for door in ("sanctions-screening", "company-verification", "compliance-check"):
        names = {t["name"] for t in tools_list(door)}
        assert ORIENTATION <= names
    assert {t["name"] for t in tools_list(OLD_DOOR)} == set(profiles.tools_for(OLD_DOOR))
    assert len(rpc("tools/list", profile=None)["result"]["tools"]) == 23
