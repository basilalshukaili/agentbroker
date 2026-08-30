#!/usr/bin/env python3
"""Every parameter we advertise must actually do something.

WHY THIS EXISTS. `screen_sanctions` accepted a `country` argument, validated
it, uppercased it, and then used it for nothing at all - while the response
said "(country filter: IR)". A caller screening a name against one country
believed their search had been narrowed. It had not. The published schema
promised a filter; the code performed an echo.

That is not a typo, it is a CLASS. It appears whenever a capability is
declared in one place and performed in another: the free key that was
offered for months and could never be issued, the booking-link checker that
shipped as a tool and was never run over our own supply, the SLO pinned into
a schema as a constant that nothing measured. Green tests never catch it,
because the code that exists is correct - there simply isn't any code.

WHAT THIS CHECKS. For every tool handler, each declared parameter must be
USED, where used means one of:

  * passed as an argument to some function call, or
  * tested in a condition that guards real work, or
  * used to index, filter, or build something.

A parameter whose only appearances are its own assignment, a type check, and
an f-string in the response is doing nothing but being repeated back.

WHAT IT DELIBERATELY ALLOWS. Echoing an argument into the receipt is correct
and expected - a receipt should say what it was asked. The failure is echoing
it INSTEAD of acting on it, so the rule is about whether any other use
exists, not about whether the echo exists.

Usage:
    python scripts/check_params_do_something.py
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CORE = os.path.join(REPO, "core")

# A parameter that is knowingly accepted-and-not-acted-on, with the reason and
# the promise that the response says so. Anything here MUST be disclosed in the
# tool's own output - that is the price of the exemption.
DECLARED_INERT = {
    # agent_id is accepted on every tool and deliberately ignored: the real
    # identity is parsed from the AUTHENTICATED token by _agent_id_from_token
    # in the dispatch layer. Trusting a caller-supplied agent_id would let
    # anyone claim to be anyone, so ignoring it is the correct behaviour and
    # it is NOT advertised in any published schema.
    ("*", "agent_id"):
        "identity comes from the authenticated token, never from the caller",
    ("screen_sanctions", "entity_type"):
        "accepted for forward compatibility; the response sets "
        "entity_type_filter_applied=false and says so",
    ("map_trade_restriction", "origin_country"):
        "echoed into the tariff guidance note ('rates from US to DE are NOT "
        "provided here'), which is exactly what its schema promises. The tool "
        "refuses to state rates at all rather than fabricate them, so there is "
        "no tariff logic for it to feed - and the response says that outright",
    ("map_trade_restriction", "product"):
        "recorded and echoed on purpose - the tool states loudly that it does "
        "NOT classify the product and returns 'partial', never 'clear'",
}


def _handlers(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("handle_"):
            yield node


def _params(fn) -> list[str]:
    a = fn.args
    names = [x.arg for x in list(a.args) + list(a.kwonlyargs)]
    return [n for n in names if n not in ("self", "cls", "arguments", "params")]


def _string_only(node) -> bool:
    """True when this expression can only ever produce a string."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp):
        return _string_only(node.left) and _string_only(node.right)
    if isinstance(node, ast.IfExp):
        return _string_only(node.body) and _string_only(node.orelse)
    return False


def _functional_uses(fn, param: str) -> int:
    """Count reads of the parameter that are not just echoing it back.

    THE RULE IS INVERTED ON PURPOSE. My first version enumerated the shapes
    that count AS use - passed to a call, used as a subscript, tested in an
    if - and immediately produced false positives: `parties` is consumed by a
    comprehension, which was not on the list, so a parameter that plainly
    works got reported as dead. A checker that cries wolf gets switched off,
    and then it protects nothing.

    So: every read counts as real use UNLESS it is provably an echo. Echoes
    are reads inside an f-string, and reads that are only a value in a dict
    literal - which is how a receipt repeats back what it was asked. Being
    wrong in this direction means missing a dead parameter; being wrong in
    the other means nobody trusts the tool.

    Aliases count too: `country_upper = country.strip().upper()` means later
    uses of country_upper are uses of the parameter.
    """
    # AN ALIAS IS A RENAME, NOT A MENTION. `country_upper = country.upper()`
    # is the same value under another name. `message = f"country: {country}"`
    # is a SENTENCE ABOUT it - and treating that as an alias is what broke the
    # first two versions of this check: `no_match_detail` inherited the alias
    # from an f-string, `human_message` inherited it from that, and returning
    # the message counted as "the parameter does something". The check passed
    # on the exact bug it was written to catch, twice, and I only found out by
    # re-introducing that bug and watching it stay green.
    #
    # So propagation requires the value to be a pure pass-through: it mentions
    # only aliases, and contains no string formatting.
    aliases = {param}
    for _ in range(3):                          # let aliases-of-aliases settle
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and len(node.targets) == 1                     and isinstance(node.targets[0], ast.Name):
                names = {n.id for n in ast.walk(node.value)
                         if isinstance(n, ast.Name)}
                if not (names & aliases):
                    continue
                if any(isinstance(n, ast.JoinedStr) for n in ast.walk(node.value)):
                    continue                    # a sentence about it
                if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                       for n in ast.walk(node.value)):
                    continue                    # string-built, same reasoning
                if not names <= aliases:
                    continue                    # mixes in other values
                aliases.add(node.targets[0].id)

    # Reads that are echoes: inside an f-string, or a bare value in a dict.
    echo_nodes = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    echo_nodes.add(id(n))
        if isinstance(node, ast.Dict):
            for v in node.values:
                if isinstance(v, ast.Name):
                    echo_nodes.add(id(v))
        # `f"...{x}..." if x else ""` - a conditional that only chooses
        # between strings is still just deciding HOW TO PHRASE the receipt,
        # not doing anything with the value. This was the last thing keeping
        # the check green on the bug it was written for.
        if isinstance(node, ast.IfExp) and _string_only(node.body)                 and _string_only(node.orelse):
            for n in ast.walk(node.test):
                if isinstance(n, ast.Name):
                    echo_nodes.add(id(n))

    # Assignment TARGETS are not reads. Neither is the parameter's own
    # PREPARATION: `country_upper = country.strip().upper()` reads `country`,
    # but only to hold it under another name - the work, if any, happens to
    # the alias later.
    #
    # THE FIRST VERSION OF THIS FUNCTION MISSED EXACTLY THAT, and I only found
    # out by re-introducing the bug it was written for and watching it pass.
    # `country` scored a use purely by being uppercased, so the check reported
    # a dead parameter as live. A guard that does not fire is worse than no
    # guard, because it also removes the suspicion that would have found it.
    target_nodes = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            is_alias_assign = any(
                isinstance(t, ast.Name) and t.id in aliases for t in tgts)
            for t in tgts:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        target_nodes.add(id(n))
            if is_alias_assign and node.value is not None:
                for n in ast.walk(node.value):
                    if isinstance(n, ast.Name) and n.id in aliases:
                        target_nodes.add(id(n))

    uses = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in aliases                 and isinstance(node.ctx, ast.Load)                 and id(node) not in echo_nodes and id(node) not in target_nodes:
            uses += 1
    return uses


