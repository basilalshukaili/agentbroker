"""The credential must arrive under the header names Claude can actually send.

Anthropic's connector documentation: "Standard header names such as
`authorization` and `x-api-key` work for every connector; Anthropic reviews and
approves any other header name before administrators can save the connector."
(https://claude.com/docs/connectors/building/authentication)

We read exactly one header, X-Agent-Identity. On Claude's hosted surfaces - the
largest place anyone would install us - an administrator cannot enter it without
Anthropic's review. The free tools worked, which is precisely why nobody noticed:
every PAID tool was unreachable from that surface and it looked like nobody
wanted them.

A fallback to `authorization` already existed and fed only the usage logger, so
we recorded the identity of callers the gate then refused. These tests pin the
normalisation to the front door, where every reader below inherits it.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent_interface import mcp_server  # noqa: E402

TOKEN = "hl_test_token_value"


def _seen(headers: dict) -> str | None:
    """Run one tools/call through the dispatcher and report the credential the
    handler was given, without executing a real tool."""
    captured: dict = {}

    async def fake_handler(params, hdrs):
        captured["hdrs"] = hdrs
        return {"content": [{"type": "text", "text": "ok"}]}

    original = mcp_server._METHOD_HANDLERS.get("tools/call")
    mcp_server._METHOD_HANDLERS["tools/call"] = fake_handler
    try:
        # asyncio.run, NOT get_event_loop().run_until_complete. These eight tests
        # passed alone and failed all eight in the full suite, because by then
        # another test had left the global loop closed. A test that only passes
        # when run alone is not evidence of anything.
        asyncio.run(mcp_server.handle_mcp_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "self_test", "arguments": {}}},
            headers=headers))
    finally:
        if original is not None:
            mcp_server._METHOD_HANDLERS["tools/call"] = original
    return (captured.get("hdrs") or {}).get("x-agent-identity")


@pytest.mark.parametrize("headers", [
    {"X-Agent-Identity": TOKEN},                       # what we published
    {"Authorization": f"Bearer {TOKEN}"},              # what Claude can send
    {"authorization": f"bearer {TOKEN}"},              # scheme is case-insensitive
    {"X-Api-Key": TOKEN},                              # the other allowed name
    {"x-api-key": TOKEN},
])
def test_the_credential_reaches_the_gate(headers):
    assert _seen(headers) == TOKEN


def test_our_own_header_wins_when_both_are_sent():
    """X-Agent-Identity is the published name; an Authorization header carrying
    something else (a proxy's own token, say) must never displace it."""
    assert _seen({"X-Agent-Identity": TOKEN,
                  "Authorization": "Bearer someone-elses-token"}) == TOKEN


def test_no_credential_stays_no_credential():
    assert not _seen({"user-agent": "probe"})


def test_a_non_bearer_authorization_is_not_treated_as_a_key():
    """Basic auth is not our token, and silently accepting it as one would turn
    a browser's stored password into an API key."""
    assert not _seen({"Authorization": "Basic dXNlcjpwYXNz"})
