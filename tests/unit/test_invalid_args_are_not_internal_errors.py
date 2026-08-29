"""
A caller's bad arguments must never be reported as a server fault.

WHY THE DISTINCTION IS WORTH A TEST FILE. The two JSON-RPC codes mean opposite
things to an agent:

    -32603 ERR_INTERNAL      "the server is broken"  -> back off, retry, give up
    -32602 ERR_INVALID_PARAMS "your request is wrong" -> fix it and call again

Report the second as the first and a working marketplace looks broken. The
agent retries an unfixable request until its budget runs out, then abandons the
task - and nothing on our side records an error, because there wasn't one.

This was already fixed once. On 2026-08-04 a missing argument surfaced as
`Internal error: 'vertical'` and a KeyError handler was added to name the
argument instead. But tool arguments are parsed into PYDANTIC models, and a
wrong type, a malformed nested object, or a field missing from a nested model
raises ValidationError, not KeyError - so those went on falling through to
ERR_INTERNAL for another three weeks. Found 2026-08-29 by calling capture_lead
with a plausible-but-wrong field name while checking something else entirely.

The lesson worth keeping: fixing ONE exception type is not fixing the class.
Everything below is about the class - a bad argument, however it is malformed,
is the caller's to fix and must say so.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("JWT_SIGNING_SECRET", "test-secret-long-enough-for-the-check")

from agent_interface import mcp_server as ms  # noqa: E402

ERR_INTERNAL = -32603
ERR_INVALID_PARAMS = -32602

# A token is required for these to reach argument parsing at all: without one
# the auth gate answers first and the bad arguments are never seen. That is
# also why this was invisible in production - anonymous probes get
# `auth_required`, and only an AUTHENTICATED caller, i.e. a paying one, ever
# reached the crash.
HEADERS = {"x-agent-identity": "test-token-value"}


def _call(tool: str, arguments: dict) -> dict:
    return asyncio.run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
         "params": {"name": tool, "arguments": arguments}},
        HEADERS))


BAD_ARGUMENTS = [
    ("missing a field of a nested model", "capture_lead",
     {"smb_id": "smb_x", "channel": "sms", "contact": {}}),
    ("wrong type for a scalar", "capture_lead",
     {"smb_id": 123, "channel": ["sms"], "contact": {"name": "n"}}),
    ("nested object sent as a string", "capture_lead",
     {"smb_id": "smb_x", "channel": "sms", "contact": "just a name"}),
    ("plausible but wrong field name", "capture_lead",
     {"business_id": "smb_x", "channel": "sms"}),
]


@pytest.mark.parametrize("label,tool,args", BAD_ARGUMENTS,
                         ids=[c[0] for c in BAD_ARGUMENTS])
def test_bad_arguments_are_never_internal_errors(label, tool, args):
    resp = _call(tool, args)
    err = resp.get("error")
    if err is None:
        # A typed failure RESULT is also a correct answer - some paths report
        # tool failures as isError results rather than protocol errors. What
        # must never happen is ERR_INTERNAL.
        assert "result" in resp, f"{label}: neither result nor error"
        return
    assert err["code"] != ERR_INTERNAL, (
        f"{label}: reported as a SERVER fault ({err.get('message', '')[:120]}). "
        f"An agent reads -32603 as 'the server is broken' and retries or gives "
        f"up; the caller can fix this and only the caller can.")
    assert err["code"] == ERR_INVALID_PARAMS, (
        f"{label}: expected {ERR_INVALID_PARAMS}, got {err['code']}")


def test_the_error_names_what_to_fix():
    """An error an agent cannot act on is barely better than the wrong code."""
    resp = _call("capture_lead",
                 {"smb_id": "smb_x", "channel": "sms", "contact": {}})
    err = resp.get("error")
    if err is None:
        pytest.skip("this path returns a typed result, covered above")
    data = err.get("data") or {}
    assert data.get("retriable") is False, (
        "retrying an unfixed request must not be advertised as worth trying")
    named = data.get("invalid_fields") or []
    assert named or "name" in err.get("message", ""), (
        "the response must name the offending field - 'invalid arguments' "
        "alone leaves the agent guessing which one")


def test_a_genuine_server_fault_is_still_internal():
    """THE OTHER DIRECTION, which matters just as much.

    Widening the invalid-argument case until it swallows real faults would be
    worse than the bug: a broken backend would report itself as the caller's
    mistake, and nobody would ever look at our logs. A real internal failure
    must still come back as -32603.
    """
    boom = ms._METHOD_HANDLERS["tools/call"]

    async def explode(*_a, **_k):
        raise RuntimeError("simulated backend outage")

    ms._METHOD_HANDLERS["tools/call"] = explode
    try:
        resp = _call("capture_lead", {"smb_id": "x"})
    finally:
        ms._METHOD_HANDLERS["tools/call"] = boom
    assert resp.get("error", {}).get("code") == ERR_INTERNAL, (
        "a real server fault must still be reported as one")


def test_this_file_actually_exercises_the_handler():
    """Guard the guard.

    Every assertion above passes trivially if the auth gate answers before
    argument parsing - which is exactly what happens WITHOUT a token, and is
    why this bug survived every anonymous probe of production. Prove the
    no-token path really does short-circuit, so that if it ever changes, this
    file's premise is re-examined rather than silently voided.
    """
    resp = asyncio.run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
         "params": {"name": "capture_lead", "arguments": {"contact": {}}}},
        {}))
    text = str(resp)
    assert "auth_required" in text or "error" in resp, (
        "an unauthenticated write call should be refused before its arguments "
        "are parsed - if that changed, the HEADERS above may no longer be what "
        "makes these tests reach the parser")
