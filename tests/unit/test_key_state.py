"""A key that was presented and did not work must be SAID, not silently treated as anonymous.

THE FIELD CASE (audit 2026-09-30): on 2026-09-30 04:55Z an external Node client sent `initialize` and
`tools/list` to hatchloop.dev/mcp/agent-broker with an X-Agent-Identity value that was a 33-character
`env:`-style placeholder - a config template its MCP client never expanded. Every table recorded it as
a keyless caller; it got free tools and, on its first write tool, an `auth_required` that read as if
it had sent no key. Nothing told it the key was the problem.

These tests pin the behaviour that closes that: the key's state is classified (valid / invalid /
expired / placeholder / none), the caller is told on initialize, tools/list and every tool result,
the reason is a code (never the presented value), and a caller with a good key or no key at all is
left completely alone.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_interface import key_state as ks
from agent_interface.identity import issue_token, TokenRequest
from agent_interface.mcp_server import handle_mcp_request


def _run(coro):
    return asyncio.run(coro)


def _good_key(agent_id="free_keystate_ok"):
    return issue_token(TokenRequest(agent_id=agent_id, principal_id=agent_id)).token


def _expired_key():
    return issue_token(TokenRequest(agent_id="free_keystate_old", principal_id="p", ttl_seconds=-60)).token


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "anonymous"])
def test_nothing_presented_is_none_and_never_a_problem(raw):
    s = ks.classify_key(raw)
    assert s.state == ks.KEY_NONE
    assert not s.is_problem and not s.present
    assert ks.auth_warning(s) is None


def test_a_good_key_is_valid_and_carries_its_identity():
    s = ks.classify_key(_good_key("free_keystate_ok"))
    assert s.state == ks.KEY_VALID
    assert s.agent_id == "free_keystate_ok"
    assert ks.auth_warning(s) is None, "a working key must never be scolded"


@pytest.mark.parametrize("raw", [
    "env:AGENTBROKER_API_KEY",                 # the 2026-09-30 field case shape
    "env:AGENTBROKER_API_KEY_FOR_HATCHLOOP",   # the 33-character shape
    "${AGENTBROKER_KEY}",
    "$AGENTBROKER_KEY",
    "{{ secrets.AGENTBROKER_KEY }}",
    "<your-api-key>",
    "YOUR_API_KEY",
    "your-key-here",
    "REPLACE_ME",
    "process.env.AGENTBROKER_KEY",
    "undefined",
    "null",
    "None",
    "Bearer",
])
def test_unexpanded_templates_and_empty_literals_are_placeholders(raw):
    s = ks.classify_key(raw)
    assert s.state == ks.KEY_PLACEHOLDER, raw
    assert s.is_problem
    assert "placeholder" in s.hint.lower() or "template" in s.hint.lower()


def test_an_expired_key_is_expired_not_invalid():
    s = ks.classify_key(_expired_key())
    assert s.state == ks.KEY_EXPIRED
    assert s.reason == "expired"
    assert "new one" in s.hint.lower() or "request" in s.hint.lower()


def test_a_tampered_signature_is_invalid_bad_signature():
    token = _good_key()
    payload, _sig = token.split(".")
    s = ks.classify_key(payload + "." + "0" * 64)
    assert s.state == ks.KEY_INVALID
    assert s.reason == "bad_signature"


def test_a_truncated_or_unshaped_key_is_invalid_malformed():
    for raw in ("abc", "justonepart", "a.b.c", "x" * 40):
        s = ks.classify_key(raw)
        assert s.state in (ks.KEY_INVALID, ks.KEY_PLACEHOLDER), raw
    assert ks.classify_key("totally-not-a-key").state == ks.KEY_INVALID
    assert ks.classify_key("totally-not-a-key").reason == "malformed"


def test_bearer_prefix_inside_the_header_is_called_out():
    s = ks.classify_key("Bearer " + _good_key())
    assert s.state == ks.KEY_INVALID
    assert s.reason == "bearer_prefix_in_header"


def test_a_revoked_key_is_invalid_revoked(monkeypatch):
    from agent_interface import identity
    token = _good_key("free_keystate_revoked")
    claims = identity._verify(token)
    monkeypatch.setattr(identity, "is_jti_revoked", lambda jti: jti == claims["jti"])
    s = ks.classify_key(token)
    assert s.state == ks.KEY_INVALID and s.reason == "revoked"


def test_the_presented_value_is_never_echoed():
    secret_looking = "env:SUPERSECRET_VALUE_abc123"
    s = ks.classify_key(secret_looking)
    w = ks.auth_warning(s)
    blob = json.dumps(w) + s.hint + s.reason
    assert "SUPERSECRET_VALUE_abc123" not in blob
    assert "abc123" not in blob


def test_classification_never_raises():
    for weird in (object(), 12345, b"bytes", ["a"], {"a": 1}):
        s = ks.classify_key(weird)       # type: ignore[arg-type]
        assert s.state in ks.KEY_STATES


def test_the_warning_says_what_still_works_and_how_to_fix():
    w = ks.auth_warning(ks.classify_key("env:X_KEY"))
    assert w["key_state"] == "placeholder"
    text = w["message"].lower()
    assert "anonymous" in text, "must say the call was NOT treated as authenticated"
    assert "need no key" in text, "must say that keyless tools still work"
    assert "auth_required" in text, "must say what will fail"
    assert w["how_to_fix"]["header"] == "X-Agent-Identity"
    assert w["how_to_fix"]["get_a_free_key"].endswith("/keys/request")


# ---------------------------------------------------------------------------
# the warning reaches the caller, on the three surfaces the audit named
# ---------------------------------------------------------------------------

HDR_BAD = {"x-agent-identity": "env:AGENTBROKER_API_KEY", "user-agent": "node"}


@pytest.fixture
def require_auth(monkeypatch):
    """Production behaviour: write tools refuse a caller without a valid key (REQUIRE_AUTH defaults
    on when ENVIRONMENT=production, off in the test environment)."""
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)


def _rpc(method, params=None, headers=None, rid=1):
    return _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
        headers=headers))


def test_initialize_tells_a_caller_with_a_bad_key():
    r = _rpc("initialize", {"protocolVersion": "2025-06-18"}, HDR_BAD)["result"]
    assert r["instructions"].startswith("AUTH WARNING:")
    assert r["_meta"]["hatchloop/auth_warning"]["key_state"] == "placeholder"


def test_tools_list_tells_a_caller_with_a_bad_key():
    r = _rpc("tools/list", {}, HDR_BAD)["result"]
    assert r["auth_warning"]["key_state"] == "placeholder"
    assert r["tools"], "the tool list itself must be untouched"


def test_a_tool_result_carries_the_warning_in_the_text_the_model_reads():
    r = _rpc("tools/call", {"name": "preview_cost",
                            "arguments": {"operation": "send_message", "params": {}}}, HDR_BAD)["result"]
    body = json.loads(r["content"][0]["text"])
    assert body["auth_warning"]["key_state"] == "placeholder"
    assert r["_meta"]["hatchloop/auth_warning"]["reason"] == "unexpanded_template"
    assert "estimated_cost_usd" in body, "the real answer must still be there beside the warning"


def test_a_failed_tool_call_with_a_bad_key_is_told_in_the_error_too(require_auth):
    """The audit's exact path: first WRITE tool, key is a placeholder."""
    r = _rpc("tools/call", {"name": "capture_lead", "arguments": {
        "smb_id": "demo_1", "prospect": {"name": "A"}}}, HDR_BAD)["result"]
    assert r["isError"] is True
    body = json.loads(r["content"][0]["text"])
    assert body["error_code"] == "auth_required"
    assert body["auth_warning"]["key_state"] == "placeholder"


