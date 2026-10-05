"""A client must not be invited to call an unsupported logging method.

Inspector 2.5.0 sets a log level before tools/list whenever initialize advertises
logging. The legacy handshake advertised it, but logging/setLevel returned
-32601, so discovery stopped before the first tool could even be listed.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface import mcp_server, profiles
from billing import usage_logger

DOORS = [None, *sorted(profiles.PROFILES)]


@pytest.fixture(autouse=True)
def no_usage_network(monkeypatch):
    monkeypatch.setattr(usage_logger, "fire_log_outcome", lambda _event: None)


def rpc(method, params=None, door=None):
    return asyncio.run(mcp_server.handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={}, profile=door,
    ))


@pytest.mark.parametrize("version", mcp_server.SUPPORTED_PROTOCOL_VERSIONS)
@pytest.mark.parametrize("door", DOORS)
def test_legacy_handshake_does_not_advertise_unsupported_logging(door, version):
    response = rpc("initialize", {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "inspector-regression", "version": "2.5.0"},
    }, door)
    result = response["result"]
    assert result["protocolVersion"] == version
    assert "logging" not in result["capabilities"]
    assert "tools" in result["capabilities"]


@pytest.mark.parametrize("door", DOORS)
def test_unsupported_set_level_still_returns_method_not_found(door):
    response = rpc("logging/setLevel", {"level": "info"}, door)
    assert response["error"]["code"] == -32601


@pytest.mark.parametrize("door", DOORS)
def test_capability_driven_discovery_reaches_tools_list(door):
    handshake = rpc("initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "inspector-regression", "version": "2.5.0"},
    }, door)["result"]
    # Replay the conditional request which aborted Inspector discovery in production.
    if "logging" in handshake["capabilities"]:
        logging_response = rpc("logging/setLevel", {"level": "info"}, door)
        assert "error" not in logging_response
    listed = rpc("tools/list", door=door)
    assert listed["result"]["tools"]
    if door is not None:
        assert {tool["name"] for tool in listed["result"]["tools"]} == set(profiles.tools_for(door))
