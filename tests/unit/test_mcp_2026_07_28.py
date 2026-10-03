"""MCP protocol revision 2026-07-28, served alongside the legacy `initialize` handshake.

THE GAP (measured 2026-10-03, HatchLoop demand review): `server/discover` - a server MUST in the revision -
was answered -32601 652 times in 47 hours from 20 callers, 939 POSTs already carried
`MCP-Protocol-Version: 2026-07-28`, and the revision removes `initialize`, so a discover-first client could not
complete a handshake with us at all.

These tests pin, per method and per door:
  * server/discover: shape, identity per door, honest capabilities, unsupported-version error, caching hints;
  * every other method's 2026-07-28 shape (resultType, serverInfo in _meta, ttlMs/cacheScope where required);
  * the refusals the revision defines, with their HTTP statuses (400 / 404) and error codes (-32020 / -32022
    / -32602 / -32601);
  * that nothing changes for older clients: `initialize` still negotiates only legacy versions, legacy
    results carry none of the new fields, legacy requests are not header-validated;
  * that the callers asking for this revision TODAY (header-only 2026-07-28, scanner-I's extra
    Mcp-Method on a 2025-06-18 request) are served rather than refused;
  * the outcome log: modern identity from `_meta`, the real HTTP status, outcomes the database accepts.

Replays of recorded and observed client handshakes are in test_mcp_handshake_replay.py.
"""
from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path

import pytest

from agent_interface import mcp_2026 as m26
from agent_interface import mcp_server, profiles
from billing import usage_logger as ul

ROOT = Path(__file__).resolve().parents[2]
M = "io.modelcontextprotocol/"
V = "2026-07-28"
SERVER_INFO = M + "serverInfo"
DOORS = sorted(profiles.PROFILES)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

@pytest.fixture
def events(monkeypatch):
    got = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: got.append(e))
    return got


@pytest.fixture
def client(events):
    from fastapi.testclient import TestClient
    import main
    main._rl_buckets.clear()
    ul.RATE_LIMIT_LOG_THROTTLE._last.clear()
    ul.RATE_LIMIT_LOG_THROTTLE._held.clear()
    c = TestClient(main.app, raise_server_exceptions=False)
    yield c
    main._rl_buckets.clear()


def _meta(version=V, caps=True, info=True):
    m = {M + "protocolVersion": version}
    if caps is not False:
        m[M + "clientCapabilities"] = {} if caps is True else caps
    if info:
        m[M + "clientInfo"] = {"name": "pytest-modern", "version": "9.9"}
    return m


def _post(client, method, params=None, *, door=None, name=None, meta=None, version=V, drop=(), headers=None, rid=1):
    """One modern request. meta=False sends NO envelope (a header-only caller); `drop` removes headers."""
    p = dict(params or {})
    if meta is not False:
        p["_meta"] = _meta() if meta is None else meta
    h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        h["Mcp-Name"] = name
    h.update(headers or {})
    for d in drop:
        h.pop(d, None)
    path = f"/mcp/{door}" if door else "/mcp"
    return client.post(path, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": p}, headers=h)


def _legacy(client, method, params=None, *, door=None, headers=None, rid=1):
    path = f"/mcp/{door}" if door else "/mcp"
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers or {})


def _result(resp):
    doc = resp.json()
    assert "result" in doc, doc
    return doc["result"]


def _err(resp):
    doc = resp.json()
    assert "error" in doc, doc
    return doc["error"]


# ---------------------------------------------------------------------------
# the version tuples: the legacy list must stay legacy
# ---------------------------------------------------------------------------

def test_the_initialize_tuple_has_no_modern_version():
    """`initialize` is the handshake the revision REMOVED. If SUPPORTED_PROTOCOL_VERSIONS gained 2026-07-28, an
    initialize offering it would be answered with it - announcing a handshake that no longer exists."""
    assert V not in mcp_server.SUPPORTED_PROTOCOL_VERSIONS
    assert not set(mcp_server.SUPPORTED_PROTOCOL_VERSIONS) & set(m26.MODERN_PROTOCOL_VERSIONS)


def test_every_version_we_speak_is_modern_first_then_legacy_newest_first():
    allv = list(mcp_server.ALL_PROTOCOL_VERSIONS)
    assert allv[0] == V
    assert allv == list(m26.MODERN_PROTOCOL_VERSIONS) + list(mcp_server.SUPPORTED_PROTOCOL_VERSIONS)
    assert allv == sorted(allv, reverse=True)


@pytest.mark.parametrize("offered", [V, "2027-01-01", "1999-01-01", "latest"])
def test_initialize_never_answers_with_a_modern_version(client, offered):
    r = _legacy(client, "initialize", {"protocolVersion": offered, "capabilities": {},
                                       "clientInfo": {"name": "x", "version": "1"}},
                headers={"MCP-Protocol-Version": V})
    assert r.status_code == 200
    res = _result(r)
    assert res["protocolVersion"] == mcp_server.PROTOCOL_VERSION == "2025-11-25"
    assert "resultType" not in res and "ttlMs" not in res, "initialize is legacy whatever the headers say"


