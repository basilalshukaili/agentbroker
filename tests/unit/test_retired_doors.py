"""Retired MCP doors answer with a tombstone a client can read, and are never counted as traffic.

Six servers from the earlier multi-product era are gone and directories still list them. In the four days
to 2026-10-03 the three busiest took 1,849 POSTs - about 440 a day, mostly directory scorers - each answered
410 Gone with a body that is not a JSON-RPC message (docs/reviews/2026-10-03-mcp-demand-evidence.md item 7).
These tests pin what replaces that: a real MCP handshake that says RETIRED in its name and instructions,
exactly one tool that returns the live server's address, 410 for GET/HEAD, nothing billed, nothing counted.

The routes run against the real app through TestClient; nothing touches the network.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
from agent_interface import profiles, retired_doors as rd
from core import tool_auth
from telemetry import metrics

SIX = ["ai-visibility", "data-enrichment", "driftwatch", "email-sending", "pdf-generator", "url-shortener"]


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _fresh_rate_limit_bucket():
    """Every /mcp request from the TestClient is one caller; 60 in a burst would start answering 429 and
    make later tests fail for a reason that has nothing to do with them."""
    main._rl_buckets.clear()
    yield
    main._rl_buckets.clear()


def rpc(client, door, method, params=None, rid=1, path=None):
    return client.post(path or f"/mcp/{door}", json={"jsonrpc": "2.0", "id": rid, "method": method,
                                                     "params": params or {}})


def content_json(resp_json):
    return json.loads(resp_json["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# which doors, and that none of them is a live door
# ---------------------------------------------------------------------------

def test_the_six_retired_servers_are_the_ones_the_site_tombstones():
    assert sorted(rd.RETIRED_DOORS) == SIX


def test_a_retired_door_is_never_also_a_live_one():
    live = set(profiles.PROFILES) | {"agent-broker"}
    assert not live & set(rd.RETIRED_DOORS)


def test_paths_that_belong_to_retired_doors():
    for d in SIX:
        for p in (f"/mcp/{d}", f"/mcp/{d}/", f"/mcp/{d}/mcp", f"/mcp/{d}/mcp/"):
            assert rd.path_is_retired(p), p
    for p in ("/mcp", "/mcp/agent-broker", "/mcp/sanctions-screening", "/mcp/data-enrichment/other",
              "/ops/find_business", "/mcp/nonsense", "/mcp/nonsense/mcp"):
        assert not rd.path_is_retired(p), p


# ---------------------------------------------------------------------------
# an MCP conversation with a retired door
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("door", SIX)
def test_initialize_completes_and_says_retired(client, door):
    r = rpc(client, door, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "scorer", "version": "1"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["serverInfo"]["name"] == f"dev.hatchloop/{door} (RETIRED)" and res["serverInfo"]["version"] == "retired"
    assert res["protocolVersion"] == "2025-06-18"
    assert "retired" in res["instructions"] and rd.LIVE_URL in res["instructions"]
    assert r.json()["id"] == 1 and r.json()["jsonrpc"] == "2.0"


def test_an_unknown_protocol_version_gets_our_newest_not_an_echo(client):
    from agent_interface.mcp_server import PROTOCOL_VERSION
    r = rpc(client, "data-enrichment", "initialize", {"protocolVersion": "evil\"</script>"})
    assert r.json()["result"]["protocolVersion"] == PROTOCOL_VERSION


@pytest.mark.parametrize("door", SIX)
def test_tools_list_is_exactly_one_tombstone_tool(client, door):
    tools = rpc(client, door, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["server_retired"]
    t = tools[0]
    assert t["description"].startswith("RETIRED") and door in t["description"] and rd.LIVE_URL in t["description"]
    assert t["inputSchema"] == {"type": "object", "properties": {}}
    assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
    assert t["_meta"]["hatchloop/readiness"]["state"] == "unavailable"


def test_the_tombstone_tool_returns_the_live_servers_address_and_is_not_an_error(client):
    r = rpc(client, "pdf-generator", "tools/call", {"name": "server_retired", "arguments": {}}).json()
    assert r["result"]["isError"] is False
    b = content_json(r)
    assert b["error"] == "server_retired" and b["retired"] == "pdf-generator" and b["retriable"] is False
    assert b["live_server"]["url"] == rd.LIVE_URL and b["live_server"]["transport"] == "streamable-http"
    assert b["what_it_was"] == "a PDF generation server"


def test_any_other_tool_name_gets_the_same_facts_as_an_error_the_model_can_read(client):
    r = rpc(client, "url-shortener", "tools/call", {"name": "shorten_url", "arguments": {"url": "https://x.example"}}).json()
    assert "error" not in r, "a model sees isError results; many clients hide JSON-RPC errors from it"
    assert r["result"]["isError"] is True
    assert content_json(r)["live_server"]["url"] == rd.LIVE_URL


def test_the_message_counts_are_derived_never_typed(client):
    msg = content_json(rpc(client, "driftwatch", "tools/call", {"name": "server_retired"}).json())["human_message"]
    assert f"{tool_auth.usable_without_key()} of its {tool_auth.total_tools()} tools work with no key" in msg


def test_nothing_is_billed_or_logged_as_a_tool_run(client):
    """A retired tool never runs: the answer is built from constants. No metering import is even reachable."""
    src = open(rd.__file__, encoding="utf-8").read()
    for forbidden in ("billing", "credits", "x402", "supabase", "requests.", "httpx"):
        assert forbidden not in src, forbidden


def test_the_other_methods_a_scorer_tries(client):
    assert rpc(client, "email-sending", "ping").json()["result"] == {}
    assert rpc(client, "email-sending", "resources/list").json()["result"] == {"resources": []}
    assert rpc(client, "email-sending", "prompts/list").json()["result"] == {"prompts": []}
    assert rpc(client, "email-sending", "resources/templates/list").json()["result"] == {"resourceTemplates": []}
    nf = rpc(client, "email-sending", "no/such/method").json()
    assert nf["error"]["code"] == -32601 and nf["id"] == 1


def test_a_notification_gets_202_and_no_body(client):
    r = client.post("/mcp/ai-visibility", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""


def test_a_batch_is_answered_member_by_member(client):
    r = client.post("/mcp/data-enrichment", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    ])
    out = r.json()
    assert [m["id"] for m in out] == [1, 3]
    assert out[1]["result"]["tools"][0]["name"] == "server_retired"


def test_an_empty_batch_and_a_notification_only_batch(client):
    assert client.post("/mcp/data-enrichment", json=[]).json()["error"]["code"] == -32600
    r = client.post("/mcp/data-enrichment", json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert r.status_code == 202


def test_garbage_is_a_json_rpc_parse_error_not_a_500(client):
    r = client.post("/mcp/data-enrichment", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r2 = client.post("/mcp/data-enrichment", json="just a string")
    assert r2.json()["error"]["code"] == -32600


def test_the_old_child_path_spelling_is_tombstoned_too(client):
    r = rpc(client, "driftwatch", "tools/list", path="/mcp/driftwatch/mcp")
    assert r.status_code == 200 and r.json()["result"]["tools"][0]["name"] == "server_retired"


# ---------------------------------------------------------------------------
# people and crawlers: 410 Gone, successor named
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [f"/mcp/{d}" for d in SIX] + [f"/mcp/{d}/mcp" for d in SIX])
def test_get_and_head_are_410_gone_with_the_successor_named(client, path):
    g = client.get(path)
    assert g.status_code == 410
    assert g.headers["link"] == f'<{rd.LIVE_URL}>; rel="successor-version"'
    assert g.headers["cache-control"] == rd.CACHE_CONTROL
    assert g.json()["live_server"]["url"] == rd.LIVE_URL and g.json()["error"] == "server_retired"
    h = client.head(path)
    assert h.status_code == 410 and h.content == b""


# ---------------------------------------------------------------------------
# nothing else changed
# ---------------------------------------------------------------------------

def test_a_live_door_is_untouched(client):
    r = rpc(client, "sanctions-screening", "tools/list").json()
    names = {t["name"] for t in r["result"]["tools"]}
    assert "screen_sanctions" in names and "server_retired" not in names


def test_an_unknown_door_still_404s_with_the_list_of_real_ones(client):
    r = rpc(client, "no-such-door", "tools/list")
    assert r.status_code == 404 and "sanctions-screening" in json.dumps(r.json())


def test_get_on_a_live_door_is_still_405_post_only(client):
    r = client.get("/mcp/sanctions-screening")
    assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_an_unknown_child_path_is_404(client):
    assert client.get("/mcp/no-such-door/mcp").status_code == 404
    assert client.post("/mcp/sanctions-screening/mcp", json={}).status_code == 404


def test_retired_traffic_does_not_inflate_the_public_counters(client):
    metrics.reset_metrics()
    for d in SIX:
        rpc(client, d, "initialize", {"protocolVersion": "2025-06-18"})
        rpc(client, d, "tools/list")
        client.get(f"/mcp/{d}")
    assert metrics._metrics.total_agents_requested == 0
    assert metrics._metrics.total_operations_completed == 0
    rpc(client, "sanctions-screening", "tools/list")             # a live door still counts
    assert metrics._metrics.total_agents_requested == 1
    metrics.reset_metrics()
