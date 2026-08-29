#!/usr/bin/env python3
"""Fail the build when a module is complete, tested, and unreachable.

WHY THIS IS MECHANICAL AND NOT A CHECKLIST.

On 2026-08-29 this codebase produced NINE instances of one bug class in a single
day: code that is finished, well-tested, and never invoked. A validator with no
caller. Capability endpoints with 28 passing tests and no HTTP route. A
scheduled task running perfectly against the wrong directory. A test skipped for
a missing dependency and counted in the passing total. A CI pipeline in a folder
GitHub does not read.

The ninth was found by an external reviewer INSIDE THE GUARD WRITTEN THAT
MORNING TO CATCH THE CLASS, thirty-eight lines below a comment warning about the
exact mistake, on the day the whole company was hunting it.

That is the finding that matters: vigilance does not work here, even at maximum
attention. The reason is structural. A unit test imports the module directly, so
THE TEST IS THE CALLER THE CODE OTHERWISE LACKS - and the one wire that is
missing is the only thing a unit test cannot see. Writing module-plus-test is
one satisfying self-contained motion; wiring it to a route, a table, a task
argument and a rewrite is four unglamorous edits in files that may not even be
open. The complete artefact FEELS finished, so it ships.

So reachability has to be a property the machine checks, not a thing a person
remembers.

WHAT IT DOES. Builds an import/reference graph over the package and reports any
module whose public surface is referenced ONLY by tests, or not at all.

WHAT IT DELIBERATELY DOES NOT DO. It does not try to be clever about dynamic
dispatch, which this codebase uses heavily (the MCP dispatcher looks up handlers
by name, FastAPI routes are decorators, Celery tasks are registered by string).
A tool that guesses at those produces false alarms, and a checker that cries
wolf gets switched off - which is how we got here. Everything it cannot see is
declared in ALLOWED below, WITH A REASON, so the exceptions are a readable list
rather than an absent check.

Usage:
    python scripts/check_reachable.py          # exit 1 on an orphan
    python scripts/check_reachable.py --list   # show the graph it built
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Packages whose modules must be reachable from production code.
WATCHED = ("core", "compliance", "billing", "reliability", "agent_interface",
           "supply", "storage", "telemetry", "optimizer", "feedback",
           "onboarding", "channels")

SKIP_DIRS = {".git", "__pycache__", "node_modules", "tests", "edge", ".ci",
             ".github", "venv", ".venv", "manifest", "docs", "scripts"}

# Modules that ARE reached, by a mechanism the import graph cannot see.
# Every entry needs a reason. An entry without one is a bug being hidden.
ALLOWED: dict[str, str] = {
    "reliability.async_runner":
        "Celery registers its tasks by string name at import; main.py imports "
        "the module for its side effect.",
    "agent_interface.profiles":
        "imported by mcp_server and main for the capability doors",
}


def _module_name(path: str) -> str:
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def _python_files() -> list[str]:
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    return out


def _is_watched(mod: str) -> bool:
    return mod.split(".")[0] in WATCHED


def build_graph():
    """Return (watched_modules, referenced_by_production)."""
    watched, referenced = set(), set()
    for path in _python_files():
        mod = _module_name(path)
        if _is_watched(mod) and not mod.endswith("__init__"):
            watched.add(mod)

    # Only PRODUCTION files vote. A reference from tests/ is exactly the
    # false signal this tool exists to ignore.
    for path in _python_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel.startswith("tests/") or "/tests/" in rel:
            continue
        src = open(path, encoding="utf-8", errors="replace").read()
        me = _module_name(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name != me:
                        referenced.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module != me:
                    referenced.add(node.module)
                    for a in node.names:
                        referenced.add(f"{node.module}.{a.name}")
    return watched, referenced


BASELINE = os.path.join(ROOT, "scripts", "reachability-baseline.txt")


def _baseline() -> set[str]:
    """Orphans that already existed when this gate was introduced.

    A NEW LINT ON AN OLD CODEBASE MUST NOT FAIL ON DAY ONE. There were eleven
    unreachable modules the day this was written; failing the build on all of
    them would have meant either deleting eleven things in a hurry or switching
    the gate off - and a switched-off gate is how the ninth bug happened.

    So the baseline is a frozen list of known debt. The gate's job is to stop
    the number GROWING, which is the only thing that prevents the tenth. It
    also fails when a baselined module becomes reachable or disappears, so the
    list shrinks as things are fixed and can never quietly grant permission to
    a new orphan that happens to share a name.
    """
    if not os.path.exists(BASELINE):
        return set()
    out = set()
    with open(BASELINE, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line)
    return out


def main(argv: list[str]) -> int:
    watched, referenced = build_graph()

    if "--list" in argv:
        print(f"{len(watched)} watched modules, {len(referenced)} references")

    orphans = []
    for mod in sorted(watched):
        if mod in referenced or mod in ALLOWED:
            continue
        # EXACT MATCH ONLY, and this was the flaw that made the gate useless.
        #
        # It used to also accept `mod.startswith(r + ".")` - "a parent package
        # being imported counts". But `from core import screen_sanctions`
        # records the parent "core" as referenced, so EVERY module under core/
        # was then treated as reachable. I proved it by dropping a brand-new
        # orphan into core/ and watching the gate report CLEAN.
        #
        # Exact matching is sufficient because all three import forms already
        # record the full dotted path: `import a.b`, `from a.b import c`, and
        # `from a import b` (which records both "a" and "a.b"). A checker that
        # cannot fail is worse than no checker - that is the entire lesson this
        # file exists to encode, and it nearly shipped inside the encoding.
        if any(r.startswith(mod + ".") for r in referenced):
            continue
        orphans.append(mod)

    # An ALLOWED entry for a module that no longer exists is stale permission -
    # the next orphan with that name would be waved through silently.
    stale = [m for m in ALLOWED if m not in watched]
    if stale:
        print("STALE EXEMPTIONS (module gone, permission remains): "
              + ", ".join(stale))

    known = _baseline()
    new_orphans = [m for m in orphans if m not in known]
    fixed = sorted(known - set(orphans))

    if new_orphans:
        print(f"check_reachable: {len(new_orphans)} NEW UNREACHABLE MODULE(S)")
        for m in new_orphans:
            print(f"  {m} - imported by no production file. This is the bug "
                  f"class that hit nine times on 2026-08-29: complete, tested, "
                  f"and never invoked. Wire it to something that runs, delete "
                  f"it, or add it to ALLOWED with the mechanism that reaches it.")
        return 1

    if fixed:
        # Shrinking the baseline is the point. Forcing the edit keeps the file
        # honest - a stale entry is standing permission for a future orphan.
        print(f"check_reachable: {len(fixed)} baselined module(s) are now "
              f"reachable or gone - remove them from "
              f"scripts/reachability-baseline.txt:")
        for m in fixed:
            print(f"  {m}")
        return 1

    if stale:
        return 1

    if orphans:
        print(f"check_reachable: OK -- {len(orphans)} known-unreachable "
              f"module(s), none new. {len(watched)} modules watched.")
    else:
        print(f"check_reachable: CLEAN -- {len(watched)} modules, all reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