def test_initialize_is_legacy_even_when_it_carries_a_modern_envelope(client):
    r = _post(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "x", "version": "1"}})
    assert r.status_code == 200
    assert _result(r)["protocolVersion"] == "2025-06-18"
    assert "resultType" not in _result(r)


# ---------------------------------------------------------------------------
# server/discover
# ---------------------------------------------------------------------------

def test_discover_returns_the_documented_result(client):
    r = _post(client, "server/discover")
    assert r.status_code == 200
    res = _result(r)
    assert res["resultType"] == "complete"
    assert res["supportedVersions"] == list(mcp_server.ALL_PROTOCOL_VERSIONS)
    assert set(res["capabilities"]) == {"tools", "resources", "prompts"}
    assert res["_meta"][SERVER_INFO] == {"name": "agent-broker", "version": mcp_server.SERVER_VERSION}
    assert isinstance(res["instructions"], str) and "tools/list" in res["instructions"]
    assert isinstance(res["ttlMs"], int) and res["ttlMs"] >= 0
    assert res["cacheScope"] == "public"
    assert r.json()["id"] == 1


def test_discover_ids_are_echoed_as_sent(client):
    assert _post(client, "server/discover", rid="discover-1").json()["id"] == "discover-1"


@pytest.mark.parametrize("variant", ["bare", "header_only", "no_version_header_with_method", "legacy_header"])
def test_discover_is_answered_for_callers_that_send_less_than_the_full_envelope(client, variant):
    """The probe that tells a client what we speak must work for a client that does not yet know."""
    body = {"jsonrpc": "2.0", "id": 5, "method": "server/discover"}
    headers = {"bare": {}, "header_only": {"MCP-Protocol-Version": V, "Mcp-Method": "server/discover"},
               "no_version_header_with_method": {"Mcp-Method": "server/discover"},
               "legacy_header": {"MCP-Protocol-Version": "2025-06-18"}}[variant]
    r = client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert _result(r)["supportedVersions"][0] == V


def test_discover_does_not_demand_clientCapabilities(client):
    """It is a probe; every other method does demand them (below)."""
    r = _post(client, "server/discover", meta=_meta(caps=False))
    assert r.status_code == 200


@pytest.mark.parametrize("door", DOORS)
def test_discover_on_a_door_introduces_the_door_not_the_wide_server(client, door):
    res = _result(_post(client, "server/discover", door=door))
    assert res["_meta"][SERVER_INFO]["name"] == door
    init = _result(_legacy(client, "initialize", {"protocolVersion": "2025-11-25"}, door=door))
    assert res["instructions"] == init["instructions"], "the two handshakes must never describe the door differently"
    assert res["_meta"][SERVER_INFO] == init["serverInfo"]


def test_discover_declares_only_what_we_do(client):
    res = _result(_post(client, "server/discover"))
    caps = res["capabilities"]
    # every declared capability has its handlers
    needs = {"tools": ("tools/list", "tools/call"),
             "resources": ("resources/list", "resources/read", "resources/templates/list"),
             "prompts": ("prompts/list", "prompts/get")}
    for cap, methods in needs.items():
        for method in methods:
            assert method in mcp_server._METHOD_HANDLERS, (cap, method)
    # nothing we do not implement
    for absent in ("logging", "completions", "experimental", "extensions"):
        assert absent not in caps, absent
    # no list ever changes at runtime and there is no subscriptions/listen stream
    assert caps["tools"] == {"listChanged": False}
    assert caps["prompts"] == {"listChanged": False}
    assert caps["resources"] == {"subscribe": False, "listChanged": False}
    assert "subscriptions/listen" not in mcp_server._METHOD_HANDLERS


def test_discover_supported_versions_are_all_actually_servable(client):
    """A version in `supportedVersions` that the server then refuses would be the worst kind of lie."""
    for version in _result(_post(client, "server/discover"))["supportedVersions"]:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"MCP-Protocol-Version": version})
        assert r.status_code == 200 and len(_result(r)["tools"]) > 5, version
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                      "params": {"_meta": {M + "protocolVersion": version,
                                                           M + "clientCapabilities": {}}}},
                        headers={"MCP-Protocol-Version": version, "Mcp-Method": "tools/list"})
        assert r.status_code == 200 and len(_result(r)["tools"]) > 5, version


