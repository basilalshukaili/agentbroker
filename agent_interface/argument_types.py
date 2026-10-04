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

EVERY ITEM IS CHECKED, WHEREVER IT SITS. The first version looked at the first 500 items of an array and passed
the rest unseen, so a wrong-typed item at index 500 got past the guard, reached the billing rail and failed late
(found by the independent review of 2026-10-04). Work is bounded by a BUDGET OF VALUES for the whole call
(`MAX_VALUES_CHECKED`, counting every array item and every declared field visited), not by a prefix: a call
within the budget is checked in full, and a call over it is refused outright, with a guided error, instead of
being checked in part.

TWO DELIBERATE EXCEPTIONS, both pinned by tests/unit/test_wrong_type_arguments_are_guided.py so that adding a
third has to be a visible decision rather than a quiet one:

  * READER_OWNS_ITS_ARGUMENTS - find_business reads its own request (core/find_business_input.py), which
    interprets the shapes callers really write (`location` as a plain string, a fractional `max_results`) and
    answers the rest with find_business-specific guidance.
  * ACCEPTED_ALSO - a (tool, argument) pair whose handler converts another JSON type on purpose.

Two arguments the schema does not describe fully get their own checks here, because the dispatcher reads them
before it reads the schema: the tool `name` of a tools/call (`tool_name_problems`) and the write tools'
`idempotency_key` (`idempotency_key_problems`).

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

MAX_DEPTH = 6                  # deeper than any published schema; a bound against hostile nesting
MAX_VALUES_CHECKED = 50_000    # array items + declared fields visited, per call. Every one is checked or the call is refused.
MAX_PROBLEMS = 20              # per call; the message names the first six
IDEMPOTENCY_KEY_MAX = 128      # the maxLength the write tools advertise for it


@dataclass(frozen=True)
class Problem:
    path: str        # "name", "prospect.name", "parties[1]"
    expected: str    # the schema's declared type, "string" or "string|null"
    got: str         # JSON type name of what arrived: "integer", "null", ...
    detail: str      # how the message describes what arrived ("a non-integer number")
    kind: str = "type"   # "type" (wrong JSON type) | "value" (right type, unusable value) | "size" (too large)
    text: str = ""       # for kind != "type": what the argument must be / why it was refused

    def field(self) -> str:
        """The entry for the error's `data.invalid_fields`."""
        if self.kind == "type":
            return f"{self.path} (expected {self.expected.replace('|', ' or ')}, got {self.got})"
        return f"{self.path} ({self.text})"


