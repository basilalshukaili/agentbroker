#!/usr/bin/env python3
"""Every advertised parameter must survive MCP dispatch.

WHY THIS EXISTS, WHEN check_params_do_something ALREADY RUNS.

That guard proves a parameter is USED - it reads the handler and the schema
and reports anything documented but inert. It passed, green, over eight
parameters that no caller could ever deliver, because it looks at the
handler and the gap was one layer above it.

`_dispatch_operation` builds each request object field by field:

    req = ScheduleAppointmentRequest(
        smb_id=args["smb_id"],
        action=AppointmentAction(args.get("action", "book")),
        service=args.get("service"),
        existing_appointment_id=args.get("existing_appointment_id"),
    )

Four of the seven parameters the manifest advertises. `requested_time` -
the time being booked, read in eight places by the handler - arrived as
None on every MCP call ever made. So did `notes`, `customer`,
`on_behalf_of` (the sender disclosure on a transactional message),
`business_id`, `price_band`, `availability_window` and `send_at_iso`.

Nothing was broken and nothing looked wrong: the parameter was in the
manifest, on the model, validated, and used downstream. Only the wire
between them was missing. That is the producer-with-no-caller shape, and
the lesson each time is the same - a green test proves correctness, never
INVOCATION.

THIS IS THE WEAKER HALF OF THE PAIR, AND IT IS MEASURED.

An adversarial reviewer defeated it with 7 of 8 one-line mutations that each
still dropped a parameter - fetching it and discarding it, logging it,
routing its value to None, a whitelist helper, rebinding `args` first, and
the one that matters:

    requested_times=args.get("requested_time")      # one character

These models use pydantic's default extra='ignore', so that constructs
cleanly with requested_time=None. No pattern over source text survives it,
and the `**args` splat escape hatch below is a blanket pass over a whole
branch (today: 14 of 78 advertised parameters).

tests/unit/test_every_param_actually_arrives.py is the half that cannot be
fooled: it drives real dispatch for every tool and asserts on what the
handler was actually handed. It catches all of the above. This script stays
because it names the offending line, runs in a second, and fails before the
tests do - but it is a fast filter, not the proof.

Exit 1 on any advertised parameter the dispatch branch does not forward.
"""
from __future__ import annotations

import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCH = os.path.join(ROOT, "agent_interface", "mcp_server.py")
MANIFEST = os.path.join(ROOT, "manifest", "manifest.json")
DISPATCH_FN = "_dispatch_operation"

# Parameters dispatch deliberately does not forward, each with the reason.
# A name may only sit here if the tool's own OUTPUT tells the caller so -
# silently ignoring a documented parameter is the bug, and moving it into an
# allow-list is not a fix.
DECLARED_NOT_FORWARDED: dict[tuple[str, str], str] = {
    # (tool, param): why
}

# A branch may hand `args` WHOLE to a reader that lives in another module, instead of reading keys one by one.
# find_business does (core/find_business_input.prepare: it must read a place from a string, an object, or a
# top-level city/region, and a kind of business from five spellings - not something a flat run of
# `args.get("x")` can express). The scan cannot follow into a second module, so the module declares the
# names it reads (a constant), and this credits the branch with them ONLY IF the branch really imports and
# calls that reader on `args`. The declaration is checked against the manifest below, so an advertised
# parameter the reader does not know still fails here; whether each one really arrives is the other half
# (tests/unit/test_every_param_actually_arrives.py).
DELEGATED_READERS: dict[str, tuple[str, str, str]] = {
    "find_business": ("core.find_business_input", "prepare", "ARGUMENTS_READ"),
}


def _advertised() -> dict[str, list[str]]:
    with open(MANIFEST, encoding="utf-8") as fh:
        man = json.load(fh)
    out: dict[str, list[str]] = {}
    for op in man.get("operations") or []:
        schema = op.get("input_schema") or op.get("inputSchema") or {}
        out[op["name"]] = list((schema.get("properties") or {}).keys())
    return out