def test_discover_with_an_unsupported_version_header_is_the_revisions_own_error(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "server/discover"},
                    headers={"MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "server/discover"})
    assert r.status_code == 400
    err = _err(r)
    assert err["code"] == -32022 == m26.ERR_UNSUPPORTED_PROTOCOL_VERSION
    assert err["data"]["supported"] == list(mcp_server.ALL_PROTOCOL_VERSIONS)
    assert err["data"]["requested"] == "1999-01-01"
    assert r.json()["id"] == 3


# ---------------------------------------------------------------------------
# the other methods, in their 2026-07-28 shape
# ---------------------------------------------------------------------------

def test_tools_list_modern_shape(client):
    r = _post(client, "tools/list")
    assert r.status_code == 200
    res = _result(r)
    assert res["resultType"] == "complete"
    assert (res["ttlMs"], res["cacheScope"]) == (m26.CACHE_TTL_MS["tools/list"], "public")
    assert res["_meta"][SERVER_INFO]["name"] == "agent-broker"
    assert "nextCursor" not in res, "no pagination: a cursor we cannot honour would be a lie"
    legacy = _result(_legacy(client, "tools/list"))
    assert [t["name"] for t in res["tools"]] == [t["name"] for t in legacy["tools"]]


def test_tools_list_order_is_deterministic(client):
    """The revision: servers SHOULD return tools in a deterministic order (client caching, LLM prompt caches)."""
    a = [t["name"] for t in _result(_post(client, "tools/list"))["tools"]]
    b = [t["name"] for t in _result(_post(client, "tools/list", rid=2))["tools"]]
    assert a == b


@pytest.mark.parametrize("door", DOORS)
def test_tools_list_on_a_door_is_that_doors_tools_only(client, door):
    modern = _result(_post(client, "tools/list", door=door))
    assert {t["name"] for t in modern["tools"]} == set(profiles.tools_for(door))
    assert modern["_meta"][SERVER_INFO]["name"] == door


def test_tools_list_legacy_shape_is_unchanged(client):
    res = _result(_legacy(client, "tools/list"))
    assert set(res) == {"tools"}, "a legacy result must carry none of the new fields"


def test_a_failing_key_makes_tools_list_private_and_uncacheable(client):
    """tools/list is `public` because it says nothing about who asks. A key that fails validation gets an
    `auth_warning` attached - that IS about who asks, and a shared cache must never hand it to the next caller."""
    anon = _result(_post(client, "tools/list"))
    assert (anon["cacheScope"], anon["ttlMs"]) == ("public", m26.CACHE_TTL_MS["tools/list"])
    bad = _result(_post(client, "tools/list", headers={"X-Agent-Identity": "not-a-real-key"}))
    assert "auth_warning" in bad
    assert (bad["cacheScope"], bad["ttlMs"]) == ("private", 0)
    assert bad["resultType"] == "complete"


def test_tools_call_modern_shape(client):
    r = _post(client, "tools/call", {"name": "preview_cost", "arguments": {"operation": "send_message", "params": {}}},
              name="preview_cost")
    assert r.status_code == 200
    res = _result(r)
    assert res["resultType"] == "complete"
    assert res["isError"] is False and res["content"][0]["type"] == "text"
    assert res["_meta"][SERVER_INFO]["name"] == "agent-broker"
    assert "ttlMs" not in res and "cacheScope" not in res, "a tool call is never cacheable"


def test_tools_call_modern_result_matches_the_legacy_result(client):
    args = {"operation": "send_message", "params": {}}
    modern = _result(_post(client, "tools/call", {"name": "preview_cost", "arguments": args}, name="preview_cost"))
    legacy = _result(_legacy(client, "tools/call", {"name": "preview_cost", "arguments": args}))
    assert modern["content"] == legacy["content"] and modern["isError"] == legacy["isError"]
    assert set(legacy) == {"content", "isError"}


def test_a_failed_tool_call_is_still_a_complete_result_with_isError(client):
    r = _post(client, "tools/call", {"name": "get_conversation", "arguments": {}}, name="get_conversation")
    assert r.status_code == 200
    res = _result(r)
    assert res["resultType"] == "complete"
    assert "identity_required" in res["content"][0]["text"]


def test_a_typed_tool_error_is_a_complete_result_with_isError_in_the_modern_era(client, monkeypatch):
    """The `_ToolError` path (here the channel_unavailable gate) builds its result separately from a normal
    return, so it needs its own proof that it gets `resultType` and `_meta` too."""
    for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
              "TWILIO_MESSAGING_SERVICE_SID", "TWILIO_FROM_NUMBER", "RESEND_API_KEY", "SENDGRID_API_KEY",
              "VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID", "VAPI_OUTBOUND_VERIFIED", "WHATSAPP_ACCESS_TOKEN",
              "WHATSAPP_PHONE_ID", "ALLOW_STUB_CHANNELS"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    args = {"recipient": {"id_type": "phone", "id_value": "+15551230000"},
            "content": {"body": "hi"}, "message_type": "transactional"}
    r = _post(client, "tools/call", {"name": "send_message", "arguments": args}, name="send_message")
    assert r.status_code == 200
    res = _result(r)
    assert res["isError"] is True
    assert json.loads(res["content"][0]["text"])["error_code"] == "channel_unavailable"
    assert res["resultType"] == "complete"
    assert res["_meta"][SERVER_INFO]["name"] == "agent-broker"
    legacy = _result(_legacy(client, "tools/call", {"name": "send_message", "arguments": args}))
    assert set(legacy) == {"content", "isError"}, "the same failure in the legacy era is exactly what it was"


def test_an_unknown_tool_is_a_protocol_error_not_a_crash(client):
    r = _post(client, "tools/call", {"name": "no_such_tool", "arguments": {}}, name="no_such_tool")
    assert r.status_code == 200, "a bad tool name is a JSON-RPC error; HTTP 400 is reserved for the envelope"
    assert _err(r)["code"] == -32602


def test_a_door_refuses_a_tool_outside_its_set_in_the_modern_era_too(client):
    outside = next(t for t in ("send_message", "find_business") if t not in profiles.tools_for("sanctions-screening"))
    r = _post(client, "tools/call", {"name": outside, "arguments": {}}, name=outside, door="sanctions-screening")
    assert _err(r)["code"] == -32602 and "not available on this endpoint" in _err(r)["message"]


def test_resources_list_and_read(client):
    lst = _result(_post(client, "resources/list"))
    assert lst["resultType"] == "complete" and lst["cacheScope"] == "public"
    assert lst["ttlMs"] == m26.CACHE_TTL_MS["resources/list"]
    uri = lst["resources"][0]["uri"]
    rd = _post(client, "resources/read", {"uri": uri}, name=uri)
    assert rd.status_code == 200
    res = _result(rd)
    assert res["resultType"] == "complete" and res["cacheScope"] == "public"
    assert res["ttlMs"] == m26.CACHE_TTL_MS["resources/read"]
    assert res["contents"][0]["uri"] == uri


def test_resource_not_found_is_invalid_params_in_this_revision(client):
    """-32002 was the old code; the revision moved it to -32602 and forbids emitting -32002."""
    r = _post(client, "resources/read", {"uri": "agent-broker://nope"}, name="agent-broker://nope")
    assert _err(r)["code"] == -32602


def test_resources_templates_list_exists_and_is_honestly_empty(client):
    """We declare `resources`, so this method has to exist. 151 calls got -32601 in 47 hours."""
    for resp in (_post(client, "resources/templates/list"), _legacy(client, "resources/templates/list")):
        assert resp.status_code == 200
        assert _result(resp)["resourceTemplates"] == []
    modern = _result(_post(client, "resources/templates/list"))
    assert modern["resultType"] == "complete" and modern["cacheScope"] == "public"
    assert modern["ttlMs"] == m26.CACHE_TTL_MS["resources/templates/list"]
    assert set(_result(_legacy(client, "resources/templates/list"))) == {"resourceTemplates"}


def test_prompts_list_and_get(client):
    lst = _result(_post(client, "prompts/list"))
    assert lst["resultType"] == "complete" and lst["cacheScope"] == "public"
    assert lst["ttlMs"] == m26.CACHE_TTL_MS["prompts/list"]
    name = lst["prompts"][0]["name"]
    got = _post(client, "prompts/get", {"name": name}, name=name)
    assert got.status_code == 200
    res = _result(got)
    assert res["resultType"] == "complete" and res["messages"]
    assert "ttlMs" not in res, "prompts/get is not one of the cacheable methods"


def test_ping_was_removed_by_the_revision_and_is_refused_only_in_its_own_envelope(client):
    """Monitors use ping and answering costs nothing, so legacy callers and the header-only shape (what
    monitors that stamp the new header on everything send) still get an answer, byte-identical for legacy.
    A request carrying the revision's own _meta envelope has opted in, and the spec's answer is 404 / -32601."""
    assert _result(_legacy(client, "ping")) == {}
    header_only = _post(client, "ping", meta=False)
    assert header_only.status_code == 200 and _result(header_only)["resultType"] == "complete"
    enveloped = _post(client, "ping")
    assert enveloped.status_code == 404 and _err(enveloped)["code"] == -32601


# ---------------------------------------------------------------------------
# the refusals the revision defines
# ---------------------------------------------------------------------------

def test_unsupported_version_in_meta_is_400_with_the_supported_list(client):
    r = _post(client, "tools/list", meta=_meta(version="2027-01-01"), version="2027-01-01")
    assert r.status_code == 400
    err = _err(r)
    assert err["code"] == -32022
    assert err["message"] == "Unsupported protocol version"
    assert err["data"] == {"supported": list(mcp_server.ALL_PROTOCOL_VERSIONS), "requested": "2027-01-01"}


def test_the_requested_version_is_bounded_in_the_echo(client):
    r = _post(client, "tools/list", meta=_meta(version="x" * 300), version="x" * 300)
    assert r.status_code == 400
    assert len(_err(r)["data"]["requested"]) == 64


@pytest.mark.parametrize("bad", [None, 5, ["2026-07-28"], "", {"a": 1}])
def test_a_non_string_protocol_version_is_invalid_params(client, bad):
    meta = _meta()
    meta[M + "protocolVersion"] = bad
    r = _post(client, "tools/list", meta=meta)
    assert r.status_code == 400 and _err(r)["code"] == -32602


@pytest.mark.parametrize("caps", [False, "x", 5, [], None])
def test_missing_or_malformed_clientCapabilities_is_invalid_params_for_every_method_but_discover(client, caps):
    meta = _meta(caps=False)
    if caps is not False:
        meta[M + "clientCapabilities"] = caps
    r = _post(client, "tools/list", meta=meta)
    assert r.status_code == 400
    err = _err(r)
    assert err["code"] == -32602
    assert "clientCapabilities" in err["message"]


def test_clientInfo_is_optional(client):
    assert _post(client, "tools/list", meta=_meta(info=False)).status_code == 200


def test_empty_clientCapabilities_object_is_valid(client):
    assert _post(client, "tools/list", meta=_meta(caps=True)).status_code == 200


@pytest.mark.parametrize("drop", ["MCP-Protocol-Version", "Mcp-Method"])
def test_a_missing_required_header_is_header_mismatch(client, drop):
    r = _post(client, "tools/list", drop=(drop,))
    assert r.status_code == 400
    assert _err(r)["code"] == -32020 == m26.ERR_HEADER_MISMATCH
    assert drop.lower() in _err(r)["message"].lower()


def test_a_protocol_version_header_that_disagrees_with_the_body_is_header_mismatch(client):
    r = _post(client, "tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 400 and _err(r)["code"] == -32020


def test_an_mcp_method_header_that_disagrees_with_the_body_is_header_mismatch(client):
    r = _post(client, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400 and _err(r)["code"] == -32020


@pytest.mark.parametrize("method,params,field", [
    ("tools/call", {"name": "preview_cost", "arguments": {}}, "name"),
    ("prompts/get", {"name": "cost_estimation"}, "name"),
    ("resources/read", {"uri": "agent-broker://cookbook"}, "uri"),
])
def test_mcp_name_is_required_and_must_mirror_the_body(client, method, params, field):
    assert _post(client, method, params, name=params[field]).status_code == 200
    missing = _post(client, method, params)
    assert missing.status_code == 400 and _err(missing)["code"] == -32020
    wrong = _post(client, method, params, name="something-else")
    assert wrong.status_code == 400 and _err(wrong)["code"] == -32020
    assert field in _err(wrong)["message"]


def test_mcp_name_may_be_base64_encoded_and_is_decoded_before_comparing(client):
    """Spec: a value that cannot travel as plain ASCII is sent as =?base64?...?= and the server MUST decode it."""
    enc = "=?base64?" + base64.b64encode(b"preview_cost").decode() + "?="
    ok = _post(client, "tools/call", {"name": "preview_cost", "arguments": {"operation": "send_message", "params": {}}},
               name=enc)
    assert ok.status_code == 200 and "result" in ok.json()
    wrong = "=?base64?" + base64.b64encode(b"screen_sanctions").decode() + "?="
    bad = _post(client, "tools/call", {"name": "preview_cost", "arguments": {}}, name=wrong)
    assert bad.status_code == 400 and _err(bad)["code"] == -32020
    broken = _post(client, "tools/call", {"name": "preview_cost", "arguments": {}}, name="=?base64?%%%not-base64?=")
    assert broken.status_code == 400 and _err(broken)["code"] == -32020


def test_decode_header_value_unit():
    assert m26.decode_header_value("plain") == "plain"
    assert m26.decode_header_value("=?base64?" + base64.b64encode("héllo 世界".encode()).decode() + "?=") == "héllo 世界"
    assert m26.decode_header_value("=?base64?!!!?=") is None
    assert m26.decode_header_value("=?base64?/w==?=") is None, "invalid UTF-8 is not a valid value"


def test_error_messages_never_echo_a_header_value(client):
    hostile = "<script>alert(1)</script>" + "A" * 500
    for resp in (
        _post(client, "tools/call", {"name": "preview_cost", "arguments": {}}, name=hostile),
        _post(client, "tools/list", headers={"MCP-Protocol-Version": "2026-07-28<script>alert(1)</script>"}),
        _post(client, "tools/list", headers={"Mcp-Method": "<script>x</script>"}),
    ):
        assert resp.status_code == 400
        assert "<script" not in resp.text and "AAAA" not in resp.text, resp.text


def test_an_unknown_method_is_404_with_method_not_found_in_the_modern_era_only(client):
    modern = _post(client, "no/such/method")
    assert modern.status_code == 404 and _err(modern)["code"] == -32601
    legacy = _legacy(client, "no/such/method")
    assert legacy.status_code == 200 and _err(legacy)["code"] == -32601


@pytest.mark.parametrize("method", ["logging/setLevel", "resources/subscribe", "resources/unsubscribe",
                                    "subscriptions/listen", "completion/complete", "tasks/get", "tasks/list"])
def test_methods_we_do_not_implement_say_so_honestly(client, method):
    """No subscriptions stream, no tasks extension, no logging: -32601, not a stub that pretends."""
    r = _post(client, method)
    assert r.status_code == 404 and _err(r)["code"] == -32601


def test_mcp_session_id_is_ignored_and_never_minted_or_echoed(client):
    r = _post(client, "tools/list", headers={"Mcp-Session-Id": "abc123"})
    assert r.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in r.headers}
    init = _legacy(client, "initialize", {"protocolVersion": "2025-11-25"})
    assert "mcp-session-id" not in {k.lower() for k in init.headers}


def test_get_and_delete_on_the_endpoint_are_405(client):
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


def test_a_notification_is_still_202_even_with_modern_headers(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
                    headers={"MCP-Protocol-Version": V})
    assert r.status_code == 202 and r.content == b""


def test_the_http_error_body_is_json(client):
    r = _post(client, "tools/list", version="2025-11-25")
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    assert set(r.json()) == {"jsonrpc", "id", "error"}


# ---------------------------------------------------------------------------
# the callers asking for this revision today, and the legacy ones that must be left alone
# ---------------------------------------------------------------------------

def test_header_only_modern_request_is_served_in_the_modern_shape(client):
    """scanner-B, scanner-C and others send the 2026-07-28 header and ask for tools/list with no envelope.
    They get a list today; rejecting them would break callers that work."""
    r = _post(client, "tools/list", meta=False)
    assert r.status_code == 200
    res = _result(r)
    assert res["resultType"] == "complete" and res["cacheScope"] == "public" and len(res["tools"]) > 5


def test_header_only_modern_request_without_mcp_method_is_served(client):
    r = _post(client, "tools/list", meta=False, drop=("Mcp-Method",))
    assert r.status_code == 200


def test_header_only_modern_request_still_has_the_headers_it_sends_checked(client):
    r = _post(client, "tools/list", meta=False, headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400 and _err(r)["code"] == -32020


def test_header_only_modern_tools_call_without_a_name_header_is_served(client):
    r = _post(client, "tools/call", {"name": "preview_cost", "arguments": {"operation": "send_message", "params": {}}},
              meta=False)
    assert r.status_code == 200 and _result(r)["resultType"] == "complete"


def test_legacy_requests_are_never_header_validated(client):
    """scanner-I sends an Mcp-Method header on 2025-06-18 requests (about 11,000 in four days). They are
    legacy; a wrong header there must change nothing."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "2025-06-18", "Mcp-Method": "something/else", "Mcp-Name": "zzz"})
    assert r.status_code == 200 and set(_result(r)) == {"tools"}


def test_a_legacy_version_named_in_the_envelope_is_served_as_legacy(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                  "params": {"_meta": {M + "protocolVersion": "2025-06-18"}}},
                    headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200 and set(_result(r)) == {"tools"}


def test_an_unrecognised_legacy_method_header_is_still_served(client):
    """Only discover treats an unknown version header as a refusal; legacy methods keep ignoring it."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 200 and set(_result(r)) == {"tools"}


def test_a_batch_of_header_only_modern_members_is_served_per_member_without_header_checks(client):
    """One HTTP request has one set of headers; they cannot mirror several bodies. (A batch whose members carry
    the modern ENVELOPE is refused whole - see the gate tests at the end of this file.)"""
    body = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}]
    r = client.post("/mcp", json=body, headers={"MCP-Protocol-Version": V})
    assert r.status_code == 200
    out = r.json()
    assert [x["id"] for x in out] == [1, 2]
    assert all(x["result"]["resultType"] == "complete" for x in out)


def test_every_door_answers_discover_and_a_modern_tools_list(client):
    assert DOORS, "there must be doors to test"
    for door in DOORS:
        assert _post(client, "server/discover", door=door).status_code == 200, door
        assert _post(client, "tools/list", door=door).status_code == 200, door


def test_an_unknown_door_is_still_404(client):
    assert _post(client, "server/discover", door="not-a-door").status_code == 404


# ---------------------------------------------------------------------------
# result shaping (pure)
# ---------------------------------------------------------------------------

def test_decorating_a_result_never_mutates_the_original():
    """A tools/call result can be the object the idempotency gate stored for replay. Decorating it in place
    would bake this request's fields into every later replay, including a legacy-era replay of the same key."""
    stored = {"content": [{"type": "text", "text": "x"}], "isError": False}
    snapshot = copy.deepcopy(stored)
    info = m26.server_info("agent-broker", "1")
    first = m26.decorate_result("tools/call", stored, info)
    second = m26.decorate_result("tools/call", stored, info)
    assert stored == snapshot and first is not stored
    first["_meta"]["poke"] = 1
    assert "poke" not in second["_meta"]


def test_decorating_keeps_the_results_own_meta():
    """The x402 paid-tool path returns its own `_meta`; serverInfo is added beside it, never over it."""
    out = m26.decorate_result("tools/call", {"content": [], "_meta": {"x402/payment-response": {"ok": True}}},
                              m26.server_info("a", "1"))
    assert out["_meta"]["x402/payment-response"] == {"ok": True}
    assert out["_meta"][SERVER_INFO] == {"name": "a", "version": "1"}


def test_decorating_never_overrides_a_result_type_the_handler_set():
    out = m26.decorate_result("tools/call", {"resultType": "input_required"}, m26.server_info("a", "1"))
    assert out["resultType"] == "input_required"


def test_only_the_methods_the_spec_names_get_cache_hints():
    assert set(m26.CACHE_TTL_MS) == {"server/discover", "tools/list", "prompts/list", "resources/list",
                                     "resources/templates/list", "resources/read"}
    assert all(isinstance(v, int) and v >= 0 for v in m26.CACHE_TTL_MS.values())
    for method in ("tools/call", "prompts/get", "ping"):
        assert "ttlMs" not in m26.decorate_result(method, {}, m26.server_info("a", "1"))


# ---------------------------------------------------------------------------
# resolve_era, as a decision table
# ---------------------------------------------------------------------------

LEG = mcp_server.SUPPORTED_PROTOCOL_VERSIONS


def _era(method, params, headers, **kw):
    return m26.resolve_era(method, params, {k.lower(): v for k, v in headers.items()}, LEG, **kw)


def test_resolve_era_decision_table():
    full = {"_meta": _meta()}
    h = {"MCP-Protocol-Version": V, "Mcp-Method": "tools/list"}
    assert _era("tools/list", full, h) == m26.Era(
        modern=True, envelope=True, version=V, client_info={"name": "pytest-modern", "version": "9.9"})
    assert _era("tools/list", None, {}) == m26.Era()
    assert _era("tools/list", {}, {"MCP-Protocol-Version": "2025-06-18"}) == m26.Era()
    assert _era("tools/list", {}, {"MCP-Protocol-Version": V}) == m26.Era(modern=True, version=V)
    assert _era("initialize", full, h) == m26.Era(), "initialize is always legacy"
    assert _era(5, full, h) == m26.Era(), "a non-string method is not ours to classify"
    rej = _era("tools/list", {"_meta": _meta(version="2099-01-01")}, {"MCP-Protocol-Version": "2099-01-01"})
    assert isinstance(rej, m26.Rejection) and (rej.http_status, rej.code) == (400, -32022)
    # check_headers=False (batch members): the envelope is still held to its rules, the headers are not
    assert _era("tools/list", full, {}, check_headers=False).modern is True
    assert isinstance(_era("tools/list", {"_meta": _meta(caps=False)}, {}, check_headers=False), m26.Rejection)


def test_resolve_era_never_raises_on_hostile_input():
    hostile = [None, 5, "x", [], {"_meta": 5}, {"_meta": []}, {"_meta": {M + "clientInfo": 5}},
               {"_meta": {M + "protocolVersion": {"a": 1}}}, {"name": 5, "_meta": _meta()}]
    for params in hostile:
        for method in ("tools/call", "server/discover", "resources/read", "prompts/get", "x"):
            for headers in ({}, {"mcp-protocol-version": V}, {"mcp-protocol-version": 5}, {"mcp-method": 7}):
                out = m26.resolve_era(method, params, headers, LEG)
                assert isinstance(out, (m26.Era, m26.Rejection))


# ---------------------------------------------------------------------------
# the outcome log
# ---------------------------------------------------------------------------

def test_modern_identity_comes_from_the_envelope_with_no_prior_initialize(client, events):
    """The revision has no handshake to remember a client from. clientInfo coverage was 80% of events; the
    envelope carries it on every request, so the first request of a connection is named."""
    _post(client, "tools/list", headers={"User-Agent": "a-brand-new-ua/1"})
    e = events[-1]
    assert (e.client_name, e.client_version) == ("pytest-modern", "9.9")
    assert e.method == "tools/list" and e.outcome == "ok" and e.http_status == 200


def test_the_modern_protocol_version_is_recorded_in_detail_and_legacy_rows_are_unchanged(client, events):
    _post(client, "tools/list", door="compliance-check")
    assert events[-1].detail == "door=compliance-check pv=2026-07-28"
    _post(client, "tools/list")
    assert events[-1].detail == "pv=2026-07-28"
    _legacy(client, "tools/list", door="compliance-check")
    assert events[-1].detail == "door=compliance-check", "a legacy row keeps exactly its old text"
    _legacy(client, "tools/list")
    assert events[-1].detail is None


def test_refusals_are_recorded_with_their_real_http_status_and_a_label(client, events):
    _post(client, "tools/list", version="2025-11-25")
    _post(client, "tools/list", meta=_meta(version="2027-01-01"), version="2027-01-01")
    _post(client, "tools/list", meta=_meta(caps=False))
    _post(client, "no/such")
    got = [(e.outcome, e.error_code, e.http_status) for e in events]
    assert got == [("rpc_error", "header_mismatch", 400),
                   ("rpc_error", "unsupported_protocol_version", 400),
                   ("rpc_error", "invalid_meta", 400),
                   ("rpc_error", "method_not_found", 404)]


def test_a_refused_request_still_names_the_client_that_sent_it(client, events):
    _post(client, "tools/list", version="2025-11-25", headers={"User-Agent": "another-new-ua/2"})
    assert events[-1].client_name == "pytest-modern"


def test_the_database_accepts_every_outcome_these_paths_emit(client, events):
    """usage_events_insert_v2 validates `outcome` against a fixed list (migrations/spine/010) and this change
    ships WITHOUT a migration, so every new path must use an outcome already on that list."""
    _post(client, "server/discover")
    _post(client, "tools/list", version="2025-11-25")
    _post(client, "no/such")
    _post(client, "tools/list", meta=_meta(caps=False))
    assert events and {e.outcome for e in events} <= set(ul.OUTCOMES)
    sql = (ROOT / "migrations" / "spine" / "010_usage_events_notification_outcome.sql").read_text(encoding="utf-8")
    allowed = set(re.findall(r"'([a-z_]+)'", re.search(r"p_outcome\s+not in\s*\((.*?)\)\s*then", sql, re.S).group(1)))
    assert {e.outcome for e in events} <= allowed


def test_discover_is_discovery_not_work():
    """A keyed discover must not inflate the work counts; a keyless one is a crawler-shaped event."""
    for method in ("server/discover", "resources/templates/list"):
        assert ul.classify_session_kind(method, None, "pytest", "some-key-id") == "crawler"
        assert ul.classify_session_kind(method, None, "pytest", None) == "crawler"


# ---------------------------------------------------------------------------
# the Cloudflare edge worker (not in the live path since the VPS cutover; kept from serving stale shapes)
# ---------------------------------------------------------------------------

def test_the_edge_worker_sends_modern_requests_to_the_origin():
    ts = (ROOT / "edge" / "src" / "mcp-edge.ts").read_text(encoding="utf-8")
    assert "isModernRequest(" in ts
    assert re.search(r"if \(!EDGE_MCP_METHODS\.has\(method\) \|\| modern\)", ts), (
        "a modern request must leave the snapshot fast path: the snapshot has no resultType / ttlMs / cacheScope")
    block = re.search(r"const MODERN_PROTOCOL_VERSIONS\s*=\s*\[(.*?)\]", ts, re.S)
    assert block, "edge no longer declares MODERN_PROTOCOL_VERSIONS"
    assert re.findall(r'"([^"]+)"', block.group(1)) == list(m26.MODERN_PROTOCOL_VERSIONS)
    assert f'"{m26.META_PROTOCOL_VERSION}"' in ts
    assert 'method === "initialize"' in ts, "initialize must never be classed as modern at the edge either"
    assert "mcp-protocol-version, mcp-method, mcp-name" in ts, "CORS must allow the new request-metadata headers"


def test_the_edge_legacy_version_list_does_not_contain_a_modern_version():
    ts = (ROOT / "edge" / "src" / "mcp-edge.ts").read_text(encoding="utf-8")
    block = re.search(r"const SUPPORTED_PROTOCOL_VERSIONS\s*=\s*\[(.*?)\]", ts, re.S).group(1)
    assert V not in re.findall(r'"([^"]+)"', block)


# ---------------------------------------------------------------------------
# the documentation says what the code does
# ---------------------------------------------------------------------------

def test_the_doc_lists_exactly_the_versions_and_methods_we_serve():
    doc = (ROOT / "docs" / "MCP-2026-07-28.md").read_text(encoding="utf-8")
    for v in mcp_server.ALL_PROTOCOL_VERSIONS:
        assert v in doc, v
    for method in ("server/discover", "resources/templates/list"):
        assert method in doc
    for method in ("subscriptions/listen", "tasks/get", "logging/setLevel"):
        assert method in doc, f"the doc must say plainly that {method} is not implemented"


# ---------------------------------------------------------------------------
# gate findings, 2026-10-03
# ---------------------------------------------------------------------------

def test_a_batch_whose_members_carry_the_modern_envelope_is_refused_whole(client):
    """The revision's transport says the POST body MUST be one request. Wrapping a request in an array also
    skipped every header check, so a modern tools/call could be sent under 'Mcp-Method: tools/list' and be
    served. Now the array is refused: 400, -32600, one error object, nothing dispatched."""
    body = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "preview_cost", "arguments": {}, "_meta": _meta()}}]
    r = client.post("/mcp", json=body, headers={"MCP-Protocol-Version": V, "Mcp-Method": "tools/list"})
    assert r.status_code == 400 and isinstance(r.json(), dict)
    assert r.json()["error"]["code"] == -32600
    mixed = body + [{"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    r2 = client.post("/mcp", json=mixed)
    assert r2.status_code == 400 and r2.json()["error"]["code"] == -32600
    assert "result" not in r2.json()


def test_a_legacy_batch_is_still_served_per_member(client):
    body = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"_meta": {M + "protocolVersion": "2025-06-18"}}}]
    r = client.post("/mcp", json=body)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [1, 2, 3]
    assert all("resultType" not in x["result"] for x in r.json())


