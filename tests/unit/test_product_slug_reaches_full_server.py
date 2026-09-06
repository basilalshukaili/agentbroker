"""The product's own slug must reach the full server on the ORIGIN too.

WHY. The founder, 2026-09-06: "i seen you making mistake of making the hatchloop
mcp endpoint specifically points to agentbroker". The origin knew `/mcp` and five
door names, so `/mcp/agent-broker` was a 404 there while the canonical host
answered 200 for the same server - one product reachable under two different
shapes depending on which host you asked. Every listing we publish names the
slug, so the slug has to work everywhere the server is.

THE TRAP THIS GUARDS. The lazy fix is to add "agent-broker" to PROFILES with a
list of all 23 tool names. That creates a sixth door whose tool list somebody has
to keep in step with the product for ever, which is exactly the drift
registry/servers.yaml exists to end. It is an ALIAS to no filtering instead, and
these tests pin that distinction.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent_interface import profiles  # noqa: E402
from agent_interface.mcp_server import handle_mcp_request  # noqa: E402


def call(method, profile=None, params=None):
    return asyncio.run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={}, profile=profile))


def test_the_alias_means_no_filtering_not_a_sixth_door():
    assert profiles.tools_for("agent-broker") is None
    assert "agent-broker" not in profiles.PROFILES


def test_the_alias_exists_and_is_accepted():
    assert profiles.exists("agent-broker")


def test_the_alias_serves_exactly_what_the_bare_endpoint_serves():
    bare = call("tools/list")["result"]["tools"]
    via_slug = call("tools/list", "agent-broker")["result"]["tools"]
    assert [t["name"] for t in bare] == [t["name"] for t in via_slug]


def test_the_alias_introduces_itself_as_the_product():
    info = call("initialize", "agent-broker",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "t", "version": "1"}})
    assert info["result"]["serverInfo"]["name"] == "agent-broker"


def test_a_door_still_narrows():
    """The alias must not have switched filtering off for everyone."""
    door = call("tools/list", "sanctions-screening")["result"]["tools"]
    assert 0 < len(door) < len(call("tools/list")["result"]["tools"])
    assert profiles.allows("sanctions-screening", "screen_sanctions")
    assert not profiles.allows("sanctions-screening", "send_message")


def test_an_unknown_name_is_still_refused_and_names_the_alias():
    with pytest.raises(profiles.ProfileError) as e:
        profiles.tools_for("not-a-server")
    assert "agent-broker" in str(e.value)


def test_the_alias_set_is_deliberately_tiny():
    """One entry. A growing alias list is a second registry by another name."""
    assert profiles.FULL_SERVER_ALIASES == frozenset({"agent-broker"})
    assert not (profiles.FULL_SERVER_ALIASES & set(profiles.PROFILES))
