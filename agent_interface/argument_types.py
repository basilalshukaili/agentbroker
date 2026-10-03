"""The TYPE of every argument a tool is called with, checked once, against the schema the agent was shown.

WHY THIS EXISTS (Door Reliability Run defect D1, 2026-10-03). `screen_sanctions {"name": 12345}` came back as
`-32603 Internal error: 'int' object has no attribute 'strip'`, and so did `verify_company_record`. JSON-RPC
-32603 tells an agent "the server is broken: back off, retry or give up"; the truth was "your argument is the
wrong type: fix it and call again". It is also a Python exception text on the public wire, reachable with no
credentials.

THE BUG CLASS HAD ALREADY BEEN FIXED THREE TIMES, ONE EXCEPTION TYPE AT A TIME (a missing argument, a pydantic
ValidationError, a nested object of the wrong shape), and each fix left the next handler down the list
unguarded. Probing the dispatcher with every declared argument of every tool once per wrong JSON type, on
2026-10-04, found twelve tools still leaking. The remedy is not a thirteenth `except` clause: broadly catching
TypeError or AttributeError would report our OWN faults as the caller's mistake, with `retriable: false`
(see the note above `_as_dict` in mcp_server.py). It is to state the contract in ONE place, from the schema
every agent is already shown by tools/list, and to enforce it before anything is held, charged or run.

WHAT IT CHECKS. Only JSON types (`string`, `integer`, `number`, `boolean`, `array`, `object`), for each
property the schema DECLARES, recursing into declared object properties and array items. It does not check
enums, ranges or lengths: those are values, and the handlers answer them with their own, more specific
guidance. An argument the schema does not declare is left alone (legacy aliases such as send_message's flat
`recipient_id` are read by the handler, not by the schema).

  * `integer` accepts an integer or an integral float (`5.0`), as JSON Schema does, and never a boolean, a
    fraction, a numeric string or a non-finite number. `number` is the same without the integrality rule.
  * An explicit `null` for an OPTIONAL argument means "not given" and is removed, so the handler's own default
    applies. Before this, `max_results: null` reached `int(None)`. For a REQUIRED argument it is a type error.

TWO DELIBERATE EXCEPTIONS, both pinned by tests/unit/test_wrong_type_arguments_are_guided.py so that adding a
third has to be a visible decision rather than a quiet one:

  * READER_OWNS_ITS_ARGUMENTS - find_business reads its own request (core/find_business_input.py), which
    interprets the shapes callers really write (`location` as a plain string, a fractional `max_results`) and
    answers the rest with find_business-specific guidance.
  * ACCEPTED_ALSO - a (tool, argument) pair whose handler converts another JSON type on purpose.

This module is pure: no I/O, no logging, no imports from the rest of the service.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

READER_OWNS_ITS_ARGUMENTS = frozenset({"find_business"})

ACCEPTED_ALSO: Mapping[tuple, frozenset] = {
    # `content` may be a bare string: _build_send_message_request wraps it as {"body": <the string>}.
    ("send_message", "content"): frozenset({"string"}),
    # `timestamp` is run through int() by handle_mint_key_mcp, which answers a non-integer in its own words.
    ("mint_key", "timestamp"): frozenset({"string"}),
}

_KNOWN_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})
_PHRASE = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean (true or false)",
    "array": "an array",
    "object": "a JSON object",
    "null": "null",
}
_GOT = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "array": "an array",
    "object": "an object",
    "null": "null",
}

MAX_DEPTH = 6              # deeper than any published schema; a bound against hostile nesting
MAX_ITEMS_CHECKED = 500    # per array
MAX_PROBLEMS = 20          # per call; the message names the first six


@dataclass(frozen=True)
class Problem:
    path: str        # "name", "prospect.name", "parties[1]"
    expected: str    # the schema's declared type, "string" or "string|null"
    got: str         # JSON type name of what arrived: "integer", "null", ...
    detail: str      # how the message describes what arrived ("a non-integer number")


def json_type(value: Any) -> str:
    """The JSON type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):          # before int: bool is an int subclass in Python
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "object"


def type_phrase(value: Any) -> str:
    """How a message names what arrived: "an integer", "a string", "null", "an array"..."""
    return _GOT[json_type(value)]


def matches(value: Any, declared: str) -> bool:
    """Does `value` have the JSON Schema type `declared`? A boolean is never a number."""
    actual = json_type(value)
    if declared == "number":
        return actual == "integer" or (actual == "number" and math.isfinite(value))
    if declared == "integer":
        return actual == "integer" or (actual == "number" and math.isfinite(value) and value.is_integer())
    return actual == declared