class _TooLarge(Exception):
    """Raised inside the walk when the budget of values runs out; `check` turns it into a Problem."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _Budget:
    __slots__ = ("left",)

    def __init__(self, left: int) -> None:
        self.left = left

    def spend(self, path: str) -> None:
        self.left -= 1
        if self.left < 0:
            raise _TooLarge(path)


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


def _value(tool: str, sub: dict, value: Any, path: str, problems: list, depth: int, budget: _Budget) -> Any:
    expected = _declared(sub)
    if expected and not any(matches(value, t) for t in expected):
        if json_type(value) in ACCEPTED_ALSO.get((tool, path), ()):
            return value
        _problem(path, expected, value, problems)
        return value
    if depth >= MAX_DEPTH:
        return value
    if isinstance(value, dict) and isinstance(sub.get("properties"), dict):
        return _object(tool, sub, value, path + ".", problems, depth + 1, budget)
    if isinstance(value, list) and isinstance(sub.get("items"), dict):
        items_schema = sub["items"]
        item_types = _declared(items_schema)
        if not item_types and not items_schema.get("properties"):
            return value
        out = list(value)
        # Items with no declared properties (strings, numbers, bare objects) are checked in a tight loop;
        # items with declared properties go through the general walk. Either way EVERY item is visited.
        plain_items = not isinstance(items_schema.get("properties"), dict)
        for i, item in enumerate(value):
            budget.spend(path)
            item_path = f"{path}[{i}]"
            if item is None:
                if item_types and "null" not in item_types:
                    _problem(item_path, item_types, item, problems)
                continue
            if plain_items:
                if item_types and not any(matches(item, t) for t in item_types):
                    _problem(item_path, item_types, item, problems)
                continue
            out[i] = _value(tool, items_schema, item, item_path, problems, depth + 1, budget)
        return out
    return value


def _object(tool: str, schema: dict, value: dict, prefix: str, problems: list, depth: int,
            budget: _Budget) -> dict:
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = dict(value)
    for key, sub in props.items():
        if key not in value:
            continue
        path = f"{prefix}{key}"
        budget.spend(path)
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
        out[key] = _value(tool, sub, item, path, problems, depth, budget)
    return out


def _too_large(path: str) -> Problem:
    return Problem(path=path, expected="bounded", got="array", detail="too many values", kind="size",
                   text=(f"is too large: the arguments of one call may hold at most {MAX_VALUES_CHECKED} "
                         f"values in all (every array item and field counts)"))


def check(tool: str, schema: Optional[dict], arguments: Any) -> tuple:
    """(arguments to dispatch, problems). Problems empty == every declared argument has its declared type.

    The returned arguments are a copy: optional nulls are removed. The caller's dict is not modified.
    """
    if tool in READER_OWNS_ITS_ARGUMENTS or not isinstance(schema, dict) or not isinstance(arguments, dict):
        return arguments, []
    problems: list = []
    try:
        cleaned = _object(tool, schema, arguments, "", problems, 0, _Budget(MAX_VALUES_CHECKED))
    except _TooLarge as too_large:
        return arguments, [_too_large(too_large.path)]
    return cleaned, problems


def tool_name_problems(value: Any) -> list:
    """Problems with the `name` of a tools/call: [] for a string or for no name at all (the dispatcher answers a
    missing one in its own words). Read by the dispatcher before any lookup keyed on it, because a list or an
    object cannot be looked up at all."""
    if value is None or isinstance(value, str):
        return []
    return [Problem(path="name", expected="string", got=json_type(value), detail=_GOT[json_type(value)])]


IDEMPOTENCY_HEADER = "X-Idempotency-Key header"


def idempotency_key_problems(value: Any, path: str = "idempotency_key") -> list:
    """Problems with a write tool's `idempotency_key`, which the dispatcher consumes before it reads the schema.
    `path` names where the key arrived: the argument, or (IDEMPOTENCY_HEADER) the X-Idempotency-Key header,
    which is held to the same rule - it used to be cut to 128 characters, so two different long keys sharing
    a 128-character prefix were claimed as one.

    [] for a usable key and for `null` (not given). A key of any other JSON type is refused - it used to be
    turned into text with str(), and a falsy one (0, false, []) silently switched the retry contract OFF on a
    write tool, so a retry could double-send or double-charge. An empty or blank string is refused for the same
    reason, and so is one over the advertised maximum, which used to be cut short so that two different long
    keys could share a claim."""
    if value is None:
        return []
    if not isinstance(value, str):
        return [Problem(path=path, expected="string", got=json_type(value),
                        detail=_GOT[json_type(value)])]
    if not value.strip():
        return [Problem(path=path, expected="string", got="string", detail="an empty string",
                        kind="value", text=f"must be a non-empty string of at most {IDEMPOTENCY_KEY_MAX} "
                                           f"characters, got an empty string")]
    if len(value) > IDEMPOTENCY_KEY_MAX:
        return [Problem(path=path, expected="string", got="string",
                        detail=f"a string of {len(value)} characters", kind="value",
                        text=f"must be a non-empty string of at most {IDEMPOTENCY_KEY_MAX} characters, got "
                             f"one of {len(value)}")]
    return []


_DEFAULT_HINT = "Types are exact: see inputSchema from tools/list."


def nothing_ran(no_commerce: bool = False, capital: bool = False) -> str:
    """The closing clause of a refusal: nothing ran and nothing was charged. On a no-commerce door (the ChatGPT
    door, agent_interface/no_commerce.py) a refusal says only that nothing ran - "charged" is a word that door
    never says, and no charge was possible there anyway."""
    text = "nothing was run" if no_commerce else "nothing was run or charged"
    return text[0].upper() + text[1:] if capital else text


def explain(tool: str, problems: list, hint: str = _DEFAULT_HINT, no_commerce: bool = False) -> str:
    """One sentence an agent can act on: which arguments, what they must be, what arrived, and that the
    refusal was free. The caller's value is never echoed back - only its JSON type. `no_commerce` drops the
    word "charged" (see nothing_ran)."""
    shown = []
    for p in problems[:6]:
        if p.kind == "type":
            want = " or ".join(_PHRASE[t] for t in p.expected.split("|"))
            shown.append(f"'{p.path}' must be {want}, got {p.detail}")
        else:
            shown.append(f"'{p.path}' {p.text}")
    more = f" (and {len(problems) - 6} more)" if len(problems) > 6 else ""
    noun = "argument" if len(problems) == 1 else "arguments"
    return (f"Invalid {noun} for '{tool}': " + "; ".join(shown) + more + ". "
            f"Fix it and call again - {nothing_ran(no_commerce)}. " + hint)


def as_data(problems: list) -> tuple:
    """(invalid_fields, expected_types) for the JSON-RPC error's `data`. `expected_types` maps the arguments
    that had the wrong JSON TYPE to the type they must have; a value or size problem has no entry there."""
    fields = [p.field() for p in problems[:6]]
    expected = {p.path: p.expected for p in problems[:6] if p.kind == "type"}
    return fields, expected