def test_auth_required_names_the_key_as_the_problem(require_auth):
    """Not 'you sent no key': the message must say the key that WAS sent was not accepted."""
    r = _rpc("tools/call", {"name": "capture_lead", "arguments": {
        "smb_id": "demo_1", "prospect": {"name": "A"}}}, HDR_BAD)["result"]
    body = json.loads(r["content"][0]["text"])
    assert body["error_code"] == "auth_required"
    assert "YOUR KEY WAS NOT ACCEPTED" in body["human_message"]
    assert body["how_to_resolve"]["key_problem"]["key_state"] == "placeholder"


def test_a_caller_with_no_key_is_left_alone(require_auth):
    for method, params in (("initialize", {"protocolVersion": "2025-06-18"}), ("tools/list", {})):
        resp = _rpc(method, params, {"user-agent": "x"})
        assert "auth_warning" not in resp["result"]
        assert "_meta" not in resp["result"]
    r = _rpc("tools/call", {"name": "capture_lead", "arguments": {
        "smb_id": "demo_1", "prospect": {"name": "A"}}}, {"user-agent": "x"})["result"]
    body = json.loads(r["content"][0]["text"])
    assert "auth_warning" not in body
    assert "key_problem" not in body["how_to_resolve"]


def test_a_caller_with_a_good_key_is_left_alone():
    h = {"x-agent-identity": _good_key("free_keystate_fine"), "user-agent": "x"}
    for method, params in (("initialize", {"protocolVersion": "2025-06-18"}), ("tools/list", {})):
        resp = _rpc(method, params, h)
        assert "auth_warning" not in resp["result"]
    r = _rpc("tools/call", {"name": "check_quota", "arguments": {}}, h)["result"]
    body = json.loads(r["content"][0]["text"])
    assert "auth_warning" not in body
    assert body.get("key_id") == "free_keystate_fine" or "tier" in body


def test_an_expired_key_in_authorization_bearer_is_also_told():
    """Hosted connectors can only send Authorization: Bearer - the warning must follow them."""
    r = _rpc("tools/list", {}, {"authorization": "Bearer " + _expired_key()})["result"]
    assert r["auth_warning"]["key_state"] == "expired"


def test_decorating_a_result_never_mutates_the_object_it_was_given():
    """A tools/call result may be the very dict the idempotency gate stored for replay. Pinning one
    caller's warning onto it would show it to the next caller replaying the same key."""
    import copy
    from agent_interface.mcp_server import _attach_auth_warning
    stored = {"content": [{"type": "text", "text": json.dumps({"status": "success"})}], "isError": False}
    snapshot = copy.deepcopy(stored)
    response = {"jsonrpc": "2.0", "id": 1, "result": stored}
    _attach_auth_warning(response, "tools/call", ks.auth_warning(ks.classify_key("env:KEY")))
    assert stored == snapshot, "the stored/replayed object was mutated in place"
    assert "auth_warning" in json.loads(response["result"]["content"][0]["text"])


def test_other_methods_are_not_decorated():
    r = _rpc("ping", {}, HDR_BAD)["result"]
    assert "auth_warning" not in r and "_meta" not in r


def test_the_warning_is_attached_to_protocol_errors_too():
    resp = _rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, HDR_BAD)
    assert resp["error"]["data"]["auth_warning"]["key_state"] == "placeholder"