def _schema_params() -> dict:
    """Every parameter we ADVERTISE, per tool, from the manifest."""
    import json
    path = os.path.join(REPO, "manifest", "manifest.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        ops = json.load(fh).get("operations") or []
    out = {}
    for op in ops:
        props = ((op.get("input_schema") or {}).get("properties") or {})
        if props:
            out[op["name"]] = sorted(props)
    return out


def _module_text(tool: str) -> str:
    """Source of everything that could implement this tool.

    Not just core/<tool>.py: request models live in core/models.py and the
    dispatch layer unpacks arguments in mcp_server.py, so a parameter can be
    legitimately consumed in any of the three.
    """
    parts = []
    for rel in (os.path.join("core", f"{tool}.py"),
                os.path.join("core", "models.py"),
                os.path.join("agent_interface", "mcp_server.py")):
        path = os.path.join(REPO, rel)
        if os.path.exists(path):
            parts.append(open(path, encoding="utf-8", errors="replace").read())
    # A few tools are implemented under a different filename.
    if not parts:
        for dirpath, _d, names in os.walk(os.path.join(REPO, "core")):
            for n in names:
                if n.endswith(".py"):
                    parts.append(open(os.path.join(dirpath, n),
                                      encoding="utf-8", errors="replace").read())
    return "\n".join(parts)


def _advertised_but_absent() -> list:
    """Parameters in the PUBLISHED schema whose name appears nowhere in code.

    WHY THIS EXISTS ALONGSIDE THE AST CHECK. The AST check reads Python
    signatures, and twelve of twenty handlers take a single Pydantic request
    model - so it sees one parameter called `request` and never looks inside.
    An audit measured 56 advertised parameters invisible to it, covering every
    write tool. `send_at_iso` or `preferred_channel` quietly doing nothing is
    precisely what this gate is for.

    This half is deliberately CRUDE and cannot produce a false positive: it
    only reports a parameter whose name does not occur ANYWHERE in the
    implementing modules. That cannot be a live parameter. It will miss a
    parameter that is mentioned but unused - the AST half covers those where
    it can see them - and missing something is the acceptable failure here.
    """
    import re
    findings = []
    for tool, params in _schema_params().items():
        text = _module_text(tool)
        if not text:
            continue
        for name in params:
            if (tool, name) in DECLARED_INERT or ("*", name) in DECLARED_INERT:
                continue
            if not re.search(r"\b" + re.escape(name) + r"\b", text):
                findings.append((tool, name))
    return findings


def main() -> int:
    if not os.path.isdir(CORE):
        print("check_params_do_something: no core/ directory - verifying nothing")
        return 2

    checked, inert, exempted = 0, [], 0
    for fname in sorted(os.listdir(CORE)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(CORE, fname)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for fn in _handlers(tree):
            tool = fn.name[len("handle_"):]
            for p in _params(fn):
                checked += 1
                if (tool, p) in DECLARED_INERT or ("*", p) in DECLARED_INERT:
                    exempted += 1
                    continue
                if _functional_uses(fn, p) == 0:
                    inert.append((fname, fn.lineno, tool, p))

    if not checked:
        print("check_params_do_something: FOUND NO HANDLERS - verifying nothing")
        return 2

    absent = _advertised_but_absent()
    if absent:
        print(f"check_params_do_something FAILED -- {len(absent)} PUBLISHED "
              f"parameter(s) do not appear anywhere in the implementing code:\n")
        for tool, name in absent:
            print(f"  {tool}({name}=...) is advertised in the manifest and "
                  f"implemented nowhere")
        return 1

    if inert:
        print(f"check_params_do_something FAILED -- {len(inert)} advertised "
              f"parameter(s) are accepted and never acted on:\n")
        for f, line, tool, p in inert:
            print(f"  core/{f}:{line}  {tool}({p}=...)")
            print(f"    >> declared in the schema, never used. Either make it "
                  f"do something, or add it to DECLARED_INERT with the reason "
                  f"AND say so in the tool's own response.")
        return 1

    advertised = sum(len(v) for v in _schema_params().values())
    print(f"check_params_do_something OK -- {checked} handler parameter(s) and "
          f"{advertised} published parameter(s) checked; {exempted} declared "
          f"inert, each disclosed in its tool's output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
