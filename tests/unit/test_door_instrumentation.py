"""Every door leaves a row that says WHICH door, under WHICH protocol version, and how many results came back.

THE GAP (verdict item A7, docs/reviews/2026-10-03-mcp-focus-verdict.md): "without it we cannot see the first
buyer". The door was free text in `detail` for five of the doors and absent for the full server; the protocol
version was free text for 2026-07-28 requests only; the number of results was nowhere. And the six retired
doors - which take about 440 requests a day - answered from a function that wrote NOTHING, so the busiest part
of our traffic was the one part we could not see.

These tests drive the real dispatcher, the real HTTP layer and the real retired-door route (transport mocked, no
network, no database) and pin, for every door:
  * the row's `door`, `protocol_version` and `result_count` are the right values;
  * a request is recorded once, whichever layer sees it;
  * nothing a stranger types (a path, a header, a tool result) reaches a row except as one of OUR labels;
  * the retired doors are recorded as retired, filed as crawler traffic when keyless, and never as a live door;
  * the logger sends the new fields to the new database function and degrades, loudly, if the database lags.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent_interface import door_label, profiles, retired_doors
from agent_interface.identity import TokenRequest, issue_token
from agent_interface.mcp_server import ALL_PROTOCOL_VERSIONS, PROTOCOL_VERSION, handle_mcp_request
from billing import usage_logger as ul
from core import tool_auth

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "spine" / "013_usage_events_door_columns.sql"
M = "io.modelcontextprotocol/"
V = "2026-07-28"
SIX = sorted(retired_doors.RETIRED_DOORS)
LIVE_DOORS = sorted(profiles.PROFILES)


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


def _dispatch(method, params=None, *, profile=None, headers=None, rid=1):
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    return _run(handle_mcp_request(payload, headers=headers or {"user-agent": "pytest-client/1"}, profile=profile))


def _modern_meta(version=V):
    return {M + "protocolVersion": version, M + "clientCapabilities": {},
            M + "clientInfo": {"name": "pytest-modern", "version": "9.9"}}


def _post(client, path, method, params=None, *, headers=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers or {})


# ---------------------------------------------------------------------------
# door: the dispatcher
# ---------------------------------------------------------------------------

def test_the_full_server_records_its_door(events):
    _dispatch("ping")
    assert events[-1].door == "agent-broker"


def test_the_full_servers_own_name_is_the_same_door_as_the_bare_path(events):
    _dispatch("ping", profile="agent-broker")
    assert events[-1].door == "agent-broker"


@pytest.mark.parametrize("door", LIVE_DOORS)
def test_every_capability_door_records_its_own_name(events, door):
    _dispatch("tools/list", profile=door)
    assert events[-1].door == door


def test_the_detail_text_older_readers_use_is_unchanged(events):
    _dispatch("tools/list", profile="sanctions-screening")
    assert events[-1].detail == "door=sanctions-screening"
    _dispatch("tools/list")
    assert events[-1].detail is None


def test_every_member_of_a_batch_carries_the_door(events):
    payload = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
               {"jsonrpc": "2.0", "method": "notifications/initialized"},
               {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
    _run(handle_mcp_request(payload, headers={"user-agent": "x"}, profile="appointment-booking"))
    assert len(events) == 3 and {e.door for e in events} == {"appointment-booking"}


def test_an_oversize_batch_that_is_refused_whole_still_says_which_door(events):
    _run(handle_mcp_request([{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(500)],
                            headers={"user-agent": "x"}, profile="sanctions-screening"))
    assert [(e.outcome, e.door) for e in events] == [("rpc_error", "sanctions-screening")]


# ---------------------------------------------------------------------------
# protocol version
# ---------------------------------------------------------------------------

def test_a_legacy_request_records_the_version_its_header_states(events):
    _dispatch("tools/list", headers={"user-agent": "x", "mcp-protocol-version": "2025-06-18"})
    assert events[-1].protocol_version == "2025-06-18"


def test_a_legacy_request_that_states_nothing_records_nothing(events):
    """A stateless server cannot know what an earlier handshake agreed. NULL says 'not stated', not 'old'."""
    _dispatch("tools/list", headers={"user-agent": "x"})
    assert events[-1].protocol_version is None


def test_a_version_we_do_not_speak_is_never_stored(events):
    for header in ("1999-01-01", "2099-12-31", "'; drop table usage_events;--", "x" * 300, ""):
        _dispatch("tools/list", headers={"user-agent": "x", "mcp-protocol-version": header})
        assert events[-1].protocol_version is None, header


def test_initialize_records_the_version_that_was_negotiated(events):
    _dispatch("initialize", {"protocolVersion": "2025-03-26"})
    assert events[-1].protocol_version == "2025-03-26"
    _dispatch("initialize", {"protocolVersion": "2030-01-01"})
    assert events[-1].protocol_version == PROTOCOL_VERSION, "an unsupported ask is answered with our newest"
    _dispatch("initialize", {"protocolVersion": "evil\"</script>"})
    assert events[-1].protocol_version == PROTOCOL_VERSION
    _dispatch("initialize", {})
    assert events[-1].protocol_version == PROTOCOL_VERSION


def test_a_modern_request_records_the_revision_it_declared(events):
    _dispatch("tools/list", {"_meta": _modern_meta()},
              headers={"user-agent": "x", "mcp-protocol-version": V, "mcp-method": "tools/list"})
    assert events[-1].protocol_version == V
    assert events[-1].detail == "pv=2026-07-28", "the detail text is unchanged too"
    assert events[-1].result_count == tool_auth.total_tools(), "a decorated 2026-07-28 result counts the same list"


def test_a_legacy_version_declared_in_the_envelope_is_recorded(events):
    _dispatch("tools/list", {"_meta": {M + "protocolVersion": "2025-11-25"}}, headers={"user-agent": "x"})
    assert events[-1].protocol_version == "2025-11-25"


def test_a_refused_unsupported_version_is_labelled_by_error_code_not_stored(events):
    _dispatch("tools/list", {"_meta": _modern_meta("2027-01-01")},
              headers={"user-agent": "x", "mcp-protocol-version": "2027-01-01"})
    e = events[-1]
    assert e.error_code == "unsupported_protocol_version" and e.protocol_version is None


def test_a_notification_records_the_version_its_header_states(events):
    _run(handle_mcp_request({"jsonrpc": "2.0", "method": "notifications/initialized"},
                            headers={"user-agent": "x", "mcp-protocol-version": "2025-06-18"}))
    e = events[-1]
    assert e.outcome == "notification" and e.protocol_version == "2025-06-18"


def test_every_recorded_version_is_one_of_ours(events):
    for v in ALL_PROTOCOL_VERSIONS:
        _dispatch("tools/list", headers={"user-agent": "x", "mcp-protocol-version": v})
        assert events[-1].protocol_version == v


# ---------------------------------------------------------------------------
# result count
# ---------------------------------------------------------------------------

def test_tools_list_counts_the_tools_the_door_offered(events):
    _dispatch("tools/list")
    assert events[-1].result_count == tool_auth.total_tools()
    for door in LIVE_DOORS:
        _dispatch("tools/list", profile=door)
        assert events[-1].result_count == len(profiles.tools_for(door)), door


def test_other_list_methods_count_what_they_listed(events):
    _dispatch("resources/list")
    assert events[-1].result_count == 5
    _dispatch("resources/templates/list")
    assert events[-1].result_count == 0
    _dispatch("prompts/list")
    assert isinstance(events[-1].result_count, int)


def test_methods_that_return_no_list_record_no_count(events):
    _dispatch("initialize", {"protocolVersion": "2025-06-18"})
    assert events[-1].result_count is None
    _dispatch("ping")
    assert events[-1].result_count is None


def test_a_failed_request_records_no_count(events):
    _dispatch("no/such/method")
    assert events[-1].result_count is None
    _dispatch("tools/call", {"name": "preview_cost", "arguments": {}})     # missing required argument
    assert events[-1].result_count is None


def test_a_tool_that_hands_back_a_list_records_how_many(events, monkeypatch):
    import agent_interface.mcp_server as m

    async def _fake_tools_call(params, headers=None):
        return {"content": [{"type": "text", "text": json.dumps(
            {"status": "success", "result": {"businesses": [{}] * 9, "result_count": 3}})}], "isError": False}
    monkeypatch.setitem(m._METHOD_HANDLERS, "tools/call", _fake_tools_call)
    _dispatch("tools/call", {"name": "find_business", "arguments": {"vertical": "x"}})
    assert events[-1].result_count == 3
    assert events[-1].tool_name == "find_business"


# ---------------------------------------------------------------------------
# the HTTP layer
# ---------------------------------------------------------------------------

def test_a_request_the_dispatcher_logged_has_its_door_and_is_logged_once(client, events):
    r = _post(client, "/mcp", "tools/list")
    assert r.status_code == 200
    assert [(e.method, e.door) for e in events] == [("tools/list", "agent-broker")]


def test_the_products_own_name_routes_to_the_same_door(client, events):
    _post(client, "/mcp/agent-broker", "ping")
    assert events[-1].door == "agent-broker"


@pytest.mark.parametrize("door", LIVE_DOORS)
def test_a_capability_door_over_http(client, events, door):
    r = _post(client, f"/mcp/{door}", "tools/list")
    assert r.status_code == 200
    assert [(e.method, e.door, e.result_count) for e in events] == [
        ("tools/list", door, len(profiles.tools_for(door)))]


def test_an_unknown_door_404_is_recorded_as_unknown_not_as_what_the_caller_typed(client, events):
    r = _post(client, "/mcp/EVIL-door-name", "ping")
    assert r.status_code == 404
    e = events[-1]
    assert (e.method, e.outcome, e.http_status, e.door) == ("http", "http_error", 404, "unknown")
    assert "EVIL" not in (e.door or "")


def test_a_key_pasted_into_the_path_never_becomes_a_door(client, events):
    key = issue_token(TokenRequest(agent_id="free_door_leak", principal_id="p")).token
    client.post(f"/mcp/{key}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    e = events[-1]
    assert e.door == "unknown" and key[:12] not in repr(e)


def test_a_rate_limited_request_still_says_which_door(client, events):
    import main
    hdr = {"x-forwarded-for": "198.51.100.88"}
    for i in range(int(main._RL_BUCKET_SIZE) + 5):
        client.post("/mcp/sanctions-screening", json={"jsonrpc": "2.0", "id": i, "method": "ping"}, headers=hdr)
    limited = [e for e in events if e.error_code == "rate_limited"]
    assert len(limited) == 1 and limited[0].door == "sanctions-screening"


def test_a_rest_route_that_is_not_an_mcp_door_has_no_door(client, events):
    """The HTTP layer also records failures on /ops/* (the REST twins). They are not an MCP door."""
    r = client.post("/ops/no_such_tool", json={})
    assert r.status_code == 404
    assert events and all(e.door is None for e in events)
    assert events[-1].outcome == "http_error"


def test_a_crash_inside_the_route_is_still_attributed_to_its_door(client, events):
    r = client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 500
    assert events[-1].door == "agent-broker"


# ---------------------------------------------------------------------------
# the retired doors: written down for the first time
# ---------------------------------------------------------------------------

def _retired(client, slug, method, params=None, *, path=None, headers=None, rid=1):
    return _post(client, path or f"/mcp/{slug}", method, params, headers=headers, rid=rid)


@pytest.mark.parametrize("slug", SIX)
def test_a_retired_door_post_leaves_one_row_labelled_retired(client, events, slug):
    r = _retired(client, slug, "initialize", {"protocolVersion": "2025-06-18",
                                              "clientInfo": {"name": "scorer", "version": "3"}},
                 headers={"user-agent": "some-scorer/1"})
    assert r.status_code == 200
    assert len(events) == 1, [(e.method, e.door) for e in events]
    e = events[0]
    assert e.door == f"retired:{slug}"
    assert (e.method, e.outcome, e.http_status) == ("initialize", "ok", 200)
    assert e.protocol_version == "2025-06-18"
    assert (e.client_name, e.client_version) == ("scorer", "3")
    assert e.user_agent == "some-scorer/1"


def test_a_retired_door_tools_list_counts_its_one_tool(client, events):
    _retired(client, "data-enrichment", "tools/list")
    e = events[-1]
    assert (e.method, e.door, e.result_count) == ("tools/list", "retired:data-enrichment", 1)


def test_calling_the_tombstone_tool_is_ok_and_calling_anything_else_is_a_failure(client, events, monkeypatch):
    _retired(client, "pdf-generator", "tools/call", {"name": "server_retired", "arguments": {}})
    ok = events[-1]
    assert (ok.outcome, ok.error_code, ok.door) == ("ok", None, "retired:pdf-generator")
    assert ok.tool_name is None, "the tombstone tool is not one of OUR tools; it is named in requested_name"
    assert ok.requested_name == "server_retired"
    _retired(client, "pdf-generator", "tools/call", {"name": "generate_pdf", "arguments": {"html": "<b>secret</b>"}})
    bad = events[-1]
    assert bad.tool_name is None
    assert (bad.outcome, bad.error_code, bad.requested_name) == ("tool_failure", "server_retired", "generate_pdf")
    assert bad.arg_names == ["html"]
    sent = []

    async def _rpc(fn, payload):
        sent.append(payload)
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    _run(ul.log_usage_outcome(bad))
    assert "secret" not in json.dumps(sent), "names only, never values: the row sent to the database has none"


def test_a_live_tool_name_asked_of_a_retired_door_is_a_request_not_a_tool_run(client, events):
    """`find_business` is one of OUR tools. Asked of a retired door it did not run, so the row must not be filed
    under it: that would put a tombstone's failures into find_business's own numbers."""
    _retired(client, "data-enrichment", "tools/call", {"name": "find_business", "arguments": {"vertical": "x"}})
    e = events[-1]
    assert e.tool_name is None and e.requested_name == "find_business"
    assert (e.outcome, e.error_code, e.door, e.result_count) == ("tool_failure", "server_retired", "retired:data-enrichment", None)


def test_a_retired_door_rpc_error_is_recorded_as_one(client, events):
    _retired(client, "driftwatch", "no/such/method")
    e = events[-1]
    assert (e.outcome, e.error_code, e.door) == ("rpc_error", "method_not_found", "retired:driftwatch")
    assert e.result_count is None


def test_a_retired_door_notification_is_recorded_as_one(client, events):
    r = client.post("/mcp/ai-visibility", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"mcp-protocol-version": "2025-06-18"})
    assert r.status_code == 202
    e = events[-1]
    assert (e.method, e.outcome, e.http_status, e.door) == (
        "notifications/initialized", "notification", 202, "retired:ai-visibility")
    assert e.protocol_version == "2025-06-18"


def test_a_retired_door_batch_is_recorded_member_by_member(client, events):
    client.post("/mcp/email-sending", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    ])
    assert [(e.method, e.outcome) for e in events] == [
        ("ping", "ok"), ("notifications/initialized", "notification"), ("tools/list", "ok")]
    assert {e.door for e in events} == {"retired:email-sending"}


def test_an_empty_or_oversize_retired_batch_is_one_error_row(client, events):
    client.post("/mcp/url-shortener", json=[])
    assert [(e.outcome, e.error_code, e.door) for e in events] == [("rpc_error", "invalid_request", "retired:url-shortener")]
    events.clear()
    client.post("/mcp/url-shortener", json=[{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(400)])
    assert [(e.outcome, e.error_code) for e in events] == [("rpc_error", "invalid_request")]


def test_unparseable_bytes_sent_to_a_retired_door_are_recorded_once(client, events):
    r = client.post("/mcp/data-enrichment", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert len(events) == 1
    e = events[0]
    assert (e.outcome, e.http_status, e.door, e.method) == ("http_error", 400, "retired:data-enrichment", "http")


def test_the_old_child_spelling_is_recorded_under_the_same_door(client, events):
    _retired(client, "driftwatch", "tools/list", path="/mcp/driftwatch/mcp")
    assert events[-1].door == "retired:driftwatch"


def test_a_modern_request_to_a_retired_door_records_its_revision_and_identity(client, events):
    r = client.post("/mcp/data-enrichment", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _modern_meta()}},
        headers={"mcp-protocol-version": V, "mcp-method": "tools/list"})
    assert r.status_code == 200
    e = events[-1]
    assert (e.door, e.protocol_version, e.client_name, e.result_count) == ("retired:data-enrichment", V, "pytest-modern", 1)


def test_a_refusal_from_a_retired_door_is_recorded_with_its_real_status_and_only_once(client, events):
    r = client.post("/mcp/data-enrichment", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _modern_meta("2027-01-01")}},
        headers={"mcp-protocol-version": "2027-01-01"})
    assert r.status_code == 400
    assert len(events) == 1, "the HTTP layer must not log a second row for a request the route already logged"
    e = events[0]
    assert (e.outcome, e.http_status, e.error_code, e.door) == (
        "rpc_error", 400, "unsupported_protocol_version", "retired:data-enrichment")
    assert e.protocol_version is None


@pytest.mark.parametrize("path", [f"/mcp/{d}" for d in SIX] + [f"/mcp/{d}/mcp" for d in SIX])
def test_a_person_or_crawler_who_gets_410_is_recorded_against_the_retired_door(client, events, path):
    assert client.get(path).status_code == 410
    e = events[-1]
    slug = path.split("/")[2]
    assert (e.method, e.outcome, e.http_status, e.door) == ("http", "http_error", 410, f"retired:{slug}")


def test_keyless_retired_traffic_is_filed_as_crawler_never_as_an_agent_doing_work():
    """A retired door does no work. A keyless tools/call to one used to classify as anon_agent, which would put
    scorer traffic into the one bucket every 'does anybody use us' figure reads."""
    kw = dict(tool_name=None, user_agent="some-scorer/1", key_id=None)
    assert ul.classify_session_kind("tools/call", **kw) == "anon_agent"
    assert ul.classify_session_kind("tools/call", door="retired:pdf-generator", **kw) == "crawler"
    assert ul.classify_session_kind("tools/call", door="agent-broker", **kw) == "anon_agent"
    assert ul.classify_session_kind("tools/call", door=None, **kw) == "anon_agent"


def test_a_key_holder_at_a_retired_door_is_still_a_key_holder():
    """That is the one signal a dead door can give us: somebody who PAID is knocking on it."""
    got = ul.classify_session_kind("tools/call", None, "x", "free_someone", door="retired:pdf-generator")
    assert got == "verified_human_key"


def test_retired_rows_reach_the_log_with_session_kind_crawler(client, events, monkeypatch):
    sent = []

    async def _rpc(fn, payload):
        sent.append((fn, payload))
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    _retired(client, "pdf-generator", "tools/call", {"name": "server_retired"})
    assert len(events) == 1
    _run(ul.log_usage_outcome(events[0]))
    assert sent[-1][1]["p_session_kind"] == "crawler" and sent[-1][1]["p_door"] == "retired:pdf-generator"


def test_a_logging_failure_cannot_break_the_tombstone(client, monkeypatch):
    def _boom(e):
        raise RuntimeError("database on fire")
    monkeypatch.setattr(ul, "fire_log_outcome", _boom)
    r = _retired(client, "data-enrichment", "tools/list")
    assert r.status_code == 200 and r.json()["result"]["tools"][0]["name"] == "server_retired"


# ---------------------------------------------------------------------------
# the logger: new fields to the new function, and a graceful ladder if the database lags
# ---------------------------------------------------------------------------

def _capture(monkeypatch, missing=()):
    """Stand in for the database. `missing` is a live set of function names that answer PGRST202."""
    class _Calls(list):
        gone: set = set()

    calls = _Calls()
    gone = set(missing)

    async def _rpc(fn, payload):
        calls.append((fn, dict(payload)))
        if fn in gone:
            raise RuntimeError(f"rpc({fn!r}) failed: HTTP 404 body={{\"code\":\"PGRST202\"}}")
        return {"id": 1}
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = 0.0
    ul._v3_missing_until = 0.0
    calls.gone = gone
    return calls


def _event(**kw):
    base = dict(method="tools/list", ip="198.51.100.1", user_agent="UA", outcome="ok", http_status=200,
                door="sanctions-screening", protocol_version="2025-06-18", result_count=8)
    base.update(kw)
    return ul.UsageEvent(**base)


def test_the_new_fields_go_to_the_new_function(monkeypatch):
    calls = _capture(monkeypatch)
    _run(ul.log_usage_outcome(_event()))
    fn, p = calls[0]
    assert fn == "usage_events_insert_v3"
    assert (p["p_door"], p["p_protocol_version"], p["p_result_count"]) == ("sanctions-screening", "2025-06-18", 8)


def test_a_value_that_is_not_ours_is_dropped_before_it_can_cost_the_whole_row(monkeypatch):
    """The database function refuses a malformed door or version (and so loses the row). The logger must never
    send one: it sends NULL for the field and keeps everything else."""
    calls = _capture(monkeypatch)
    _run(ul.log_usage_outcome(_event(door="EVIL <script>", protocol_version="not-a-date", result_count=-5)))
    p = calls[0][1]
    assert (p["p_door"], p["p_protocol_version"], p["p_result_count"]) == (None, None, None)
    assert p["p_method"] == "tools/list" and p["p_outcome"] == "ok"
    calls.clear()
    _run(ul.log_usage_outcome(_event(door="x" * 80, protocol_version="2025-06-18\n", result_count=True)))
    p = calls[0][1]
    assert (p["p_door"], p["p_protocol_version"], p["p_result_count"]) == (None, None, None)
    calls.clear()
    _run(ul.log_usage_outcome(_event(result_count=10 ** 9)))
    assert calls[0][1]["p_result_count"] is None


def test_the_payload_matches_the_new_functions_signature():
    """Producer/consumer contract: every key the code sends must be a parameter of the function migration 013
    creates, and every parameter without a default must be sent."""
    sql = MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"create or replace function public\.usage_events_insert_v3\((.*?)\)\s*returns", sql, re.S)
    assert m, "013 must create usage_events_insert_v3"
    sql_params = set(re.findall(r"\b(p_[a-z_]+)\b", m.group(1)))

    sent = []

    async def _rpc(fn, payload):
        sent.append(payload)
        return {"id": 1}
    import storage.supabase_client as sb
    orig = sb.rpc
    sb.rpc = _rpc
    try:
        ul._v2_missing_until = ul._v3_missing_until = 0.0
        _run(ul.log_usage_outcome(ul.UsageEvent(method="tools/call", tool_name="x")))
    finally:
        sb.rpc = orig
    assert set(sent[0]) == sql_params, set(sent[0]) ^ sql_params


def test_if_the_new_function_is_missing_it_falls_back_to_v2_without_the_new_fields(monkeypatch, caplog):
    """Code deployed ahead of migration 013: the outcome columns keep being recorded; only door, version and count
    are lost - and the log says so, once, loudly."""
    calls = _capture(monkeypatch, missing=("usage_events_insert_v3",))
    with caplog.at_level("ERROR"):
        _run(ul.log_usage_outcome(_event()))
        _run(ul.log_usage_outcome(_event()))
    assert [c[0] for c in calls] == ["usage_events_insert_v3", "usage_events_insert_v2", "usage_events_insert_v2"]
    v2 = calls[1][1]
    assert "p_door" not in v2 and "p_protocol_version" not in v2 and "p_result_count" not in v2
    assert v2["p_outcome"] == "ok" and v2["p_http_status"] == 200
    assert "usage_log_v3_missing" in caplog.text and "migrations/spine/013" in caplog.text
    ul._v3_missing_until = 0.0


def test_if_both_new_functions_are_missing_it_still_falls_back_to_the_original(monkeypatch):
    calls = _capture(monkeypatch, missing=("usage_events_insert_v3", "usage_events_insert_v2"))
    _run(ul.log_usage_outcome(_event()))
    _run(ul.log_usage_outcome(_event()))
    assert [c[0] for c in calls] == ["usage_events_insert_v3", "usage_events_insert_v2",
                                     "usage_events_insert", "usage_events_insert"]
    assert set(calls[2][1]) == {"p_tool", "p_args_hash", "p_ip_hash", "p_user_agent", "p_key_id",
                                "p_session_kind", "p_method"}
    ul._v2_missing_until = ul._v3_missing_until = 0.0


def test_the_ladder_retries_the_new_function_after_the_pause(monkeypatch):
    calls = _capture(monkeypatch, missing=("usage_events_insert_v3",))
    _run(ul.log_usage_outcome(_event()))
    assert [c[0] for c in calls] == ["usage_events_insert_v3", "usage_events_insert_v2"]
    assert ul._v3_missing_until > 0
    calls.gone.clear()                              # the migration has been applied...
    calls.clear()
    _run(ul.log_usage_outcome(_event()))
    assert [c[0] for c in calls] == ["usage_events_insert_v2"], "...but the pause has not elapsed yet"
    ul._v3_missing_until = 0.0                      # ...and now it has
    calls.clear()
    _run(ul.log_usage_outcome(_event()))
    assert [c[0] for c in calls] == ["usage_events_insert_v3"]
    assert calls[0][1]["p_door"] == "sanctions-screening"


def test_a_real_failure_is_not_mistaken_for_a_missing_function(monkeypatch):
    calls = []

    async def _rpc(fn, payload):
        calls.append(fn)
        raise RuntimeError("rpc failed: HTTP 503")
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc", _rpc)
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    before = ul.get_usage_logger_health()["failed"]
    _run(ul.log_usage_outcome(_event()))
    assert calls == ["usage_events_insert_v3"], "a 503 must not trigger any fallback"
    assert ul.get_usage_logger_health()["failed"] == before + 1


def test_the_old_seven_field_logger_is_untouched():
    """log_usage_event (the original) still sends exactly its seven fields to usage_events_insert."""
    import inspect
    src = inspect.getsource(ul.log_usage_event)
    assert '"usage_events_insert"' in src and "p_door" not in src


# ---------------------------------------------------------------------------
# the observer hook on the pure tombstone module
# ---------------------------------------------------------------------------

def test_a_failing_observer_cannot_change_the_tombstone_answer():
    def boom(message, reply):
        raise RuntimeError("the recorder is on fire")
    one = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert retired_doors.handle("data-enrichment", one, {}, observe=boom) == retired_doors.handle("data-enrichment", one, {})
    batch = [one, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    assert retired_doors.handle("data-enrichment", batch, {}, observe=boom) == retired_doors.handle("data-enrichment", batch, {})
    assert retired_doors.handle("data-enrichment", [], {}, observe=boom)["error"]["code"] == -32600


def test_the_observer_is_told_about_every_message_once_and_nothing_else_changes():
    seen = []
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
    out = retired_doors.handle("pdf-generator", batch, {}, observe=lambda m, r: seen.append((m.get("method"), r is None)))
    assert seen == [("ping", False), ("notifications/initialized", True), ("tools/list", False)]
    assert out == retired_doors.handle("pdf-generator", batch, {}), "the observer changes nothing about the answer"
    seen.clear()
    retired_doors.handle("pdf-generator", [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(40)], {},
                         observe=lambda m, r: seen.append(m))
    assert len(seen) == 1 and isinstance(seen[0], list), "a batch refused whole is one message to the observer"
