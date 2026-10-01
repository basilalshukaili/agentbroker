"""Every MCP request leaves a row saying how it went - not only the ones that succeeded.

THE GAP (audit 2026-09-30): usage_events was written only AFTER a handler returned. A tool failure, a
bad argument, an unknown tool, a crash, an HTTP 429 and an HTTP 502 all wrote nothing, so "did the
people holding keys get what they wanted?" had no answer for any period; four keyed crashes on
2026-09-01 left no row at all and were visible only because the credit ledger happened to log a refund.

These tests drive handle_mcp_request and the HTTP middleware with the transport mocked (no network,
no Supabase) and pin: one event per request; the right outcome and error code on every exit; argument
NAMES only, never values; the key state; latency; client name carried from initialize; unknown tool
names recorded safely; 429 floods throttled; and the RPC payload matching migration 009's signature.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent_interface import request_observer as ro
from agent_interface.identity import issue_token, TokenRequest
from agent_interface.mcp_server import handle_mcp_request
from billing import usage_logger as ul

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "spine" / "009_keyholder_outcome_logging_and_compliance_rpcs.sql"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def events(monkeypatch):
    """Capture every UsageEvent instead of sending it anywhere."""
    got = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: got.append(e))
    return got


def _call(method, params=None, headers=None, payload_extra=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    payload.update(payload_extra or {})
    return _run(handle_mcp_request(payload, headers=headers or {"user-agent": "pytest-client/1"}))


def _tool(name, arguments, headers=None):
    return _call("tools/call", {"name": name, "arguments": arguments}, headers)


# ---------------------------------------------------------------------------
# one event per request, right outcome at every exit
# ---------------------------------------------------------------------------

def test_a_successful_call_is_logged_ok_with_latency_and_arg_names(events):
    _tool("preview_cost", {"operation": "send_message", "params": {}})
    assert len(events) == 1
    e = events[0]
    assert (e.method, e.tool_name, e.outcome) == ("tools/call", "preview_cost", "ok")
    assert e.error_code is None
    assert e.http_status == 200
    assert isinstance(e.latency_ms, int) and e.latency_ms >= 0
    assert e.arg_names == ["operation", "params"]
    assert e.key_state == "none"


def test_a_tool_that_ran_and_failed_is_logged_as_a_failure_not_a_success(events):
    """isError results used to be filed under 'succeeded' because the handler returned normally."""
    r = _tool("capture_lead", {"smb_id": "no_such_smb", "prospect": {"name": "A"}})["result"]
    assert r["isError"] is True
    e = events[-1]
    assert e.outcome == "tool_failure"
    assert e.error_code, "the typed reason_code of the receipt must be kept"


def test_a_typed_tool_error_is_logged_with_its_error_code(events, monkeypatch):
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    _tool("capture_lead", {"smb_id": "x", "prospect": {"name": "A"}})
    e = events[-1]
    assert e.outcome == "tool_error"
    assert e.error_code == "auth_required"


def test_an_unknown_tool_is_logged_with_the_name_asked_for(events):
    """The only way to learn which tools callers expect us to have: 0 unknown names had ever
    reached usage_events because the request raised before the log call."""
    r = _tool("find_buisness", {"vertical": "plumbing"})
    assert r["error"]["code"] == -32602
    e = events[-1]
    assert e.outcome == "rpc_error" and e.error_code == "unknown_tool"
    assert e.tool_name is None, "an unknown name must not be filed as if it were one of our tools"
    assert e.requested_name == "find_buisness"


def test_an_unknown_tool_name_that_looks_like_a_secret_is_not_stored(events):
    secret = "eyJ" + "A" * 80
    _tool(secret, {})
    e = events[-1]
    assert e.requested_name == "<invalid>"
    assert secret not in repr(e)


def test_bad_arguments_are_a_protocol_error_with_a_code(events):
    _tool("preview_cost", {})                      # missing required argument
    e = events[-1]
    assert e.outcome == "rpc_error"
    assert e.error_code in ("missing_argument", "invalid_argument")


def test_arguments_that_are_not_an_object_are_logged(events):
    _call("tools/call", {"name": "preview_cost", "arguments": ["not", "an", "object"]})
    e = events[-1]
    assert e.outcome == "rpc_error" and e.error_code == "invalid_argument"
    assert e.arg_names == []


def test_missing_method_and_unknown_method_are_logged(events):
    r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1}, headers={"user-agent": "x"}))
    assert r["error"]["code"] == -32600
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "invalid_request")
    _call("nonsense/method")
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "method_not_found")


def test_a_crash_inside_a_tool_is_logged_with_the_exception_class_only(events, monkeypatch):
    """Four keyed crashes on 2026-09-01 left no usage row and no operation row."""
    import agent_interface.mcp_server as m

    async def _boom(*a, **k):
        raise ZeroDivisionError("secret-ish detail: +15551230000")
    monkeypatch.setitem(m._METHOD_HANDLERS, "tools/call", _boom)
    r = _tool("preview_cost", {"operation": "send_message", "params": {}})
    assert r["error"]["code"] == -32603
    e = events[-1]
    assert (e.outcome, e.error_code) == ("exception", "ZeroDivisionError")
    assert "+15551230000" not in repr(e), "the exception MESSAGE can quote caller input; never store it"


def test_every_method_is_logged_once_not_twice(events):
    for method, params in (("initialize", {"protocolVersion": "2025-06-18"}), ("tools/list", {}),
                           ("ping", {}), ("resources/list", {}), ("prompts/list", {})):
        before = len(events)
        _call(method, params)
        assert len(events) == before + 1, method
        assert events[-1].outcome == "ok"


# ---------------------------------------------------------------------------
# what is and is not recorded
# ---------------------------------------------------------------------------

def test_argument_values_never_reach_the_database(monkeypatch):
    """Names only. The row sent to the RPC must contain none of the values."""
    sent = []

    async def _rpc(fn, payload):
        sent.append((fn, payload))
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)

    secret_phone, secret_body = "+96891234567", "my private message body"
    ev = ul.UsageEvent(
        method="tools/call", tool_name="send_message",
        arguments={"recipient": {"id_value": secret_phone}, "content": {"body": secret_body}},
        ip="203.0.113.7", user_agent="UA", key_id=None, outcome="tool_failure",
        error_code="compliance_violation", http_status=200, latency_ms=12,
        key_state="none", arg_names=ro.safe_arg_names({"recipient": 1, "content": 2}))
    _run(ul.log_usage_outcome(ev))
    assert len(sent) == 1
    blob = json.dumps(sent[0][1])
    assert secret_phone not in blob and secret_body not in blob
    assert "203.0.113.7" not in blob, "the address is hashed, never sent"
    assert sent[0][1]["p_arg_names"] == ["content", "recipient"]
    assert sent[0][1]["p_args_hash"], "an 8-char hash of the arguments is still kept, as before"


def test_argument_names_that_could_carry_a_secret_are_masked():
    names = ro.safe_arg_names({
        "operation": 1,
        "sk_live_" + "a1" * 20: 2,                # long hex-ish blob used as a key name
        "eyJhbGciOiJIUzI1NiJ9": 3,                # JWT-shaped
        "has space and $ymbols": 4,
    })
    assert "operation" in names
    assert names.count("<other>") == 1 and len(names) == 2     # all three masked into ONE marker
    assert not any("sk_live" in n or "eyJ" in n for n in names)


def test_arg_names_are_bounded():
    names = ro.safe_arg_names({f"arg{i}": i for i in range(200)})
    assert len(names) <= 40


def test_non_dict_arguments_have_no_names():
    for bad in (None, [], "x", 5):
        assert ro.safe_arg_names(bad) == []


def test_key_state_and_key_id_are_logged_for_a_valid_key(events):
    key = issue_token(TokenRequest(agent_id="free_outcome_keyed", principal_id="p")).token
    _tool("check_quota", {}, {"x-agent-identity": key, "user-agent": "x"})
    e = events[-1]
    assert e.key_state == "valid"
    assert e.key_id == "free_outcome_keyed"


def test_key_state_is_logged_for_a_placeholder_and_no_key_id_is_invented(events):
    _tool("check_quota", {}, {"x-agent-identity": "env:KEY", "user-agent": "x"})
    e = events[-1]
    assert e.key_state == "placeholder"
    assert e.key_id in (None, "anonymous")


def test_client_name_from_initialize_is_carried_to_the_next_call(events):
    """The handshake and the tool call are separate HTTP requests; a failing call is attributed to
    the client that made it, not just to 'node'."""
    ro.CLIENTS._data.clear()
    h = {"user-agent": "node", "x-forwarded-for": "198.51.100.20"}
    _call("initialize", {"protocolVersion": "2025-06-18",
                         "clientInfo": {"name": "Cursor", "version": "0.45.1"}}, h)
    assert events[-1].client_name == "Cursor" and events[-1].client_version == "0.45.1"
    _tool("get_status", {"operation_id": "nope"}, h)
    assert events[-1].client_name == "Cursor"
    # a different caller is not misattributed
    _tool("get_status", {"operation_id": "nope"}, {"user-agent": "node", "x-forwarded-for": "198.51.100.99"})
    assert events[-1].client_name is None


def test_client_info_is_sanitised_and_bounded():
    name, version = ro.safe_client_info({"clientInfo": {"name": "Evil\nClient\x00<script>" + "x" * 500,
                                                        "version": "1.0\r\n"}})
    assert "\n" not in name and "\x00" not in name and "<" not in name
    assert len(name) <= 128 and version == "1.0"
    assert ro.safe_client_info({"clientInfo": "nope"}) == (None, None)
    assert ro.safe_client_info(None) == (None, None)


def test_client_registry_expires_and_is_bounded():
    reg = ro.ClientRegistry(ttl_s=10, max_entries=3)
    for i in range(5):
        reg.remember(f"fp{i}", f"c{i}", "1", now=100.0)
    assert len(reg._data) == 3 and reg.recall("fp4", now=101.0)[0] == "c4"
    assert reg.recall("fp4", now=111.0) == (None, None), "entries expire"


def test_the_address_logged_is_one_hop_never_the_chain(events):
    _call("ping", {}, {"user-agent": "x", "x-forwarded-for": "203.0.113.5, 10.0.0.1, 10.0.0.2"})
    assert events[-1].ip == "203.0.113.5"


def test_door_is_recorded(events):
    _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                            headers={"user-agent": "x"}, profile="sanctions-screening"))
    assert events[-1].detail == "door=sanctions-screening"


# ---------------------------------------------------------------------------
# the logger itself
# ---------------------------------------------------------------------------

def test_log_usage_outcome_sends_v2_with_every_field(monkeypatch):
    sent = []

    async def _rpc(fn, payload):
        sent.append((fn, payload))
        return {"id": 7}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = 0.0
    before = ul.get_usage_logger_health()["succeeded"]
    _run(ul.log_usage_outcome(ul.UsageEvent(
        method="tools/call", tool_name="screen_sanctions", ip="198.51.100.1", user_agent="UA",
        key_id="free_x", principal_type="human", outcome="ok", http_status=200, latency_ms=33,
        client_name="C", client_version="1", key_state="valid", arg_names=["name"],
        requested_name=None, detail="door=sanctions-screening")))
    fn, p = sent[0]
    assert fn == "usage_events_insert_v2"
    assert p["p_outcome"] == "ok" and p["p_latency_ms"] == 33 and p["p_key_state"] == "valid"
    assert p["p_session_kind"] == "verified_human_key"
    assert ul.get_usage_logger_health()["succeeded"] == before + 1


def test_log_usage_outcome_rejects_an_unknown_outcome_by_defaulting_not_crashing(monkeypatch):
    sent = []

    async def _rpc(fn, payload):
        sent.append(payload)
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = 0.0
    _run(ul.log_usage_outcome(ul.UsageEvent(method="x", outcome="made-up")))
    assert sent[0]["p_outcome"] == "ok"


def test_if_the_v2_function_is_missing_it_falls_back_to_v1_and_says_so(monkeypatch, caplog):
    """Code deployed ahead of the migration, or a database rolled back: logging must degrade to
    the 7-field insert, loudly, never go dark."""
    calls = []

    async def _rpc(fn, payload):
        calls.append(fn)
        if fn == "usage_events_insert_v2":
            raise RuntimeError("rpc('usage_events_insert_v2') failed: HTTP 404 body={\"code\":\"PGRST202\"}")
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = 0.0
    with caplog.at_level("ERROR"):
        _run(ul.log_usage_outcome(ul.UsageEvent(method="tools/call", tool_name="find_business")))
        _run(ul.log_usage_outcome(ul.UsageEvent(method="tools/call", tool_name="find_business")))
    assert calls == ["usage_events_insert_v2", "usage_events_insert", "usage_events_insert"], calls
    assert "usage_log_v2_missing" in caplog.text
    ul._v2_missing_until = 0.0


def test_a_real_failure_is_not_mistaken_for_a_missing_function(monkeypatch):
    calls = []

    async def _rpc(fn, payload):
        calls.append(fn)
        raise RuntimeError("rpc failed: HTTP 503")
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = 0.0
    before = ul.get_usage_logger_health()["failed"]
    _run(ul.log_usage_outcome(ul.UsageEvent(method="ping")))
    assert calls == ["usage_events_insert_v2"], "a 503 must not trigger the v1 fallback"
    assert ul.get_usage_logger_health()["failed"] == before + 1


def test_the_rpc_payload_matches_the_migrations_signature():
    """Producer/consumer contract: every key the code sends must be a parameter of the function
    migration 009 creates. A renamed argument would 404 at runtime and tests would stay green."""
    sql = MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"create or replace function public\.usage_events_insert_v2\((.*?)\)\s*returns", sql, re.S)
    sql_params = set(re.findall(r"\b(p_[a-z_]+)\b", m.group(1)))

    sent = []

    async def _rpc(fn, payload):
        sent.append(payload)
        return {"id": 1}
    import storage.supabase_client as sb
    orig = sb.rpc
    sb.rpc = _rpc
    try:
        ul._v2_missing_until = 0.0
        _run(ul.log_usage_outcome(ul.UsageEvent(method="tools/call", tool_name="x")))
    finally:
        sb.rpc = orig
    assert set(sent[0]) == sql_params, (set(sent[0]) ^ sql_params)


# ---------------------------------------------------------------------------
# the HTTP layer: 429s, bad bodies, unknown doors, crashes
# ---------------------------------------------------------------------------

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


def test_a_request_the_handler_logged_is_not_logged_again_at_the_http_layer(client, events):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 200
    assert [e.method for e in events] == ["ping"]


def test_a_rate_limited_request_is_logged_as_an_http_error_and_throttled(client, events):
    import main
    # drain the bucket for this caller, then keep hammering
    hdr = {"x-forwarded-for": "198.51.100.77"}
    statuses = [client.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": "ping"}, headers=hdr).status_code
                for i in range(int(main._RL_BUCKET_SIZE) + 40)]
    assert 429 in statuses
    http_rows = [e for e in events if e.outcome == "http_error"]
    assert len(http_rows) == 1, f"40 rejections must produce ONE row, got {len(http_rows)}"
    e = http_rows[0]
    assert e.http_status == 429 and e.error_code == "rate_limited" and e.method == "http"
    assert e.detail.startswith("POST /mcp")


def test_the_suppressed_count_is_reported_on_the_next_row():
    t = ul.BurstThrottle(window_s=30.0)
    assert t.admit("k", now=0.0) == (True, 0)
    for _ in range(5):
        assert t.admit("k", now=1.0)[0] is False
    assert t.admit("k", now=31.0) == (True, 5)


def test_unknown_door_404_is_logged(client, events):
    r = client.post("/mcp/not-a-door", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 404
    e = events[-1]
    assert (e.outcome, e.http_status, e.error_code) == ("http_error", 404, "not_found")
    assert "/mcp/not-a-door" in e.detail


def test_a_bad_json_body_that_crashes_the_route_is_logged_as_500(client, events):
    r = client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 500
    e = events[-1]
    assert (e.outcome, e.http_status, e.error_code) == ("http_error", 500, "unhandled_exception")


def test_non_mcp_paths_are_not_logged_by_this_layer(client, events):
    client.get("/health")
    assert events == []


def test_the_http_layer_event_carries_the_valid_key_id(client, events):
    key = issue_token(TokenRequest(agent_id="free_http_keyed", principal_id="p")).token
    client.post("/mcp/not-a-door", json={}, headers={"x-agent-identity": key})
    e = events[-1]
    assert e.key_id == "free_http_keyed" and e.key_state == "valid"


# ---------------------------------------------------------------------------
# the URL path is attacker-controlled text too (gate finding, P3)
# ---------------------------------------------------------------------------

def test_safe_path_keeps_ordinary_routes_and_masks_anything_secret_shaped():
    assert ro.safe_path("/mcp") == "/mcp"
    assert ro.safe_path("/mcp/not-a-door") == "/mcp/not-a-door"
    assert ro.safe_path("/mcp/sanctions-screening/") == "/mcp/sanctions-screening/"
    # a key pasted into the path (the claims part of a token is ~380-416 characters, so the old
    # 200-character cut-off kept the first half of it)
    keyish = "eyJhZ2VudF9pZCI6ImZyZWVfeCJ9" + "a" * 300
    assert ro.safe_path(f"/mcp/{keyish}") == "/mcp/<other>"
    assert ro.safe_path("/mcp/" + "0123456789abcdef" * 4) == "/mcp/<other>"       # a bare hex signature
    assert ro.safe_path("/mcp/has spaces and $symbols") == "/mcp/<other>"
    assert ro.safe_path("/a/b/c/d/e/f/g") == "/a/b/c/d/..."                        # bounded depth
    assert ro.safe_path("") == "/" and ro.safe_path(None) == "/"
    assert len(ro.safe_path("/" + "x" * 500)) <= 120


def test_a_key_pasted_into_the_url_path_is_not_stored_in_the_usage_row(client, events):
    key = issue_token(TokenRequest(agent_id="free_path_leak", principal_id="p")).token
    r = client.post(f"/mcp/{key}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 404
    e = events[-1]
    assert e.outcome == "http_error" and e.http_status == 404
    assert key[:20] not in e.detail and "eyJ" not in e.detail, e.detail
    assert e.detail == "POST /mcp/<other>"
