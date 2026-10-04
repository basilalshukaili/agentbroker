"""The wrong-type guard, after the independent review of the D1 fix (2026-10-04).

The review fuzzed the envelope (6 doors x 11 methods x 8 wrong types) and the arrays the sweep never reached,
and found five places where D1's own claim - "a wrong-typed argument is refused with a guided -32602 before
anything is held, charged or run" - was not yet true. Every test here failed on the D1 commit.

  1. `method` and `params.name` of the wrong type. `{"method": [1]}` was an HTTP 500 ("unhashable type: 'list'")
     on every door, and `tools/call` with `params.name = ["screen_sanctions"]` was a -32603 "Internal error"
     on every narrow door: the same Python exception text on the public wire that D1 set out to remove.
  2. `idempotency_key` was never type-checked: the wrapper pops it first. `12345` and `{"a": 1}` were accepted
     and turned into a claim key by str(); `0`, `false` and `[]` silently switched the retry contract off on
     a write tool, so a retry could double-send or double-charge.
  3. The guard looked only at the first 500 items of an array. A wrong-typed item at index 500 or later passed
     it, reached the billing rail, and failed late.
  4. The idempotency claim was taken before the type guard ran, so a refused call briefly held a key.
  5. An unknown enum value was echoed back unbounded.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("JWT_SIGNING_SECRET", "test-secret-long-enough-for-the-check")

from agent_interface import argument_types as at  # noqa: E402
from agent_interface import mcp_server as ms  # noqa: E402
from agent_interface import profiles as _profiles  # noqa: E402

ERR_INVALID_REQUEST = -32600
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

LEAK = re.compile(
    r"(?i)internal error|traceback|object has no attribute|nonetype|keyerror|typeerror|attributeerror"
    r"|unhashable|is not a valid|invalid literal|argument must be a string|must be a real number")

DOORS = [None, *sorted(_profiles.PROFILES)]
HDRS = {"x-agent-identity": "test-bearer-token-abc"}


def rpc(payload, profile=None, headers=None):
    return asyncio.run(ms.handle_mcp_request(payload, headers or {}, profile))


def call(name, arguments, profile=None, headers=None):
    return rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}, profile, headers)


def _schema(tool):
    return next(t["inputSchema"] for t in ms._build_tool_list() if t["name"] == tool)


def _baseline(schema):
    props = schema.get("properties") or {}
    out = {}
    for key in schema.get("required") or []:
        prop = props.get(key, {})
        t = prop.get("type")
        out[key] = ({"string": "test", "integer": 1, "number": 1.0, "boolean": True, "array": []}.get(t)
                    if t != "object" else {k: "test" for k in prop.get("required") or []})
        if "enum" in prop:
            out[key] = prop["enum"][0]
    return out


@pytest.fixture
def reached(monkeypatch):
    """Anything past the type guard is recorded. A refused call must leave this empty."""
    seen = []

    async def _dispatch(name, args, headers=None, skip_auth=False):
        seen.append(("dispatch", name))
        return {"status": "success", "result": {}}

    monkeypatch.setattr(ms, "_dispatch_and_label", _dispatch)
    monkeypatch.setattr(ms, "_channel_gate", lambda *a, **k: seen.append(("channel_gate",)))
    monkeypatch.setattr(ms, "_mcp_gate_identity", lambda *a, **k: seen.append(("identity_gate",)))
    return seen


# ---------------------------------------------------------------------------
# 1. The envelope: `method` and `params.name`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", DOORS)
@pytest.mark.parametrize("method", [[1], {"a": 1}, ["tools/list"], 5, 1.5, True],
                         ids=["list", "dict", "list-of-str", "int", "float", "bool"])
def test_a_method_that_is_not_a_string_is_a_typed_invalid_request_on_every_door(profile, method):
    resp = rpc({"jsonrpc": "2.0", "id": 3, "method": method}, profile)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_REQUEST, resp
    assert "method" in err["message"] and "string" in err["message"]
    assert not LEAK.search(err["message"]), err["message"]


@pytest.mark.parametrize("method", [[1], {"a": 1}, 5])
def test_a_request_without_an_id_and_a_bad_method_is_still_an_invalid_request_not_a_crash(method):
    resp = rpc({"jsonrpc": "2.0", "method": method})
    assert (resp.get("error") or {}).get("code") == ERR_INVALID_REQUEST, resp


@pytest.mark.parametrize("path", ["/mcp", *[f"/mcp/{p}" for p in sorted(_profiles.PROFILES)]])
@pytest.mark.parametrize("method", [[1], {"a": 1}])
def test_a_method_that_is_not_a_string_is_not_an_http_500_on_any_door(path, method):
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app, raise_server_exceptions=False).post(
        path, json={"jsonrpc": "2.0", "id": 1, "method": method})
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert r.json()["error"]["code"] == ERR_INVALID_REQUEST, r.text[:200]
    assert not LEAK.search(r.text)


@pytest.mark.parametrize("profile", DOORS)
@pytest.mark.parametrize("name", [["screen_sanctions"], {"n": 1}, 5, 1.5, True, False, 0, []],
                         ids=["list", "dict", "int", "float", "true", "false", "zero", "empty-list"])
def test_a_tool_name_that_is_not_a_string_is_a_guided_argument_error_on_every_door(reached, profile, name):
    resp = call(name, {}, profile)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "'name'" in err["message"] and "string" in err["message"].lower()
    assert not LEAK.search(err["message"]), err["message"]
    data = err["data"]
    assert data["error_code"] == "invalid_argument" and data["retriable"] is False
    assert any(f.startswith("name (") for f in data["invalid_fields"]), data
    assert not reached


def test_a_tool_name_that_is_not_a_string_is_refused_before_the_idempotency_wrapper_reads_it(reached):
    """The wrapper asks `name in <set>` as soon as a key is present; an unhashable name raised there."""
    resp = call(["send_message"], {"idempotency_key": "k-1"}, headers=HDRS)
    assert (resp.get("error") or {}).get("code") == ERR_INVALID_PARAMS, resp
    assert not LEAK.search(json.dumps(resp))
    assert not reached


@pytest.mark.parametrize("name", [["screen_sanctions"], {"n": 1}, 5, True])
def test_the_impl_checks_the_tool_name_itself_and_does_not_rely_on_its_wrapper(name):
    """_h_tools_call_impl is reached through the idempotency wrapper today; it must not depend on that."""
    with pytest.raises(ms._ArgumentTypeError) as ei:
        asyncio.run(ms._h_tools_call_impl({"name": name, "arguments": {}}, {}))
    assert ei.value.problems[0].path == "name"


def test_a_missing_name_is_still_a_missing_name():
    resp = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}})
    assert "Missing 'name'" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# 2. idempotency_key
# ---------------------------------------------------------------------------

@pytest.fixture
def idem(monkeypatch):
    """A hermetic idempotency store, a spy on the claim, and a stub for everything after the guard."""
    import storage.supabase_client as sb
    from agent_interface import idempotency_gate as ig
    from storage.idempotency_store import get_idempotency_store

    async def _no_rows(*a, **k):
        return []

    async def _no_insert(*a, **k):
        return None

    monkeypatch.setattr(sb, "select_rows", _no_rows)
    monkeypatch.setattr(sb, "insert_row", _no_insert)
    get_idempotency_store()._store.clear()

    claims, impl_calls = [], []
    real_claim = ig.claim

    async def spy_claim(scope, tool, key):
        claims.append((tool, key))
        return await real_claim(scope, tool, key)

    monkeypatch.setattr(ig, "claim", spy_claim)
    yield claims, impl_calls
    get_idempotency_store()._store.clear()


def _send_args(**extra):
    return {"recipient": {"id_value": "+15551230000"}, "message_type": "transactional",
            "content": {"body": "hi"}, **extra}


@pytest.mark.parametrize("value", [12345, 1.5, True, False, 0, [], ["k"], {"a": 1}, {}],
                         ids=["int", "float", "true", "false", "zero", "empty-list", "list", "dict", "empty-dict"])
def test_an_idempotency_key_of_the_wrong_type_is_refused_not_stringified_or_ignored(idem, monkeypatch, value):
    claims, impl_calls = idem

    async def impl(params, headers=None):
        impl_calls.append(params)
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    resp = call("send_message", _send_args(idempotency_key=value), headers=HDRS)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "idempotency_key" in err["message"] and "string" in err["message"].lower()
    assert not LEAK.search(err["message"])
    assert err["data"]["error_code"] == "invalid_argument" and err["data"]["retriable"] is False
    assert any(f.startswith("idempotency_key (") for f in err["data"]["invalid_fields"])
    assert not claims and not impl_calls, "a key of the wrong type must not claim, and the tool must not run"


@pytest.mark.parametrize("value,why", [("", "empty"), ("   ", "blank"), ("k" * 129, "too long")])
def test_an_idempotency_key_that_is_empty_or_longer_than_the_declared_maximum_is_refused(idem, monkeypatch, value, why):
    claims, impl_calls = idem

    async def impl(params, headers=None):
        impl_calls.append(params)
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    err = call("send_message", _send_args(idempotency_key=value), headers=HDRS).get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, why
    assert "idempotency_key" in err["message"] and "128" in err["message"]
    assert not claims and not impl_calls


@pytest.mark.parametrize("value", ["k", "a-retry-key-0001", "k" * 128])
def test_a_proper_idempotency_key_is_claimed_as_given(idem, monkeypatch, value):
    claims, impl_calls = idem

    async def impl(params, headers=None):
        impl_calls.append(dict(params["arguments"]))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    resp = call("send_message", _send_args(idempotency_key=value), headers=HDRS)
    assert "error" not in resp, resp
    assert claims == [("send_message", value)]
    assert "idempotency_key" not in impl_calls[0], "the key is consumed by the wrapper"


def test_a_null_idempotency_key_means_not_given(idem, monkeypatch):
    claims, impl_calls = idem

    async def impl(params, headers=None):
        impl_calls.append(dict(params["arguments"]))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    resp = call("send_message", _send_args(idempotency_key=None), headers=HDRS)
    assert "error" not in resp, resp
    assert not claims and len(impl_calls) == 1


def test_a_tool_that_does_not_declare_idempotency_key_still_ignores_one(idem, monkeypatch):
    """Only the write tools advertise the key; on any other tool it is consumed and has no effect, as before."""
    claims, impl_calls = idem

    async def impl(params, headers=None):
        impl_calls.append(dict(params["arguments"]))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    resp = call("lookup_us_contracts", {"company_name": "Acme", "idempotency_key": 5}, headers=HDRS)
    assert "error" not in resp, resp
    assert not claims and "idempotency_key" not in impl_calls[0]


def test_a_call_refused_for_another_argument_never_takes_the_idempotency_claim(idem):
    """The claim used to be taken first and released after the type guard raised; for that moment the key was
    held by a call that was never going to run."""
    claims, _ = idem
    resp = call("send_message", {"recipient": 5, "message_type": "transactional", "content": {"body": "hi"},
                                 "idempotency_key": "k-held"}, headers=HDRS)
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS and "recipient" in err["message"], resp
    assert claims == [], "the key was claimed for a call the type guard refused"


# ---------------------------------------------------------------------------
# 3. Arrays: every item is checked, wherever it sits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 3, 499, 500, 501, 5000])
def test_a_wrong_typed_item_is_caught_at_any_index(reached, n):
    schema = _schema("escalate_to_human")
    arguments = {**_baseline(schema), "context": {"transcript": [{} for _ in range(n)] + [7]}}
    err = call("escalate_to_human", arguments).get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, (n, err)
    assert f"context.transcript[{n}]" in err["message"], err["message"][:200]
    assert err["data"]["error_code"] == "invalid_argument"
    assert not reached, f"item {n} passed the guard and reached {reached}"


def test_a_long_array_of_correctly_typed_items_is_still_accepted(reached):
    schema = _schema("escalate_to_human")
    arguments = {**_baseline(schema), "context": {"transcript": [{} for _ in range(5000)]}}
    resp = call("escalate_to_human", arguments)
    assert (resp.get("error") or {}).get("code") != ERR_INVALID_PARAMS, resp
    assert ("channel_gate",) in reached, "a valid 5000-item transcript must pass the guard"


def test_an_array_too_large_to_check_is_refused_with_a_guided_error_rather_than_checked_in_part(reached):
    schema = _schema("escalate_to_human")
    big = [{}] * (at.MAX_VALUES_CHECKED + 1)
    err = call("escalate_to_human", {**_baseline(schema), "context": {"transcript": big}}).get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, err
    assert "too large" in err["message"].lower() and "context.transcript" in err["message"]
    assert err["data"]["error_code"] == "invalid_argument" and err["data"]["retriable"] is False
    assert str(at.MAX_VALUES_CHECKED) in err["message"]
    assert not reached


def test_the_checker_itself_reports_the_index_of_a_late_bad_item():
    schema = {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "string"}}}}
    _cleaned, problems = at.check("t", schema, {"xs": ["a"] * 2000 + [1]})
    assert [p.path for p in problems] == ["xs[2000]"]


# ---------------------------------------------------------------------------
# 5. An enum value is not echoed without bound
# ---------------------------------------------------------------------------

def test_an_unknown_action_is_not_echoed_in_full():
    long_value = "zz-" + "x" * 5000
    err = call("schedule_appointment", {"smb_id": "s", "action": long_value}).get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, err
    assert long_value not in err["message"] and len(err["message"]) < 600, len(err["message"])
    for allowed in ("book", "cancel", "check_availability"):
        assert allowed in err["message"]


def test_a_short_unknown_action_still_says_what_arrived():
    err = call("schedule_appointment", {"smb_id": "s", "action": "zz-not-a-member"}).get("error") or {}
    assert "zz-not-a-member" in err["message"]


# ---------------------------------------------------------------------------
# The public text says what the code does
# ---------------------------------------------------------------------------

def _errors_section():
    text = open(os.path.join(ROOT, "api", "errors.md"), encoding="utf-8").read()
    return text.split("## Argument errors on MCP")[1].split("\n---")[0]


def test_the_docs_name_every_exception_to_the_type_rule():
    section = _errors_section()
    for exception in ("find_business", "send_message", "mint_key"):
        assert exception in section, exception
    assert set(at.READER_OWNS_ITS_ARGUMENTS) == {"find_business"}
    assert {tool for tool, _arg in at.ACCEPTED_ALSO} == {"send_message", "mint_key"}


def test_the_docs_describe_the_idempotency_key_rule_and_the_params_leniency():
    section = _errors_section()
    assert "idempotency_key" in section and "128" in section
    assert "params" in section and "no parameters" in section