def _declared(sub: Any) -> list:
    """The known JSON types a property schema declares, as a list (empty = nothing to check)."""
    if not isinstance(sub, dict):
        return []
    t = sub.get("type")
    if isinstance(t, str):
        t = [t]
    if not isinstance(t, (list, tuple)):
        return []
    names = [x for x in t if x in _KNOWN_TYPES or x == "null"]
    if len(names) != len(t):
        return []                       # a type word this module does not know: say nothing rather than guess
    return names


def _describe_got(value: Any, expected: list) -> str:
    actual = json_type(value)
    if actual == "number" and isinstance(value, float):
        if not math.isfinite(value):
            return "a non-finite number"
        if "integer" in expected and "number" not in expected:
            return "a non-integer number"
    return _GOT[actual]


def _problem(path: str, expected: list, value: Any, problems: list) -> None:
    if len(problems) < MAX_PROBLEMS:
        problems.append(Problem(path=path, expected="|".join(expected), got=json_type(value),
                                detail=_describe_got(value, expected)))


def _value(tool: str, sub: dict, value: Any, path: str, problems: list, depth: int) -> Any:
    expected = _declared(sub)
    if expected and not any(matches(value, t) for t in expected):
        if json_type(value) in ACCEPTED_ALSO.get((tool, path), ()):
            return value
        _problem(path, expected, value, problems)
        return value
    if depth >= MAX_DEPTH:
        return value
    if isinstance(value, dict) and isinstance(sub.get("properties"), dict):
        return _object(tool, sub, value, path + ".", problems, depth + 1)
    if isinstance(value, list) and isinstance(sub.get("items"), dict):
        items_schema = sub["items"]
        if not _declared(items_schema) and not items_schema.get("properties"):
            return value
        out = list(value)
        for i, item in enumerate(value[:MAX_ITEMS_CHECKED]):
            item_path = f"{path}[{i}]"
            if item is None:
                if _declared(items_schema) and "null" not in _declared(items_schema):
                    _problem(item_path, _declared(items_schema), item, problems)
                continue
            out[i] = _value(tool, items_schema, item, item_path, problems, depth + 1)
        return out
    return value


def _object(tool: str, schema: dict, value: dict, prefix: str, problems: list, depth: int) -> dict:
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = dict(value)
    for key, sub in props.items():
        if key not in value:
            continue
        path = f"{prefix}{key}"
        item = value[key]
        if item is None:
            if "null" in _declared(sub):
                continue                         # the schema allows null here: leave it
            if key in required:
                if _declared(sub):
                    _problem(path, _declared(sub), item, problems)
                continue
            del out[key]                          # optional + null == not given
            continue
        out[key] = _value(tool, sub, item, path, problems, depth)
    return out


def check(tool: str, schema: Optional[dict], arguments: Any) -> tuple:
    """(arguments to dispatch, problems). Problems empty == every declared argument has its declared type.

    The returned arguments are a copy: optional nulls are removed. The caller's dict is not modified.
    """
    if tool in READER_OWNS_ITS_ARGUMENTS or not isinstance(schema, dict) or not isinstance(arguments, dict):
        return arguments, []
    problems: list = []
    cleaned = _object(tool, schema, arguments, "", problems, 0)
    return cleaned, problems


def explain(tool: str, problems: list) -> str:
    """One sentence an agent can act on: which arguments, what they must be, what arrived, and that the
    refusal was free. The caller's value is never echoed back - only its JSON type."""
    shown = []
    for p in problems[:6]:
        want = " or ".join(_PHRASE[t] for t in p.expected.split("|"))
        shown.append(f"'{p.path}' must be {want}, got {p.detail}")
    more = f" (and {len(problems) - 6} more)" if len(problems) > 6 else ""
    noun = "argument" if len(problems) == 1 else "arguments"
    return (f"Invalid {noun} for '{tool}': " + "; ".join(shown) + more + ". "
            "Fix the type and call again - nothing was run or charged. "
            "Types are exact: see inputSchema from tools/list.")


def as_data(problems: list) -> tuple:
    """(invalid_fields, expected_types) for the JSON-RPC error's `data`."""
    fields = [f"{p.path} (expected {p.expected.replace('|', ' or ')}, got {p.got})" for p in problems[:6]]
    expected = {p.path: p.expected for p in problems[:6]}
    return fields, expected
