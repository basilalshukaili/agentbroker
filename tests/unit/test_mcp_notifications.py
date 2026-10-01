"""JSON-RPC notifications and client responses are accepted, never answered, never counted as errors.

THE BUG (measured 2026-10-01 from the outcome log the day before): `notifications/initialized` - the
message every MCP client sends right after `initialize` - was answered `method_not_found`. 32 of the 34
rpc_errors from real callers in 25 minutes were that one message. A notification is a JSON-RPC message
WITHOUT an `id`; the receiver must not reply to it, and over Streamable HTTP a POST that carries only
notifications and/or responses is accepted with HTTP 202 and no body (MCP spec, transports).

These tests pin: no reply object for any id-less message (known or unknown, on every door, alone or
inside a batch); HTTP 202 with an empty body; a notification can never RUN anything (an id-less
`tools/call` is not executed); the outcome row says outcome=notification, not an error; and the
database function accepts that outcome (migration 010) so the row is not silently dropped.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent_interface import mcp_server
from agent_interface.mcp_server import handle_mcp_request
from billing import usage_logger as ul

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_010 = ROOT / "migrations" / "spine" / "010_usage_events_notification_outcome.sql"

HEADERS = {"user-agent": "pytest-client/1"}
KNOWN = ("notifications/initialized", "notifications/cancelled", "notifications/progress",
         "notifications/roots/list_changed")


def _run(coro):
    return asyncio.run(coro)


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


def _note(method, **extra):
    return {"jsonrpc": "2.0", "method": method, **extra}


# ---------------------------------------------------------------------------
# the dispatcher
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", KNOWN)
def test_a_known_notification_gets_no_reply_and_is_logged_as_a_notification(events, method):
    assert _run(handle_mcp_request(_note(method), headers=HEADERS)) is None
    assert len(events) == 1
    e = events[0]
    assert e.method == method
    assert e.outcome == "notification"
    assert e.error_code is None
    assert e.http_status == 202


def test_initialized_with_params_and_no_id_is_still_a_notification(events):
    r = _run(handle_mcp_request(_note("notifications/progress", params={"progressToken": "t", "progress": 1}),
                                headers=HEADERS))
    assert r is None
    assert events[-1].outcome == "notification"


def test_an_unknown_notification_is_ignored_silently_but_logged(events):
    r = _run(handle_mcp_request(_note("notifications/some_future_thing"), headers=HEADERS))
    assert r is None
    e = events[-1]
    assert e.outcome == "notification"
    assert e.error_code == "unknown_notification"
    assert e.requested_name == "notifications/some_future_thing"


def test_any_idless_message_is_a_notification_even_outside_the_notifications_namespace(events):
    r = _run(handle_mcp_request(_note("vendor/ext_ping"), headers=HEADERS))
    assert r is None
    assert (events[-1].outcome, events[-1].error_code) == ("notification", "unknown_notification")


def test_an_unknown_notification_name_that_looks_like_a_secret_is_not_stored(events):
    secret = "notifications/eyJ" + "A" * 80
    assert _run(handle_mcp_request(_note(secret), headers=HEADERS)) is None
    e = events[-1]
    assert secret not in repr(e)
    assert e.requested_name == "<invalid>"


@pytest.mark.parametrize("method", ["tools/call", "tools/list", "initialize", "ping"])
def test_a_request_method_sent_without_an_id_is_a_notification_and_is_never_executed(events, monkeypatch, method):
    """JSON-RPC: no id means no reply. It must not RUN either - a tools/call nobody can receive the
    result of is how a paid operation gets performed for free of consequences."""
    ran = []

    async def _spy(*a, **k):
        ran.append(method)
        return {}
    monkeypatch.setitem(mcp_server._METHOD_HANDLERS, method, _spy)
    r = _run(handle_mcp_request(
        _note(method, params={"name": "preview_cost", "arguments": {"operation": "send_message", "params": {}}}),
        headers=HEADERS))
    assert r is None
    assert ran == []
    e = events[-1]
    assert e.outcome == "notification" and e.error_code == "request_without_id"


def test_the_same_methods_WITH_an_id_are_still_answered(events):
    for method in ("ping", "tools/list"):
        r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 7, "method": method, "params": {}}, headers=HEADERS))
        assert r["id"] == 7 and "result" in r
        assert events[-1].outcome == "ok"


def test_an_unknown_method_WITH_an_id_is_still_method_not_found(events):
    r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 3, "method": "nonsense/method"}, headers=HEADERS))
    assert r["error"]["code"] == -32601 and r["id"] == 3
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "method_not_found")


def test_a_notification_method_WITH_an_id_is_a_request_and_is_refused(events):
    """An id makes it a request; there is no request called notifications/initialized."""
    r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 4, "method": "notifications/initialized"}, headers=HEADERS))
    assert r["error"]["code"] == -32601
    assert events[-1].outcome == "rpc_error"


def test_an_explicit_null_id_is_still_a_request(events):
    r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": None, "method": "ping"}, headers=HEADERS))
    assert r == {"jsonrpc": "2.0", "id": None, "result": {}}


def test_a_client_response_to_a_server_request_is_accepted_without_a_reply(events):
    """Streamable HTTP: a client may POST a JSON-RPC response (to a ping or sampling request). 202."""
    for msg in ({"jsonrpc": "2.0", "id": "srv-1", "result": {}},
                {"jsonrpc": "2.0", "id": "srv-2", "error": {"code": -32601, "message": "no"}}):
        assert _run(handle_mcp_request(msg, headers=HEADERS)) is None
        assert events[-1].outcome == "notification"
        assert events[-1].method == "response"
        assert events[-1].error_code is None


def test_a_message_with_no_method_and_no_id_is_still_an_invalid_request(events):
    r = _run(handle_mcp_request({"jsonrpc": "2.0"}, headers=HEADERS))
    assert r["error"]["code"] == -32600
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "invalid_request")


def test_the_key_state_of_a_notification_is_still_recorded(events):
    h = {**HEADERS, "x-agent-identity": "not-a-real-key-at-all"}
    assert _run(handle_mcp_request(_note("notifications/initialized"), headers=h)) is None
    assert events[-1].key_state in ("invalid", "placeholder")
    assert events[-1].outcome == "notification"


def test_the_door_is_recorded_on_a_notification(events):
    _run(handle_mcp_request(_note("notifications/initialized"), headers=HEADERS, profile="sanctions-screening"))
    assert events[-1].detail == "door=sanctions-screening"


def test_a_notification_after_initialize_is_attributed_to_the_client_that_initialized(events):
    _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18",
                                        "clientInfo": {"name": "TestClient", "version": "9"}}},
                            headers=HEADERS))
    _run(handle_mcp_request(_note("notifications/initialized"), headers=HEADERS))
    assert events[-1].client_name == "TestClient"


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def test_a_batch_of_only_notifications_gets_no_reply_and_each_is_logged(events):
    r = _run(handle_mcp_request([_note("notifications/initialized"), _note("notifications/cancelled")],
                                headers=HEADERS))
    assert r is None
    assert [(e.method, e.outcome) for e in events] == [("notifications/initialized", "notification"),
                                                       ("notifications/cancelled", "notification")]


def test_a_mixed_batch_answers_the_requests_only(events):
    r = _run(handle_mcp_request([_note("notifications/initialized"),
                                 {"jsonrpc": "2.0", "id": 11, "method": "ping"},
                                 _note("notifications/made_up"),
                                 {"jsonrpc": "2.0", "id": 12, "method": "tools/list"}], headers=HEADERS))
    assert isinstance(r, list) and [x["id"] for x in r] == [11, 12]
    assert all("error" not in x for x in r)
    assert [e.outcome for e in events] == ["notification", "ok", "notification", "ok"]


def test_a_batch_containing_an_unknown_method_with_an_id_reports_that_error_for_that_id_only(events):
    r = _run(handle_mcp_request([_note("notifications/initialized"),
                                 {"jsonrpc": "2.0", "id": 5, "method": "nope"},
                                 {"jsonrpc": "2.0", "id": 6, "method": "ping"}], headers=HEADERS))
    assert [x["id"] for x in r] == [5, 6]
    assert r[0]["error"]["code"] == -32601 and "result" in r[1]


def test_an_empty_batch_is_one_invalid_request(events):
    r = _run(handle_mcp_request([], headers=HEADERS))
    assert isinstance(r, dict) and r["error"]["code"] == -32600 and r["id"] is None
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "invalid_request")


def test_a_batch_that_is_too_large_is_refused_whole(events):
    big = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(mcp_server.MAX_BATCH + 1)]
    r = _run(handle_mcp_request(big, headers=HEADERS))
    assert isinstance(r, dict) and r["error"]["code"] == -32600
    assert len(events) == 1 and events[0].error_code == "invalid_request"


def test_a_batch_member_that_is_not_an_object_is_an_invalid_request_for_that_member(events):
    r = _run(handle_mcp_request([5, {"jsonrpc": "2.0", "id": 2, "method": "ping"}], headers=HEADERS))
    assert isinstance(r, list) and len(r) == 2
    assert r[0]["error"]["code"] == -32600 and r[0]["id"] is None
    assert r[1]["id"] == 2


def test_a_payload_that_is_neither_object_nor_array_is_an_invalid_request_not_a_crash(events):
    for bad in ("hello", 5, None):
        r = _run(handle_mcp_request(bad, headers=HEADERS))
        assert r["error"]["code"] == -32600, bad


# ---------------------------------------------------------------------------
# HTTP: 202 on every door
# ---------------------------------------------------------------------------

def test_http_notification_on_mcp_is_202_with_an_empty_body(client, events):
    r = client.post("/mcp", json=_note("notifications/initialized"))
    assert r.status_code == 202
    assert r.content == b""
    assert events[-1].outcome == "notification"


def test_http_unknown_door_is_still_404_for_a_notification(client, events):
    r = client.post("/mcp/not-a-door", json=_note("notifications/initialized"))
    assert r.status_code == 404


def test_http_notification_is_202_on_every_capability_door(client, events):
    from agent_interface import profiles
    assert profiles.PROFILES, "there must be doors to test"
    for door in sorted(profiles.PROFILES):
        r = client.post(f"/mcp/{door}", json=_note("notifications/initialized"))
        assert r.status_code == 202, door
        assert r.content == b"", door
        assert events[-1].outcome == "notification" and events[-1].detail == f"door={door}", door


def test_http_batch_of_notifications_is_202(client, events):
    r = client.post("/mcp", json=[_note("notifications/initialized"), _note("notifications/cancelled")])
    assert r.status_code == 202 and r.content == b""
    assert [e.outcome for e in events] == ["notification", "notification"]


def test_http_batch_with_requests_is_200_and_an_array(client, events):
    r = client.post("/mcp", json=[_note("notifications/initialized"),
                                  {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 200
    assert r.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_http_a_real_client_handshake_has_no_error_row(client, events):
    """initialize -> notifications/initialized -> tools/list, as every MCP client does it."""
    a = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "handshake-test", "version": "1"}}})
    b = client.post("/mcp", json=_note("notifications/initialized"))
    c = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert (a.status_code, b.status_code, c.status_code) == (200, 202, 200)
    assert "result" in a.json() and len(c.json()["result"]["tools"]) > 5
    assert [e.outcome for e in events] == ["ok", "notification", "ok"]
    assert not [e for e in events if e.outcome in ("rpc_error", "http_error", "exception")]


def test_http_a_malformed_body_is_still_a_failure(client, events):
    r = client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 500
    assert events[-1].outcome == "http_error"


# ---------------------------------------------------------------------------
# the outcome is storable
# ---------------------------------------------------------------------------

def test_notification_is_an_outcome_the_logger_will_send():
    assert "notification" in ul.OUTCOMES
    sent = []

    async def _rpc(fn, payload):
        sent.append(payload)
        return {"id": 1}
    import storage.supabase_client as sb
    orig = sb.rpc
    sb.rpc = _rpc
    try:
        ul._v2_missing_until = 0.0
        _run(ul.log_usage_outcome(ul.UsageEvent(method="notifications/initialized", outcome="notification",
                                                http_status=202)))
    finally:
        sb.rpc = orig
    assert sent[0]["p_outcome"] == "notification", "it must not be silently relabelled 'ok'"


def test_migration_010_makes_the_database_accept_every_outcome_the_code_can_send():
    """The database function validates the outcome. Without this migration every notification row is
    rejected by the function and lost - the exact silent-no-op shape the outcome log exists to end."""
    sql = MIGRATION_010.read_text(encoding="utf-8")
    m = re.search(r"p_outcome\s+not in\s*\((.*?)\)\s*then", sql, re.S)
    assert m, "010 must re-create the function with the outcome allow-list"
    allowed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert allowed == set(ul.OUTCOMES), (allowed ^ set(ul.OUTCOMES))
    # the signature must be the one 009 created (a different signature would create an overload)
    sig_009 = re.search(r"create or replace function public\.usage_events_insert_v2\((.*?)\)\s*returns",
                        (ROOT / "migrations" / "spine" / "009_keyholder_outcome_logging_and_compliance_rpcs.sql")
                        .read_text(encoding="utf-8"), re.S).group(1)
    sig_010 = re.search(r"create or replace function public\.usage_events_insert_v2\((.*?)\)\s*returns",
                        sql, re.S).group(1)
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    assert norm(sig_010) == norm(sig_009)
    # the partial "failures" index must not fill up with one handshake row per client
    assert "'notification'" in sql.split("idx_usage_events_failures", 1)[1]
