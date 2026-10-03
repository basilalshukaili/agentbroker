"""A wrong-TYPE argument must be answered with a typed, guided validation error, on every tool.

THE DEFECT (Door Reliability Run, 2026-10-03, defect D1). `screen_sanctions {"name": 12345}` came back as

    -32603  Internal error: 'int' object has no attribute 'strip'

and so did `verify_company_record`. -32603 tells an agent "the server is broken: back off or give up"; the
truth was "your argument is the wrong type: fix it and call again". It is also a Python exception text on the
public wire, and it was reached with no credentials.

WHY THIS FILE SWEEPS INSTEAD OF TESTING TWO TOOLS. The repo already fixed this bug class three times, one
exception type at a time (a missing argument, a pydantic ValidationError, a nested value of the wrong shape),
and each time the next tool down the list still leaked. Probing the live dispatcher with every declared
argument of every tool, once per wrong JSON type, found the leak in twelve tools on 2026-10-04:
schedule_appointment, get_status, get_outcome, import_booking_url, check_compliance, verify_company_record,
screen_sanctions, map_trade_restriction, lookup_us_contracts (and pydantic-backed tools that were typed only
by accident, with messages that named no accepted type). So the contract is stated once, over the schema the
agent was actually shown, and enforced in one place before anything is held, charged or run.

WHAT IS ASSERTED, for every (tool, declared argument, wrong JSON type) -
  * the handler is never reached (the dispatcher's seam is replaced by a tripwire),
  * the answer is -32602 with data.error_code == "invalid_argument" and retriable == False,
  * it names the argument and the accepted type, and lists the argument in data.invalid_fields,
  * it does not echo the caller's value back,
  * it says nothing a Python traceback would.
Nested fields and array items are swept the same way.

THE TWO DELIBERATE EXCEPTIONS are pinned by name so that adding a third has to be a visible decision:
find_business (it has its own reader, core/find_business_input.py, which interprets documented alternative
shapes, e.g. `location` as a plain string) and the two (tool, field) pairs whose handler converts another type
on purpose.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import socket
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("JWT_SIGNING_SECRET", "test-secret-long-enough-for-the-check")

from agent_interface import mcp_server as ms  # noqa: E402

ERR_INTERNAL = -32603
ERR_INVALID_PARAMS = -32602

# What a Python exception text looks like when it reaches a caller.
LEAK = re.compile(
    r"(?i)internal error|traceback|object has no attribute|nonetype|keyerror|typeerror|attributeerror"
    r"|unhashable|is not a valid|invalid literal|argument must be a string|must be a real number")

SENTINEL = "zz-sentinel-9f3c"

# The only tools whose arguments are read by something other than the generic guard, and why.
EXEMPT_TOOLS = {"find_business"}
# (tool, argument) -> JSON types the handler is written to accept IN ADDITION to the schema's.
ACCEPTED_ALSO = {
    # `content` may be a bare string: _build_send_message_request wraps it as {"body": ...}.
    ("send_message", "content"): {"string"},
    # `timestamp` is run through int() by handle_mint_key_mcp, which answers a non-integer itself.
    ("mint_key", "timestamp"): {"string"},
}

WRONG_VALUES = [
    ("integer", 12345),
    ("number", 1.5),
    ("boolean", True),
    ("string", SENTINEL),
    ("array", [SENTINEL]),
    ("object", {"k": SENTINEL}),
]

WORDS = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
         "array": "array", "object": "object"}


def _jtype(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _is(value, declared: str) -> bool:
    """An oracle written independently of the code under test."""
    actual = _jtype(value)
    if declared == "number":
        return actual in ("integer", "number")
    if declared == "integer":
        return actual == "integer" or (actual == "number" and math.isfinite(value) and value.is_integer())
    return actual == declared


def _call(tool: str, arguments, profile: str | None = None, headers: dict | None = None) -> dict:
    return asyncio.run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
         "params": {"name": tool, "arguments": arguments}}, headers or {}, profile))


def _sample(prop: dict):
    """A plausible valid value for a schema property (only used to fill the OTHER required arguments)."""
    if "enum" in prop:
        return prop["enum"][0]
    t = prop.get("type")
    if t == "string":
        return "test"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {k: _sample(prop["properties"][k]) for k in prop.get("required") or []
                if k in (prop.get("properties") or {})}
    return "x"


def _baseline(schema: dict) -> dict:
    props = schema.get("properties") or {}
    return {k: _sample(props[k]) for k in schema.get("required") or [] if k in props}


def _walk(schema: dict, prefix: str = ""):
    """(path, declared type, property schema, container path) for every typed property, nested ones included."""
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        yield path, prop.get("type"), prop
        if prop.get("type") == "object" and prop.get("properties"):
            yield from _walk(prop, path + ".")
        if prop.get("type") == "array" and isinstance(prop.get("items"), dict) and prop["items"].get("type"):
            yield f"{path}[0]", prop["items"]["type"], prop["items"]


def _set(arguments: dict, schema: dict, path: str, value) -> dict:
    """arguments with `path` (a.b or a[0]) set to value, creating the containers the schema requires."""
    out = json.loads(json.dumps(arguments))
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    cursor = out
    node = schema
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part.startswith("["):
            if last:
                cursor[:] = [value]
            else:
                raise AssertionError("arrays of arrays are not in any schema")
            return out
        prop = (node.get("properties") or {}).get(part, {})
        if last:
            cursor[part] = value
        else:
            nxt = cursor.get(part)
            if prop.get("type") == "array":
                cursor[part] = [] if not isinstance(nxt, list) else nxt
                cursor = cursor[part]
                node = prop.get("items") or {}
                # the next part is "[0]"; keep the list itself as the cursor
                continue
            if not isinstance(nxt, dict):
                cursor[part] = _baseline(prop) if prop.get("type") == "object" else {}
            cursor = cursor[part]
            node = prop
    return out


def _tools() -> list[dict]:
    return ms._build_tool_list()


def _cases():
    for tool in _tools():
        name = tool["name"]
        if name in EXEMPT_TOOLS:
            continue
        schema = tool["inputSchema"]
        for path, declared, _prop in _walk(schema):
            if declared not in WORDS:
                continue
            top = re.split(r"[.\[]", path)[0]
            if top == "idempotency_key":          # popped before validation by design
                continue
            for label, value in WRONG_VALUES:
                if _is(value, declared):
                    continue
                if label in ACCEPTED_ALSO.get((name, path), set()):
                    continue
                yield pytest.param(name, path, declared, label, value, id=f"{name}.{path}<-{label}")


CASES = list(_cases())


@pytest.fixture
def tripwire(monkeypatch):
    """No tool handler may run for a wrong-typed argument. Reaching it is the failure."""
    reached = []

    async def _boom(name, args, headers=None, skip_auth=False):
        reached.append((name, args))
        raise RuntimeError("HANDLER REACHED with a wrong-typed argument")

    monkeypatch.setattr(ms, "_dispatch_and_label", _boom)
    return reached


@pytest.mark.parametrize("tool,path,declared,label,value", CASES)
def test_a_wrong_typed_argument_is_refused_with_a_guided_error(tripwire, tool, path, declared, label, value):
    schema = next(t["inputSchema"] for t in _tools() if t["name"] == tool)
    arguments = _set(_baseline(schema), schema, path, value)
    resp = _call(tool, arguments)

    assert not tripwire, f"{tool}.{path}={label}: the handler ran instead of refusing"
    err = resp.get("error")
    assert err is not None, f"{tool}.{path}={label}: accepted ({str(resp)[:160]})"
    assert err["code"] == ERR_INVALID_PARAMS, f"{tool}.{path}={label}: {err['code']} {err['message'][:120]}"
    msg = err["message"]
    assert not LEAK.search(msg), f"{tool}.{path}={label}: leaks an exception text: {msg[:160]}"
    assert path in msg, f"{tool}.{path}={label}: does not name the argument: {msg[:160]}"
    assert WORDS[declared] in msg.lower(), f"{tool}.{path}={label}: does not name the accepted type: {msg[:160]}"
    assert label in msg.lower() or (label == "number" and "number" in msg.lower()), (
        f"{tool}.{path}={label}: does not say what it received: {msg[:160]}")
    data = err.get("data") or {}
    assert data.get("error_code") == "invalid_argument"
    assert data.get("retriable") is False
    assert any(f.startswith(path + " (") for f in data.get("invalid_fields", [])), data
    assert data.get("expected_types", {}).get(path) == declared, data
    assert SENTINEL not in json.dumps(resp), "the caller's value was echoed back"


def test_the_sweep_covers_every_tool_and_is_not_trivially_small():
    swept = {c.values[0] for c in CASES}
    every = {t["name"] for t in _tools()}
    # Tools with no typed arguments at all cannot be swept; the rest must all be here.
    typed = {t["name"] for t in _tools()
             if any(d in WORDS for _p, d, _s in _walk(t["inputSchema"]))}
    assert (typed - EXEMPT_TOOLS) <= swept, f"never swept: {sorted((typed - EXEMPT_TOOLS) - swept)}"
    assert len(every) == 23, "the tool set changed: re-read this test's exemptions"
    assert len(CASES) > 250, len(CASES)


def test_the_exemptions_are_exactly_the_ones_this_file_documents():
    from agent_interface import argument_types as at
    assert set(at.READER_OWNS_ITS_ARGUMENTS) == EXEMPT_TOOLS
    assert {k: set(v) for k, v in at.ACCEPTED_ALSO.items()} == ACCEPTED_ALSO
    props = {t["name"]: {p for p, _d, _s in _walk(t["inputSchema"])} for t in _tools()}
    for (tool, path) in ACCEPTED_ALSO:
        assert path in props[tool], f"{tool}.{path} is not a declared argument any more"


# ---------------------------------------------------------------------------
# The two named repros, through the doors a reviewer would use
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile,tool", [
    ("sanctions-screening", "screen_sanctions"),
    ("company-verification", "verify_company_record"),
    (None, "screen_sanctions"),
    (None, "verify_company_record"),
])
def test_drr_d1_a_number_for_name_is_a_guided_error_not_an_internal_one(profile, tool):
    resp = _call(tool, {"name": 12345}, profile=profile)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "name" in err["message"] and "string" in err["message"].lower()
    assert "integer" in err["message"].lower()
    assert not LEAK.search(err["message"]) and "strip" not in err["message"]
    assert err["data"]["error_code"] == "invalid_argument"


def test_the_error_says_nothing_was_run_or_charged():
    err = _call("screen_sanctions", {"name": 12345})["error"]
    assert "nothing was run or charged" in err["message"].lower()


def test_several_wrong_arguments_are_all_named_in_one_answer():
    err = _call("screen_sanctions", {"name": 12345, "country": ["OM"], "type": True})["error"]
    for field in ("name", "country", "type"):
        assert field in err["message"]
    assert len(err["data"]["invalid_fields"]) == 3


# ---------------------------------------------------------------------------
# What counts as the right type
# ---------------------------------------------------------------------------

def test_null_for_an_optional_argument_means_omitted(monkeypatch):
    seen = {}

    async def _stub(name, args, headers=None, skip_auth=False):
        seen["args"] = dict(args)
        return {"status": "success", "result": {}}

    monkeypatch.setattr(ms, "_dispatch_and_label", _stub)
    resp = _call("lookup_us_contracts", {"company_name": "Acme", "max_results": None})
    assert "error" not in resp, resp
    assert seen["args"] == {"company_name": "Acme"}, "an explicit null must reach the handler as 'not given'"


def test_null_for_a_required_argument_is_refused_by_name():
    err = _call("screen_sanctions", {"name": None})["error"]
    assert err["code"] == ERR_INVALID_PARAMS
    assert "name" in err["message"] and "null" in err["message"].lower()
    assert not LEAK.search(err["message"])


@pytest.mark.parametrize("value", [True, False, 1.5, "5", float("inf"), float("nan")])
def test_an_integer_argument_does_not_accept_a_boolean_a_fraction_a_string_or_a_non_finite(tripwire, value):
    err = _call("lookup_us_contracts", {"company_name": "Acme", "max_results": value}).get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS and "max_results" in err["message"], err
    assert not tripwire


@pytest.mark.parametrize("value", [5, 5.0, 10])
def test_an_integer_argument_accepts_an_integer_and_an_integral_float(monkeypatch, value):
    async def _stub(name, args, headers=None, skip_auth=False):
        return {"status": "success", "result": {}}

    monkeypatch.setattr(ms, "_dispatch_and_label", _stub)
    assert "error" not in _call("lookup_us_contracts", {"company_name": "Acme", "max_results": value})


def test_nested_types_are_named_by_path(tripwire):
    err = _call("map_trade_restriction", {"product": "coffee", "destination_country": "DE",
                                          "parties": ["Acme", 5]})["error"]
    assert "parties[1]" in err["message"], err["message"]
    err = _call("capture_lead", {"smb_id": "s", "prospect": {"name": 5}})["error"]
    assert "prospect.name" in err["message"], err["message"]
    assert not tripwire


def test_arguments_the_schema_does_not_declare_are_left_alone(monkeypatch):
    """Legacy aliases (send_message's recipient_id/recipient_type) are not in the schema and must still reach
    the handler that reads them."""
    seen = {}

    async def _stub(name, args, headers=None, skip_auth=False):
        seen["args"] = dict(args)
        return {"status": "success", "result": {}}

    monkeypatch.setattr(ms, "_dispatch_and_label", _stub)
    monkeypatch.setattr(ms, "_channel_gate", lambda *a, **k: None)     # this deployment has no SMS channel
    resp = _call("send_message", {"recipient_id": "+14045550100", "recipient_type": "phone",
                                  "message_type": "transactional", "content": "hello"})
    assert "error" not in resp, resp
    assert seen["args"]["recipient_id"] == "+14045550100"
    assert seen["args"]["content"] == "hello", "a bare string content is a deliberate, documented leniency"


def test_a_refused_call_reaches_no_gate_so_it_costs_and_holds_nothing(monkeypatch):
    """The type check runs before the channel gate, the identity gate and every billing rail, so a typo in
    an argument consumes no quota, no credit hold and no free-tier operation."""
    reached = []
    monkeypatch.setattr(ms, "_channel_gate", lambda *a, **k: reached.append("channel_gate"))
    monkeypatch.setattr(ms, "_mcp_gate_identity", lambda *a, **k: reached.append("identity_gate"))
    resp = _call("send_message", {"recipient": "+14045550100", "message_type": "transactional",
                                  "content": {"body": "hi"}})
    assert resp.get("error", {}).get("code") == ERR_INVALID_PARAMS, resp
    assert not reached, reached


# ---------------------------------------------------------------------------
# The envelope itself: `params` that is not an object
# ---------------------------------------------------------------------------

def _rpc(method: str, params, profile: str | None = None, with_id: bool = True):
    payload = {"jsonrpc": "2.0", "method": method, "params": params}
    if with_id:
        payload["id"] = 7
    return asyncio.run(ms.handle_mcp_request(payload, {}, profile))


@pytest.mark.parametrize("profile", [None, "sanctions-screening", "company-verification", "compliance-check"])
@pytest.mark.parametrize("method", ["tools/call", "tools/list", "resources/read", "prompts/get"])
@pytest.mark.parametrize("params", [12345, 1.5, True, "text", ["a"]], ids=["int", "float", "bool", "str", "list"])
def test_params_that_is_not_an_object_is_a_typed_refusal_on_every_door(profile, method, params):
    """Before: on a door `{**params, "_profile": ...}` raised an unhandled TypeError (an HTTP 500); on the full
    server a handler's `params.get` came back as "Internal error: 'int' object has no attribute 'get'"."""
    resp = _rpc(method, params, profile)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "params" in err["message"] and "object" in err["message"]
    assert not LEAK.search(err["message"]), err["message"]
    assert err["data"]["error_code"] == "invalid_argument" and err["data"]["retriable"] is False
    assert err["data"]["expected_types"] == {"params": "object"}
    assert "text" not in err["message"].replace("context", "")        # the value is never echoed


def test_a_notification_with_odd_params_is_still_never_answered():
    assert _rpc("notifications/initialized", 12345, with_id=False) is None
    assert _rpc("notifications/initialized", "x", "sanctions-screening", with_id=False) is None


@pytest.mark.parametrize("params", [None, {}, [], 0, False, ""])
def test_absent_or_empty_params_are_still_fine(params):
    resp = _rpc("tools/list", params)
    assert "result" in resp and resp["result"]["tools"], resp


# ---------------------------------------------------------------------------
# The exempt tool still must not leak, and the wrong-VALUE enum bug found by the same sweep
# ---------------------------------------------------------------------------

@pytest.fixture
def no_network(monkeypatch):
    def _refuse(*a, **k):
        raise OSError("network disabled for this test")
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


def _find_business_cases():
    schema = next(t["inputSchema"] for t in _tools() if t["name"] == "find_business")
    for field, prop in (schema.get("properties") or {}).items():
        for label, value in WRONG_VALUES + [("null", None)]:
            yield pytest.param(field, label, value, id=f"find_business.{field}<-{label}")


@pytest.mark.parametrize("field,label,value", list(_find_business_cases()))
def test_find_business_reads_its_own_arguments_and_never_leaks(no_network, field, label, value):
    resp = _call("find_business", {"vertical": "home_services", "location": {"zip_or_city": "Nizwa, Oman"},
                                   field: value})
    text = json.dumps(resp)
    assert resp.get("error", {}).get("code") != ERR_INTERNAL, text[:200]
    assert not LEAK.search(text), text[:300]


def test_schedule_appointment_with_an_unknown_action_lists_the_allowed_ones(no_network):
    resp = _call("schedule_appointment", {"smb_id": "s", "action": "nonsense"})
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    for allowed in ("book", "cancel", "check_availability"):
        assert allowed in err["message"]
    assert "action" in err["message"]


def _enum_cases():
    for tool in _tools():
        if tool["name"] in EXEMPT_TOOLS:
            continue
        for path, declared, prop in _walk(tool["inputSchema"]):
            if declared == "string" and "enum" in prop and "[" not in path and "." not in path:
                yield pytest.param(tool["name"], path, id=f"{tool['name']}.{path}")


@pytest.mark.parametrize("tool,path", list(_enum_cases()))
def test_an_unknown_enum_value_never_comes_back_as_an_internal_error(no_network, tool, path):
    schema = next(t["inputSchema"] for t in _tools() if t["name"] == tool)
    resp = _call(tool, _set(_baseline(schema), schema, path, "zz-not-a-member"))
    text = json.dumps(resp)
    assert resp.get("error", {}).get("code") != ERR_INTERNAL, text[:300]
    assert not LEAK.search(text), text[:300]
