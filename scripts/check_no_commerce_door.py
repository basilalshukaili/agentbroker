#!/usr/bin/env python3
"""Gate: nothing the ChatGPT door says may sell, price or link to credits.

    python scripts/check_no_commerce_door.py

OpenAI does not let a listed plugin sell, price or promote credits (docs/directory-kit-2026-10/
ISSUES-BEFORE-SUBMITTING.md section 13), and it RE-SCANS a listed plugin daily: a tool description changed later to
mention a price is held until it passes, and can get an approved plugin removed. Pricing text is legitimate on the
other doors, so this is a gate on ONE door, /mcp/chatgpt, and on everything our own code can say on it:

  * initialize and server/discover;  * tools/list (names, titles, descriptions, schemas);
  * resources/list, prompts/list;    * the refusal for a tool the door does not have;
  * the refusal of an x402 attachment;  * the over-ceiling answer;
  * every result shape, projected from the captured real receipts in tests/fixtures/chatgpt_door/;
  * every string literal in the three handlers that feed the door (a new "free" or "credits" in one of them would
    reach ChatGPT).

Third-party text (registry and list entries, the caller's own words) is never scanned: a company may be called
"Credit Suisse AG" and a list entry "FREE ZONE TRADING LLC".

THE GATE PROVES IT CAN FAIL: it runs the same scan over the Claude-facing door's tools/list and handshake, which DO
carry pricing, and refuses to pass unless that scan finds something. A gate that prints CLEAN over text it cannot see is
how this repo's guards have failed before.

Exit 0 = clean (and the self-check found the control dirty), 1 = a surface carries commerce wording, 2 = the gate itself
is broken.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ENVIRONMENT", "development")

from agent_interface import no_commerce, profiles  # noqa: E402
from agent_interface.mcp_server import get_full_manifest, handle_mcp_request  # noqa: E402

DOOR = "chatgpt"
CONTROL = "sanctions-screening"
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "chatgpt_door")
HANDLERS = ("core/verify_company_record.py", "core/map_trade_restriction.py", "core/screen_sanctions.py")
# string literals in the handlers that the door rewords or never returns (the cost block); see no_commerce._PHRASES
ALLOWED_LITERALS = {
    "USD", "free",
    ". The company may not be a legal entity registered with these free registries.",
    " per call so one request cannot occupy the service. Split the list across calls - each call screens every party "
    "it is given, completely. Nothing was screened on this call and nothing was charged.",
}


def call(method: str, params: dict | None = None, profile: str = DOOR) -> dict:
    return asyncio.run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers={}, profile=profile))


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def hits(obj) -> list:
    out = []
    for s in strings(obj):
        for m in no_commerce.FORBIDDEN_RE.finditer(s):
            out.append(f"{m.group(0)!r} in ...{s[max(0, m.start() - 35):m.end() + 35]!r}")
    return out


def door_surfaces() -> dict:
    s: dict = {}
    s["initialize"] = call("initialize", {})
    s["server/discover"] = call("server/discover", {})
    s["tools/list"] = call("tools/list")
    s["resources/list"] = call("resources/list")
    s["prompts/list"] = call("prompts/list")
    s["resources/read manifest"] = call("resources/read", {"uri": "agent-broker://manifest"})
    s["prompts/get cost_estimation"] = call("prompts/get", {"name": "cost_estimation"})
    s["refusal of a tool the door lacks"] = call("tools/call", {"name": "preview_cost", "arguments": {}})
    s["x402 attachment refused"] = call(
        "tools/call", {"name": "screen_sanctions", "arguments": {"name": "x"}, "_meta": {"x402/payment": "x"}})
    # the over-ceiling answer, without running a tool
    saved = os.environ.get("CHATGPT_DOOR_DAILY_CEILING")
    os.environ["CHATGPT_DOOR_DAILY_CEILING"] = "1"
    try:
        no_commerce.reset_ceiling_for_tests()
        no_commerce.consume_ceiling("192.0.2.1")
        over = no_commerce.consume_ceiling("192.0.2.1")
        s["over-ceiling answer"] = over
    finally:
        if saved is None:
            os.environ.pop("CHATGPT_DOOR_DAILY_CEILING", None)
        else:
            os.environ["CHATGPT_DOOR_DAILY_CEILING"] = saved
        no_commerce.reset_ceiling_for_tests()
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".json"):
            with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
                receipt = json.load(fh)["receipt"]
            s[f"result shape: {name}"] = no_commerce.tool_result(receipt)
    return s


def handler_literals() -> list:
    out = []
    for rel in HANDLERS:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                    docs.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if no_commerce.FORBIDDEN_RE.search(node.value) and node.value not in ALLOWED_LITERALS:
                    out.append(f"{rel}:{node.lineno}: {node.value[:80]!r}")
    return out


def main() -> int:
    if DOOR not in profiles.PROFILES or not profiles.is_no_commerce(DOOR):
        print(f"FAIL  the {DOOR} door is not defined as a no-commerce profile")
        return 2
    failures = []

    for label, surface in door_surfaces().items():
        found = hits(surface)
        print(f"{'FAIL' if found else 'PASS'}  {label}")
        for f in found[:5]:
            print("        " + f)
        failures += [f"{label}: {f}" for f in found]

    others = {o["name"] for o in get_full_manifest()["operations"]} - set(profiles.tools_for(DOOR))
    text = json.dumps([call("tools/list"), call("initialize", {})])
    leaked = sorted(n for n in others if re.search(rf"\b{re.escape(n)}\b", text))
    print(f"{'FAIL' if leaked else 'PASS'}  no tool the door lacks is named in tools/list or initialize")
    failures += [f"names a tool the door lacks: {n}" for n in leaked]

    lits = handler_literals()
    print(f"{'FAIL' if lits else 'PASS'}  handler prose carries no commerce wording beyond the two sentences the door rewords")
    for f in lits[:5]:
        print("        " + f)
    failures += lits

    # The gate must be able to fail: the Claude-facing door carries pricing, so the same scan must find it.
    control = hits([call("tools/list", profile=CONTROL), call("initialize", {}, profile=CONTROL)])
    if not control:
        print(f"FAIL  self-check: the scan found nothing in the {CONTROL} door, which carries pricing; the gate is blind")
        return 2
    print(f"PASS  self-check: the same scan finds {len(control)} hits on the {CONTROL} door (it is meant to)")

    if failures:
        print(f"\n{len(failures)} problem(s): the ChatGPT door must not sell, price or link to credits.")
        return 1
    print("\nclean: nothing the ChatGPT door says sells, prices or links to credits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
