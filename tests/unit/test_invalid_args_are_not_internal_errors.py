"""
A caller's bad arguments must never be reported as a server fault.

WHY THE DISTINCTION IS WORTH A TEST FILE. The two JSON-RPC codes mean opposite
things to an agent:

    -32603 ERR_INTERNAL       "the server is broken"  -> back off, retry, give up
    -32602 ERR_INVALID_PARAMS "your request is wrong"  -> fix it and call again

Report the second as the first and a working marketplace looks broken. The
agent retries an unfixable request until its budget runs out, then abandons the
task - and nothing on our side records an error, because there wasn't one.

THIS FILE HAS NOW BEEN WRONG TWICE, WHICH IS THE POINT OF ITS CURRENT SHAPE.

  * 2026-08-04: a MISSING argument surfaced as `Internal error: 'vertical'`. A
    KeyError handler was added. One exception type fixed.
  * 2026-08-29 (morning): pydantic ValidationError was found doing the same
    thing and a second handler was added - with a test file asserting four
    "malformation shapes".
  * 2026-08-29 (afternoon): an external reviewer showed those four shapes were
    ONE. They all sent `contact`/`channel` to capture_lead, which reads
    `prospect` - so every case died identically on `ProspectData(**{})` before
    reaching the thing it claimed to test, and produced byte-identical output.
    Three of the four were duplicates and the fourth exercised the OLD handler.
    The reviewer also found THREE more exception types still returning -32603,
    one of them reachable with no credentials at all.

So this file no longer trusts that differently-written cases are different
cases. `test_each_case_is_a_distinct_shape` asserts that the responses actually
differ from one another - a duplicate parameterisation now fails instead of
inflating the count.

The fix under test is not another `except` clause. Broadly catching TypeError
or ValueError would report our OWN faults as the caller's, with
`retriable: false`, telling an agent never to retry a transient outage. Instead
the two failing shapes are converted at the point of use (`_as_dict`,
`_as_enum`) and `arguments` is shape-checked before any handler reads it.
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


def _call(tool: str, arguments, headers: dict | None = None) -> dict:
    return asyncio.run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
         "params": {"name": tool, "arguments": arguments}},
        headers or {}))


# Each entry is a GENUINELY different failure mode, verified to produce a
# different message. `arguments` itself being the wrong JSON type is listed
# first because it is the one an unauthenticated caller can reach.
BAD_ARGUMENTS = [
    ("arguments is a JSON array", "find_business", ["plumbing"]),
    ("arguments is a JSON string", "find_business", "plumbing"),
    ("nested object sent as a string", "capture_lead",
     {"smb_id": "s", "prospect": "just a name"}),
    ("unknown enum member", "send_message",
     {"smb_id": "s", "recipient_type": "nonsense", "recipient_id": "x",
      "message_type": "marketing", "content": {"body": "hi"}}),
    ("value fails model validation", "send_message",
     {"smb_id": "s", "recipient": {"id_type": "phone", "id_value": "not-e164"},
      "message_type": "transactional", "content": {"body": "hi"}}),
    ("required argument absent", "verify_business", {}),
]


@pytest.mark.parametrize("label,tool,args", BAD_ARGUMENTS,
                         ids=[c[0] for c in BAD_ARGUMENTS])
def test_bad_arguments_are_never_internal_errors(label, tool, args):
    resp = _call(tool, args)
    err = resp.get("error")
    if err is None:
        assert "result" in resp, f"{label}: neither result nor error"
        return
    assert err["code"] != ERR_INTERNAL, (
        f"{label}: reported as a SERVER fault ({err.get('message', '')[:120]}). "
        f"An agent reads -32603 as 'the server is broken' and retries or gives "
        f"up; the caller can fix this and only the caller can.")
    assert err["code"] == ERR_INVALID_PARAMS, (
        f"{label}: expected {ERR_INVALID_PARAMS}, got {err['code']}")


def test_each_case_is_a_distinct_shape():
    """THE GUARD THE PREVIOUS VERSION OF THIS FILE NEEDED AND DID NOT HAVE.

    Its four cases produced one identical response because they all named a
    field the tool does not read. Four parameterisations, one code path, and a
    green suite that looked like coverage. A duplicate must fail here rather
    than quietly inflate the count.
    """
    messages = {}
    for label, tool, args in BAD_ARGUMENTS:
        err = _call(tool, args).get("error") or {}
        messages[label] = err.get("message", "<no error>")
    dupes = {}
    for label, msg in messages.items():
        dupes.setdefault(msg, []).append(label)
    collisions = {m: ls for m, ls in dupes.items() if len(ls) > 1}
    assert not collisions, (
        "these cases are the SAME case wearing different names - they produce "
        f"identical responses, so only one path is really covered: {collisions}")


def test_anonymous_callers_reach_this_path():
    """THE PREMISE THE OLD VERSION OF THIS FILE GOT BACKWARDS.

    It asserted that the auth gate answers before arguments are parsed, and
    used that to claim only paying callers were affected. Both halves were
    wrong: `_WRITE_TOOLS_REQUIRING_AUTH` covers eight write tools, so the
    twelve READ-ONLY tools skip the gate entirely - and those are the ones an
    evaluating agent tries first.

    Pinned as a fact, not an aside: if read tools ever move behind auth, this
    fails and the reasoning above gets re-examined instead of silently rotting.
    """
    assert "find_business" not in ms._WRITE_TOOLS_REQUIRING_AUTH
    resp = _call("find_business", ["plumbing"])          # no headers at all
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, (
        f"an anonymous malformed read call returned {err.get('code')} - "
        f"{err.get('message', '')[:100]}")
    assert "must be a JSON object" in err.get("message", "")


def test_the_error_names_what_to_fix():
    """An error an agent cannot act on is barely better than the wrong code."""
    err = _call("send_message",
                {"smb_id": "s", "recipient_type": "nonsense", "recipient_id": "x",
                 "message_type": "marketing", "content": {"body": "hi"}}).get("error")
    assert err is not None
    msg = err.get("message", "")
    # An enum error must LIST the permitted values. "'nonsense' is not a valid
    # RecipientIdType" sends the agent off to fetch a schema; naming the four
    # options lets it fix the call immediately.
    assert "recipient_type" in msg and "phone" in msg, (
        f"the error must name the field and its allowed values, got: {msg[:140]}")


@pytest.mark.parametrize("exc", [
    RuntimeError("simulated backend outage"),
    ValueError("an internal invariant broke"),
    TypeError("internal type confusion"),
])
def test_genuine_server_faults_are_still_internal(exc):
    """THE OTHER DIRECTION, and the reviewer's sharpest catch.

    The previous version probed only RuntimeError. That let a one-word widening
    - `except ValidationError` to `except ValueError` - pass every test while
    reporting every internal ValueError in the stack (including the enum
    failures this file exists over) to the caller as their own mistake, with
    `retriable: false`.

    ValueError and TypeError are the two that must NOT be swallowed, because
    they are exactly what a careless broadening would catch.
    """
    original = ms._METHOD_HANDLERS["tools/call"]

    async def explode(*_a, **_k):
        raise exc

    ms._METHOD_HANDLERS["tools/call"] = explode
    try:
        resp = _call("verify_business", {"smb_id": "s"})
    finally:
        ms._METHOD_HANDLERS["tools/call"] = original
    assert resp.get("error", {}).get("code") == ERR_INTERNAL, (
        f"{type(exc).__name__} is a SERVER fault and must stay -32603. "
        f"Reporting it as the caller's invalid argument tells an agent never "
        f"to retry something that may well succeed on the next call.")
