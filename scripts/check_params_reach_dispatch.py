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


def _forwarded(branch: ast.AST) -> tuple[set[str], bool]:
    """Which arg keys the branch reads, and whether it splats everything."""
    keys: set[str] = set()
    splat = False
    for node in ast.walk(branch):
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
    for tool, params in sorted(advertised.items()):
        branch = branches.get(tool)
        if branch is None:
            problems.append(
                f"{tool}: advertised in the manifest with no dispatch branch - "
                f"calling it returns method-not-found")
            continue
        keys, splat = _forwarded(branch)
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
