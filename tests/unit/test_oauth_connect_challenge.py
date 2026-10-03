"""A refused call becomes the signal an assistant's Connect button listens for - and nothing else changes.

THE CONTRACT, from the two clients' own documentation:
  Claude   starts sign-in ONLY on an HTTP 401 carrying `WWW-Authenticate: Bearer ... resource_metadata`; a 200
           with isError "is an application-level tool failure ... there is no auth prompt".
  ChatGPT  starts sign-in from a normal tool result whose `_meta["mcp/www_authenticate"]` carries a challenge
           with both `error` and `error_description`, and does not re-trigger from a 401.

THE PROMISE TO EVERYONE ELSE, pinned here as hard as the contract: keyless tools are never challenged; a call
the dispatcher allowed is never touched (valid key, valid access token, a payment); the body a 401 carries is
the body the caller always got; a bad key sent the old way keeps the old answer; and when the sign-in cannot
complete, or is switched off, or the style is `off`, the response is byte-identical to before.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import config
import main
from agent_interface import identity as ident
from agent_interface.oauth import limits, settings
from agent_interface.oauth.store import MemoryStore, set_store
from core import tool_auth

BASE = "https://api.hatchloop.dev"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)         # production's setting: write tools refuse anonymous callers
    monkeypatch.delenv("OAUTH_CHALLENGE_STYLE", raising=False)
    monkeypatch.delenv("OAUTH_CONNECT_ENABLED", raising=False)
    set_store(MemoryStore())
    limits.LIMITS.reset()
    main._rl_buckets.clear()
    yield
    set_store(None)
    main._rl_buckets.clear()


@pytest.fixture
def client():
    # This file pins the CLAUDE contract (a 401 on a refused call), so its caller is a connector that is known to
    # start sign-in from one. Every other caller is in test_oauth_connect_gate_fixes.py.
    return TestClient(main.app, base_url=BASE, raise_server_exceptions=False, headers={"user-agent": "Claude-User"})


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def call(tool, arguments=None, rid=1):
    return rpc("tools/call", {"name": tool, "arguments": arguments or {}}, rid)


def key_for(email="holder@example.org", **claims):
    cid = "free_" + ident.hashlib.sha256(email.encode()).hexdigest()[:16] if hasattr(ident, "hashlib") else "free_x"
    return ident.issue_token(ident.TokenRequest(agent_id=cid, principal_id=cid, principal_type="human",
                                                budget_cap_usd=0.0, extra_claims=claims)).token


def expired_token():
    return ident.issue_token(ident.TokenRequest(agent_id="free_old", principal_id="free_old", ttl_seconds=-30,
                                                extra_claims={"aud": "https://api.hatchloop.dev/mcp"})).token


def refusal_text(resp_json):
    return resp_json["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# every key-requiring tool, called with no credential at all
# ---------------------------------------------------------------------------

def _legacy_code(client, monkeypatch, tool, args):
    """What this tool answered an anonymous caller before the sign-in existed (style off = byte-identical)."""
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", "off")
    r = client.post("/mcp", json=call(tool, args))
    monkeypatch.delenv("OAUTH_CHALLENGE_STYLE")
    assert r.status_code == 200
    body = json.loads(refusal_text(r.json()))
    return body.get("error_code") or body.get("reason_code")


ARGS = {"reference": "1234", "business_number": "+15550001111"}


@pytest.mark.parametrize("tool", sorted(tool_auth.TOOLS_REQUIRING_KEY))
def test_an_anonymous_caller_is_challenged_exactly_when_signing_in_would_help(client, monkeypatch, tool):
    """auth_required / identity_required -> 401 with the old body. Anything else the tool said (call_business:
    channel_unavailable, because voice is not provisioned) is not an account problem and is left exactly alone -
    sending someone through a sign-in that cannot make the tool work would be the dishonest answer."""
    legacy = _legacy_code(client, monkeypatch, tool, ARGS)
    r = client.post("/mcp", json=call(tool, ARGS))
    if legacy not in ("auth_required", "identity_required"):
        assert r.status_code == 200 and "_meta" not in r.json()["result"], (tool, legacy)
        return
    assert r.status_code == 401, (tool, r.status_code)
    wa = r.headers["www-authenticate"]
    assert wa.startswith("Bearer ") and 'error="invalid_token"' in wa and 'error_description="Authentication required for this tool"' in wa
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in wa
    assert f'scope="{settings.SCOPE_TOOLS}"' in wa
    assert "no-store" in r.headers["cache-control"]
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1 and body["result"]["isError"] is True       # the answer they always got
    assert legacy in refusal_text(body)
    assert body["result"]["_meta"]["mcp/www_authenticate"][0].startswith("Bearer resource_metadata=")


def test_most_of_the_key_requiring_tools_really_are_challenged(client, monkeypatch):
    challenged = [t for t in sorted(tool_auth.TOOLS_REQUIRING_KEY)
                  if client.post("/mcp", json=call(t, ARGS)).status_code == 401]
    assert len(challenged) >= 7 and "get_conversation" in challenged and "send_message" in challenged


def test_keyless_tools_are_never_challenged(client):
    keyless = [t for t in ("check_quota", "preview_cost", "get_status", "check_booking_link", "map_trade_restriction")
               if not tool_auth.requires_key(t)]
    assert len(keyless) >= 4
    args = {"check_quota": {}, "preview_cost": {"operation": "send_message", "params": {}}, "get_status": {"operation_id": "nope"},
            "check_booking_link": {"url": "https://cal.com/x"}, "map_trade_restriction": {"destination_country": "OM", "product": "bolts"}}
    for tool in keyless:
        r = client.post("/mcp", json=call(tool, args[tool]))
        assert r.status_code == 200 and "www-authenticate" not in r.headers, tool
        assert "_meta" not in (r.json().get("result") or {}), tool


def test_handshake_listing_and_notifications_are_never_challenged(client):
    for body, status in ((rpc("initialize", {"protocolVersion": "2025-06-18"}), 200), (rpc("tools/list"), 200),
                         (rpc("ping"), 200), ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 202)):
        r = client.post("/mcp", json=body)
        assert r.status_code == status and "www-authenticate" not in r.headers


@pytest.mark.parametrize("path", ["/mcp", "/mcp/appointment-booking", "/mcp/sms-whatsapp-messaging"])
def test_the_doors_that_carry_a_key_requiring_tool_challenge_too_and_point_at_their_own_document(client, path):
    tool = "schedule_appointment" if path.endswith("appointment-booking") else "send_message"
    r = client.post(path, json=call(tool, {}))
    assert r.status_code == 401
    suffix = path[1:]
    assert f"/.well-known/oauth-protected-resource/{suffix}" in r.headers["www-authenticate"]


def test_a_door_the_site_host_fronts_points_at_the_public_name_the_person_typed(client):
    r = client.post("/mcp", json=call("get_conversation", {}), headers={"Host": "hatchloop.dev"})
    assert r.status_code == 401
    assert (f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp/agent-broker?host=hatchloop.dev"'
            in r.headers["www-authenticate"])
    doc = client.get("/.well-known/oauth-protected-resource/mcp/agent-broker?host=hatchloop.dev").json()
    assert doc["resource"] == "https://hatchloop.dev/mcp/agent-broker"        # ... and that document says the same


# ---------------------------------------------------------------------------
# calls the dispatcher allowed are never touched
# ---------------------------------------------------------------------------

def test_a_valid_key_in_either_header_is_not_challenged(client):
    k = key_for()
    for headers in ({"X-Agent-Identity": k}, {"Authorization": f"Bearer {k}"}, {"X-Api-Key": k}):
        r = client.post("/mcp", json=call("get_conversation", {"reference": "1234", "business_number": "+15550001111"}), headers=headers)
        assert r.status_code == 200 and "www-authenticate" not in r.headers, headers
        assert "identity_required" not in refusal_text(r.json())


def test_an_access_token_issued_by_the_sign_in_is_accepted_at_the_door_it_was_issued_for(client):
    from agent_interface.oauth import tokens
    subject = tokens.Subject(agent_id="free_abcdef0123456789", principal_id="free_abcdef0123456789", paid=False, plan="free")
    tok = tokens.mint_access_token(subject, resource="https://api.hatchloop.dev/mcp", scope="agentbroker.tools",
                                   client_id="dcr_x", family_id="fam").token
    r = client.post("/mcp", json=call("get_conversation", {"reference": "1234", "business_number": "+15550001111"}),
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and "identity_required" not in refusal_text(r.json())


def test_an_expired_access_token_gets_a_401_that_says_so_so_the_client_refreshes(client):
    r = client.post("/mcp", json=call("send_message", {}), headers={"Authorization": f"Bearer {expired_token()}"})
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"] and "expired" in r.headers["www-authenticate"]


def test_a_garbage_bearer_token_is_a_401_not_a_200(client):
    r = client.post("/mcp", json=call("send_message", {}), headers={"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401 and "not valid" in r.headers["www-authenticate"]


@pytest.mark.parametrize("header,value", [("X-Agent-Identity", "env:MY_KEY"), ("X-Agent-Identity", "garbage.value"),
                                           ("X-Api-Key", "garbage.value")])
def test_a_bad_key_sent_the_old_way_keeps_the_old_answer_and_its_diagnosis(client, header, value):
    r = client.post("/mcp", json=call("send_message", {}), headers={header: value})
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    body = refusal_text(r.json())
    assert "auth_required" in body and ("YOUR KEY WAS NOT ACCEPTED" in body or header == "X-Api-Key")


def test_a_valid_key_that_lacks_the_scope_for_a_tool_keeps_the_old_answer(client):
    narrow = ident.issue_token(ident.TokenRequest(agent_id="sub_narrow", principal_id="narrow", allowed_operations=["find_business"])).token
    r = client.post("/mcp", json=call("send_message", {}), headers={"Authorization": f"Bearer {narrow}"})
    assert r.status_code == 200 and "auth_required" in refusal_text(r.json())          # a 401 would start a pointless sign-in


# ---------------------------------------------------------------------------
# ChatGPT's form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{"User-Agent": "openai-mcp/1.0.0"}, {"User-Agent": "Mozilla/5.0 ChatGPT-User/1.0"},
                                      {"X-Openai-Host-Hash": "abc", "User-Agent": "x"}])
def test_chatgpt_gets_a_normal_result_carrying_the_challenge_in_meta(client, headers):
    r = client.post("/mcp", json=call("send_message", {}), headers=headers)
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    meta = r.json()["result"]["_meta"]["mcp/www_authenticate"]
    assert len(meta) == 1
    value = meta[0]
    assert value.startswith("Bearer ") and f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in value
    assert 'error="' in value and 'error_description="' in value          # OpenAI: both are required to trigger the UI
    assert r.json()["result"]["isError"] is True and "auth_required" in refusal_text(r.json())


# ---------------------------------------------------------------------------
# batches, switches, readiness
# ---------------------------------------------------------------------------

def test_a_batch_with_one_refused_member_is_a_401_carrying_the_whole_batch(client):
    r = client.post("/mcp", json=[rpc("tools/list", rid=1), call("send_message", rid=2), rpc("ping", rid=3)])
    assert r.status_code == 401
    body = r.json()
    assert [m["id"] for m in body] == [1, 2, 3] and "_meta" in body[1]["result"] and "_meta" not in body[0]["result"]


@pytest.mark.parametrize("style,expect_status", [("http401", 401), ("tool_result", 200), ("off", 200)])
def test_the_style_can_be_forced(client, monkeypatch, style, expect_status):
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", style)
    r = client.post("/mcp", json=call("send_message", {}))
    assert r.status_code == expect_status
    assert ("_meta" in r.json()["result"]) is (style != "off")


def test_with_the_style_off_the_response_is_byte_identical_to_the_previous_behaviour(client, monkeypatch):
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", "off")
    off = client.post("/mcp", json=call("send_message", {}))
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "0")
    disabled = client.post("/mcp", json=call("send_message", {}))
    assert off.status_code == disabled.status_code == 200
    assert off.json()["result"] == disabled.json()["result"] and "_meta" not in off.json()["result"]


def test_when_the_sign_in_cannot_complete_nothing_is_challenged_and_nothing_is_advertised(client):
    class NotReady(MemoryStore):
        async def ready(self):
            return False
    set_store(NotReady())
    r = client.post("/mcp", json=call("send_message", {}))
    assert r.status_code == 200 and "_meta" not in r.json()["result"]
    tools = client.post("/mcp", json=rpc("tools/list")).json()["result"]["tools"]
    assert all("securitySchemes" not in t for t in tools)


def test_when_the_database_is_slow_the_answer_is_still_the_old_one_not_a_hang(client):
    import asyncio

    class Slow(MemoryStore):
        async def ready(self):
            await asyncio.sleep(30)
            return True
    set_store(Slow())
    t0 = time.monotonic()
    r = client.post("/mcp", json=call("send_message", {}))
    assert r.status_code == 200 and time.monotonic() - t0 < 6


def test_a_failure_inside_the_challenge_never_turns_a_good_answer_into_a_crash(client, monkeypatch):
    from agent_interface.oauth import challenge as ch

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(ch, "_credential_verdict", boom)
    r = client.post("/mcp", json=call("send_message", {}))
    assert r.status_code == 200 and "auth_required" in refusal_text(r.json())


# ---------------------------------------------------------------------------
# tools/list: ChatGPT's securitySchemes, derived from the one place that knows
# ---------------------------------------------------------------------------

def test_every_tool_advertises_the_scheme_the_service_actually_enforces(client):
    tools = client.post("/mcp", json=rpc("tools/list")).json()["result"]["tools"]
    assert len(tools) == tool_auth.total_tools()
    seen = {"noauth": 0, "oauth2": 0, "mixed": 0}
    for t in tools:
        schemes = t["securitySchemes"]
        kinds = [s["type"] for s in schemes]
        cls = tool_auth.auth_class(t["name"])
        if cls == "needs_key":
            assert kinds == ["oauth2"], t["name"]
            seen["oauth2"] += 1
        elif cls == "quota_free":
            assert kinds == ["noauth", "oauth2"], t["name"]
            seen["mixed"] += 1
        else:
            assert kinds == ["noauth"], t["name"]
            seen["noauth"] += 1
        for s in schemes:
            if s["type"] == "oauth2":
                assert s["scopes"] == [settings.SCOPE_TOOLS]
    assert seen["oauth2"] == tool_auth.needs_key() and seen["noauth"] == tool_auth.keyless()
    assert seen["mixed"] == tool_auth.quota_free()


def test_the_cached_manifest_is_not_mutated_by_the_annotation(client):
    from agent_interface import mcp_server
    client.post("/mcp", json=rpc("tools/list"))
    assert all("securitySchemes" not in t for t in mcp_server._build_tool_list())


def test_a_door_lists_schemes_only_for_the_tools_it_serves(client):
    tools = client.post("/mcp/sanctions-screening", json=rpc("tools/list")).json()["result"]["tools"]
    assert tools and all(t["securitySchemes"] for t in tools)
    assert {t["name"] for t in tools} == {"screen_sanctions"} or all(not tool_auth.requires_key(t["name"]) for t in tools)
