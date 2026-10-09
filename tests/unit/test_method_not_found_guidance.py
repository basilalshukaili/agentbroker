"""A method this endpoint does not have is answered with a way forward, not a bare -32601.

THE EVIDENCE (usage_events, 2026-09-25 .. 2026-10-09, docs/reviews/2026-10-09-agentbroker-request-analysis.md):
the A2A method names `message/send`, `tasks/send` and `SendMessage` reached this MCP endpoint 411 times from
outside callers, and `tasks/list` another 46. Our own agent card names this URL as the card's endpoint, so a caller
that reads the card and speaks A2A lands here, gets `Method 'message/send' not found`, and has nothing to act on.
The card also claimed `streaming`, `pushNotifications` and `stateTransitionHistory` - three A2A capabilities that
do not exist here.

These tests pin: the refusal says this endpoint is MCP, names the methods it does answer and the next call to
make; an A2A name is called what it is; the caller's method text is never echoed raw; the 2026-07-28 shape keeps
its HTTP 404; the ChatGPT door keeps its fixed wording (it must not gain a new surface); and neither A2A document we
serve (/.well-known/agent-card.json and /.well-known/agents.json) claims A2A capabilities.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface import mcp_server, well_known
from agent_interface.mcp_server import handle_mcp_request
from billing import usage_logger as ul

HEADERS = {"user-agent": "pytest-client/1"}
A2A_ONLY = ("message/send", "message/stream", "tasks/send", "tasks/sendSubscribe", "SendMessage",
            "SendStreamingMessage", "tasks/resubscribe", "agent/getAuthenticatedExtendedCard")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def events(monkeypatch):
    got = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: got.append(e))
    return got


def _call(method, **kw):
    return _run(handle_mcp_request({"jsonrpc": "2.0", "id": 9, "method": method, "params": {}},
                                   headers=HEADERS, **kw))


@pytest.mark.parametrize("method", A2A_ONLY)
def test_an_a2a_method_is_named_as_such_and_pointed_at_mcp(method, events):
    r = _call(method)
    assert r["error"]["code"] == -32601
    msg = r["error"]["message"]
    assert "A2A" in msg and "MCP" in msg, msg
    data = r["error"]["data"]
    assert data["error_code"] == "method_not_found"
    assert data["protocol"] == "mcp"
    assert data["how_to_resolve"]["call"] == "tools/list"
    assert "tools/call" in data["supported_methods"]
    # telemetry is unchanged: the same outcome and code the analysis already counts
    assert (events[-1].outcome, events[-1].error_code) == ("rpc_error", "method_not_found")


def test_any_unknown_method_lists_what_the_endpoint_does_answer(events):
    r = _call("tools/banana")
    assert r["error"]["code"] == -32601
    assert r["error"]["message"].startswith("Method 'tools/banana' not found")
    assert "A2A" not in r["error"]["message"]
    data = r["error"]["data"]
    assert data["supported_methods"] == sorted(mcp_server._METHOD_HANDLERS), \
        "the list must be read from the dispatcher's own table so it cannot drift"
    assert data["protocol"] == "mcp"


@pytest.mark.parametrize("method", ["tasks/get", "tasks/list", "tasks/cancel"])
def test_task_methods_are_not_called_a2a_only_because_mcp_has_a_tasks_extension(method):
    msg = _call(method)["error"]["message"]
    assert "not found" in msg
    assert "A2A-only" not in msg and "is an A2A method" not in msg


def test_the_callers_method_text_is_not_echoed_raw():
    hostile = "<script>alert(1)</script>" + "A" * 300
    r = _call(hostile)
    assert r["error"]["code"] == -32601
    text = repr(r["error"])
    assert "<script" not in text
    assert len(r["error"]["message"]) < 200


def test_a_removed_method_is_not_offered_to_a_modern_request():
    """In the 2026-07-28 envelope `ping` is removed (and answered 404), so it must not be listed as supported."""
    from agent_interface import mcp_2026 as m26
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}
    r = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"_meta": meta}},
                                headers={**HEADERS, "mcp-protocol-version": "2026-07-28", "mcp-method": "ping"}))
    assert r["error"]["code"] == -32601
    assert getattr(r, "http_status", 200) == 404
    supported = r["error"]["data"]["supported_methods"]
    assert "ping" not in supported and "tools/list" in supported
    assert m26.REMOVED_METHODS == frozenset({"ping"}), "this test assumes ping is the only removed method"


def test_the_chatgpt_door_keeps_its_fixed_wording_and_gains_no_new_surface():
    r = _call("message/send", profile="chatgpt")
    assert r["error"]["code"] == -32601
    assert "data" not in r["error"], "the door's refusal is a fixed sentence; nothing is added to it"
    assert "A2A" not in r["error"]["message"]


def test_the_agent_card_does_not_claim_a2a_capabilities_this_server_lacks():
    card = well_known.get_agent_card()
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False,
                                    "stateTransitionHistory": False}
    assert card["_meta"]["a2a"]["implemented"] is False
    assert card["_meta"]["a2a"]["use"] == "mcp"
    assert card["_meta"]["a2a"]["mcpEndpoint"] == card["url"]


# ---------------------------------------------------------------------------------------------------------------------
# The SECOND A2A document (gate finding P2 on d4f34c1)
# ---------------------------------------------------------------------------------------------------------------------
#
# The fix above corrected /.well-known/agent-card.json only. /.well-known/agents.json (well_known.get_agents_json, served
# by main.py) was untouched and went on declaring protocol_version "a2a-v0.2", streaming / push_notifications /
# state_transition_history all True and "a2a" among its supported protocols, with the service root as its URL - so after
# the release the two documents contradicted each other and the one the registries crawl still invited A2A callers.

A2A_CAPABILITY_FLAGS = ("streaming", "push_notifications", "state_transition_history")


def test_agents_json_does_not_claim_a2a_capabilities_this_server_lacks():
    doc = well_known.get_agents_json()
    assert doc["capabilities"] == {flag: False for flag in A2A_CAPABILITY_FLAGS}
    assert doc["protocol_version"] is None, "no A2A protocol version is spoken here"
    assert "a2a" not in doc["supported_protocols"]
    assert doc["supported_protocols"][0] == "mcp"


def test_agents_json_points_at_the_mcp_endpoint_and_says_a2a_is_not_implemented():
    doc = well_known.get_agents_json()
    card = well_known.get_agent_card()
    assert doc["a2a"] == card["_meta"]["a2a"] == {"implemented": False, "use": "mcp", "mcpEndpoint": card["url"]}
    assert doc["url"] == card["url"] == doc["discovery_urls"]["mcp"], "one endpoint, named the same way in both documents"


def test_the_two_a2a_documents_cannot_disagree_about_a_capability():
    doc, card = well_known.get_agents_json(), well_known.get_agent_card()
    assert not any(doc["capabilities"].values()) and not any(card["capabilities"].values())
    assert len(doc["capabilities"]) == len(card["capabilities"]) == 3


def test_the_served_route_returns_the_corrected_document():
    """main.py serves it; the dispatcher test above is not the wire."""
    from fastapi.testclient import TestClient
    import main
    body = TestClient(main.app).get("/.well-known/agents.json").json()
    assert body["capabilities"] == {flag: False for flag in A2A_CAPABILITY_FLAGS}
    assert "a2a" not in body["supported_protocols"] and body["a2a"]["implemented"] is False


def test_the_committed_edge_snapshot_of_agents_json_agrees():
    """The edge worker serves /.well-known/agents.json from this snapshot (edge/src/discovery.ts)."""
    import json
    from pathlib import Path
    snap = json.loads((Path(__file__).resolve().parents[2] / "edge" / "src" / "snapshots" / "agents.json")
                      .read_text(encoding="utf-8"))
    assert snap["capabilities"] == {flag: False for flag in A2A_CAPABILITY_FLAGS}
    assert "a2a" not in snap["supported_protocols"] and snap["a2a"]["implemented"] is False


def test_the_discovery_notes_no_longer_list_the_a2a_claim_as_open():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "docs" / "DISCOVERY.md").read_text(encoding="utf-8")
    assert "The A2A card still declares" not in text
    assert "agents.json" in text and "a2a.implemented" in text