def test_the_version_header_must_agree_with_the_envelope_even_when_the_envelope_names_a_legacy_version(client):
    """MCP-Protocol-Version: 2026-07-28 with _meta protocolVersion 2024-11-05 contradicts itself; the revision's
    rule is HeaderMismatch, and the old behaviour (served as legacy, nothing said) hid the contradiction."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {M + "protocolVersion": "2024-11-05"}}}
    r = client.post("/mcp", json=body, headers={"MCP-Protocol-Version": V})
    assert r.status_code == 400 and _err(r)["code"] == -32020
    # a legacy header that agrees with a legacy envelope, or a legacy header with a modern-looking Mcp-Method, is untouched
    ok = client.post("/mcp", json=body, headers={"MCP-Protocol-Version": "2024-11-05", "Mcp-Method": "whatever"})
    assert ok.status_code == 200 and set(_result(ok)) == {"tools"}


def test_an_mcp_name_header_with_no_string_name_in_the_body_is_header_mismatch(client):
    for params in ({}, {"name": 5}, {"name": None}, {"name": ["preview_cost"]}):
        r = _post(client, "tools/call", params, headers={"Mcp-Name": "preview_cost"})
        assert r.status_code == 400 and _err(r)["code"] == -32020, params
    # header-only callers are still only checked for what they send AND the body has
    h = {"MCP-Protocol-Version": V, "Mcp-Name": "preview_cost"}
    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, headers=h)
    assert r2.status_code != 400 or _err(r2)["code"] != -32020


def test_the_probe_docstring_says_who_must_be_told_about_its_user_agent():
    """The docstring claimed the 'hatchloop-' prefix makes the outcome log class the probe as our own
    infrastructure. Nothing in this repository does; HatchLoop's traffic audit matches own-infra user agents by
    EXACT string. So the docstring now names the list, and the probe's user agent is the string to register."""
    text = (ROOT / "scripts" / "probe_mcp_2026.py").read_text(encoding="utf-8")
    assert "so the outcome log classes these requests as our own infrastructure" not in text
    assert "OWN_INFRA_UA_EXACT" in text and "hatchloop-mcp-2026-probe/1" in text