def _dispatch_branches() -> dict[str, ast.AST]:
    """Map tool name -> the AST of the branch that handles it.

    PARSED, NOT GREPPED. A text scan of the branch body reports a parameter
    as forwarded when its name merely appears in a nearby comment or in a
    neighbouring branch, which is how the original version of this check
    would have called the eight real bugs clean.
    """
    with open(DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == DISPATCH_FN:
            fn = node
            break
    if fn is None:
        print(f"check_params_reach_dispatch FAIL -- no {DISPATCH_FN}() in "
              f"{os.path.relpath(DISPATCH, ROOT)}; this guard is reading the "
              f"wrong file and would pass over anything")
        sys.exit(1)

    branches: dict[str, ast.AST] = {}

    def _name_of(test: ast.AST) -> str | None:
        # `name == "find_business"` / `name in ("a", "b")`
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left, op, right = test.left, test.ops[0], test.comparators[0]
            if isinstance(left, ast.Name) and left.id == "name":
                if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
                    return str(right.value)
        return None

    def _walk_if(node: ast.If) -> None:
        got = _name_of(node.test)
        if got:
            branches[got] = ast.Module(body=node.body, type_ignores=[])
        for sub in node.orelse:
            if isinstance(sub, ast.If):
                _walk_if(sub)

    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            _walk_if(node)
    return branches


def _module_functions() -> dict[str, ast.AST]:
    """Every function defined in the dispatch module, by name. Lets a branch that hands `args`
    whole to a helper (send_message -> _build_send_message_request, shared with the channel gate so
    the gate can only act on a request the dispatcher would also accept) be credited with what the
    helper reads, instead of being reported as forwarding nothing."""
    with open(DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _forwarded(branch: ast.AST, helpers: dict[str, ast.AST] | None = None,
               _depth: int = 0) -> tuple[set[str], bool]:
    """Which arg keys the branch reads, and whether it splats everything."""
    keys: set[str] = set()
    splat = False
    for node in ast.walk(branch):
        # f(args, ...) where f is a function of this module that itself reads args[...] / args.get(...)
        if helpers and _depth < 2 and isinstance(node, ast.Call) \
                and isinstance(node.func, ast.Name) and node.func.id in helpers \
                and node.args and isinstance(node.args[0], ast.Name) \
                and node.args[0].id == "args":
            sub_keys, sub_splat = _forwarded(helpers[node.func.id], helpers, _depth + 1)
            keys |= sub_keys
            splat = splat or sub_splat
        # args["x"] and args.get("x")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "args" and isinstance(node.slice, ast.Constant):
            keys.add(str(node.slice.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "args" and node.args \
                and isinstance(node.args[0], ast.Constant):
            keys.add(str(node.args[0].value))
        # **args / **_as_dict(args, ...) forwards the lot
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg is not None:
                    continue
                v = kw.value
                if isinstance(v, ast.Name) and v.id == "args":
                    splat = True
                if isinstance(v, ast.Call) and v.args \
                        and isinstance(v.args[0], ast.Name) and v.args[0].id == "args":
                    splat = True
    return keys, splat


def _delegated_keys(tool: str, branch: ast.AST) -> set[str]:
    """The names a delegated reader declares, if (and only if) the branch imports and calls it on `args`."""
    spec = DELEGATED_READERS.get(tool)
    if not spec:
        return set()
    module, fn, attr = spec
    imported = {a.asname or a.name
                for n in ast.walk(branch) if isinstance(n, ast.ImportFrom) and n.module == module
                for a in n.names if a.name == fn}
    called = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in imported
                 and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "args"
                 for n in ast.walk(branch))
    if not (imported and called):
        return set()
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import importlib
    return set(getattr(importlib.import_module(module), attr))


def main() -> int:
    advertised = _advertised()
    branches = _dispatch_branches()

    if not branches:
        print("check_params_reach_dispatch FAIL -- parsed zero dispatch "
              "branches; the guard is not reading what it thinks it is")
        return 1

    problems: list[str] = []
    checked = 0
    splatted = 0
    helpers = _module_functions()
    for tool, params in sorted(advertised.items()):
        branch = branches.get(tool)
        if branch is None:
            problems.append(
                f"{tool}: advertised in the manifest with no dispatch branch - "
                f"calling it returns method-not-found")
            continue
        keys, splat = _forwarded(branch, helpers)
        keys |= _delegated_keys(tool, branch)
        if splat:
            splatted += 1
            continue
        for p in params:
            checked += 1
            if p in keys:
                continue
            why = DECLARED_NOT_FORWARDED.get((tool, p))
            if why:
                continue
            problems.append(
                f"{tool}.{p}: advertised in the manifest, never read from "
                f"args in the dispatch branch - it reaches the handler as "
                f"None no matter what the caller sends")

    if problems:
        print("check_params_reach_dispatch FAIL")
        for p in problems:
            print("  - " + p)
        print("\nForward it in _dispatch_operation, remove it from the "
              "manifest, or add it to DECLARED_NOT_FORWARDED with the reason "
              "AND a disclosure in the tool's own output.")
        return 1

    print(f"check_params_reach_dispatch OK -- {checked} advertised "
          f"parameter(s) reach their handler across "
          f"{len(advertised) - splatted} field-by-field branch(es); "
          f"{splatted} branch(es) forward everything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
