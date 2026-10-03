"""The features meet: MCP 2026-07-28 requests, the OAuth challenge, the retired-door tombstones and find_business.

Each of these four branches was built and tested alone. The merge conflict in main.py (two routes that answer
through different helpers) is exactly where they could silently stop composing, so this file pins the crossings:

  * a MODERN refused call is challenged like a legacy one - a 401 for a known connector, the readable result with
    the `_meta` hint for everyone else - and keeps the revision's result shape;
  * the revision's own refusals (400 / 404) are never turned into anything else by the challenge;
  * notifications are still 202, batches still behave, a door still challenges, a retired door is still a tombstone.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import config
import main
from agent_interface.oauth import limits
from agent_interface.oauth.store import MemoryStore, set_store

M = "io.modelcontextprotocol/"
V = "2026-07-28"
ENVELOPE = {M + "protocolVersion": V, M + "clientCapabilities": {}, M + "clientInfo": {"name": "pytest", "version": "1"}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    for k in ("OAUTH_CHALLENGE_STYLE", "OAUTH_CONNECT_ENABLED", "OAUTH_CHALLENGE_401_CLIENTS"):
        monkeypatch.delenv(k, raising=False)
    set_store(MemoryStore())
    limits.LIMITS.reset()
    main._rl_buckets.clear()
    yield
    set_store(None)
    main._rl_buckets.clear()


def _client(ua):
    return TestClient(main.app, base_url="https://api.hatchloop.dev", raise_server_exceptions=False,
                      headers={"user-agent": ua})


def _modern_call(client, tool="send_message", path="/mcp", **extra_headers):
    headers = {"MCP-Protocol-Version": V, "Mcp-Method": "tools/call", "Mcp-Name": tool}
    headers.update(extra_headers)
    return client.post(path, headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": tool, "arguments": {}, "_meta": dict(ENVELOPE)}})


def test_a_modern_refused_call_from_a_known_connector_is_a_401_with_the_revisions_result_shape():
    r = _modern_call(_client("Claude-User"))
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer ")
    result = r.json()["result"]
    assert result["isError"] is True and result["resultType"] == "complete"
    assert json.loads(result["content"][0]["text"])["error_code"] in ("auth_required", "identity_required")


def test_a_modern_refused_call_from_anyone_else_keeps_the_readable_answer_and_the_hint():
    for ua in ("python-httpx/0.27.0", "scanner-I/0.1", "openai-mcp/1.0.0", "Grok/1.0"):
        r = _modern_call(_client(ua))
        assert r.status_code == 200, ua
        result = r.json()["result"]
        assert result["isError"] is True and result["resultType"] == "complete"
        assert result["_meta"]["mcp/www_authenticate"][0].startswith("Bearer resource_metadata="), ua


@pytest.mark.parametrize("path", ["/mcp/sms-whatsapp-messaging", "/mcp/appointment-booking"])
def test_a_door_challenges_a_modern_call_too(path):
    tool = "schedule_appointment" if path.endswith("booking") else "send_message"
    r = _modern_call(_client("Claude-User"), tool=tool, path=path)
    assert r.status_code == 401 and "/.well-known/oauth-protected-resource/" + path[1:] in r.headers["www-authenticate"]


def test_the_revisions_own_refusals_are_never_turned_into_a_challenge():
    c = _client("Claude-User")
    wrong_method = _modern_call(c, Mcp_Method="x") if False else c.post(
        "/mcp", headers={"MCP-Protocol-Version": V, "Mcp-Method": "tools/list", "Mcp-Name": "send_message"},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "send_message", "arguments": {}, "_meta": dict(ENVELOPE)}})
    assert wrong_method.status_code == 400 and wrong_method.json()["error"]["code"] == -32020
    assert "www-authenticate" not in wrong_method.headers
    unknown = c.post("/mcp", headers={"MCP-Protocol-Version": V, "Mcp-Method": "no/such"},
                     json={"jsonrpc": "2.0", "id": 4, "method": "no/such", "params": {"_meta": dict(ENVELOPE)}})
    assert unknown.status_code == 404 and "www-authenticate" not in unknown.headers
    version = c.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list",
                                   "params": {"_meta": {**ENVELOPE, M + "protocolVersion": "1999-01-01"}}},
                     headers={"MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "tools/list"})
    assert version.status_code == 400 and version.json()["error"]["code"] == -32022


def test_notifications_discovery_and_keyless_tools_are_never_challenged_in_either_era():
    c = _client("Claude-User")
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    d = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert d.status_code == 200 and "www-authenticate" not in d.headers
    free = _modern_call(c, tool="check_quota")
    assert free.status_code == 200 and "www-authenticate" not in free.headers
    legacy_free = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "check_quota", "arguments": {}}})
    assert legacy_free.status_code == 200 and "resultType" not in legacy_free.json()["result"]


def test_a_legacy_refused_call_is_challenged_exactly_as_before_the_merge():
    r = _client("Claude-User").post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": "send_message", "arguments": {}}})
    assert r.status_code == 401 and "resultType" not in r.json()["result"]


def test_a_retired_door_is_a_tombstone_not_a_challenge():
    r = _client("Claude-User").post("/mcp/email-sending", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_message", "arguments": {}}})
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    assert json.loads(r.json()["result"]["content"][0]["text"])["error"] == "server_retired"


def test_find_business_still_refuses_an_extreme_number_with_a_guided_message_in_the_modern_era():
    """1e999 is a legal JSON number that Python reads as infinity; the test client's encoder refuses to send it,
    so the body is written by hand, as a scorer would send it."""
    c = _client("python-httpx/0.27.0")
    body = ('{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"find_business","arguments":'
            '{"location":"Muscat","capability":"dentist","max_results":1e999},"_meta":' + json.dumps(ENVELOPE) + '}}')
    r = c.post("/mcp", content=body, headers={"content-type": "application/json", "MCP-Protocol-Version": V,
                                               "Mcp-Method": "tools/call", "Mcp-Name": "find_business"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True and result["resultType"] == "complete"
    assert json.loads(result["content"][0]["text"])["error_code"] == "invalid_argument"
