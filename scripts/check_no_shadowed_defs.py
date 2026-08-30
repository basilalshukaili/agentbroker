#!/usr/bin/env python3
"""No module may define the same name twice at top level.

core/screen_sanctions.py defined `_fetch_ofac_sdn_csv` and
`_fetch_ofac_alt_csv` TWICE each, byte-identical, a hundred lines apart. It
was harmless only by luck: Python binds the last definition, and the copies
happened to agree.

The danger is the edit that comes next. Someone fixes a bug in the first copy,
every test passes because the tests exercise the module (and therefore the
SECOND copy), and the fix does nothing. That is the same shape as the
producer-with-no-caller class this repo keeps hitting - working code that
nothing reaches - except the unreachable version is the one you just edited.

Nothing flagged it: it is not an unreachable module, not an unused import, not
a failing test. A duplicate top-level name is trivially detectable from the
AST, so it is checked here.

Exit 1 on any module that binds a top-level function or class name twice.
"""
from __future__ import annotations

import ast
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".pytest_cache", "build", "dist"}

# A redefinition guarded by try/except ImportError or `if TYPE_CHECKING` is a
# fallback, not a duplicate. Those live inside an If/Try, so only module-level
# bare definitions are counted - see _top_level below.


def _top_level(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            yield node


def main() -> int:
    problems = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
            except SyntaxError:
                continue
            scanned += 1
            first: dict[str, int] = {}
            counts: collections.Counter = collections.Counter()
            for node in _top_level(tree):
                counts[node.name] += 1
                first.setdefault(node.name, node.lineno)
            for name, n in counts.items():
                if n > 1:
                    lines = [node.lineno for node in _top_level(tree)
                             if node.name == name]
                    problems.append(
                        f"{os.path.relpath(path, ROOT)}: {name} defined "
                        f"{n} times (lines {lines}) - only the last one is "
                        f"bound, so an edit to any other is dead")

    if problems:
        print("check_no_shadowed_defs FAIL")
        for p in problems:
            print("  - " + p)
        print("\nDelete the shadowed copies, or rename them if they were "
              "meant to be different functions.")
        return 1

    print(f"check_no_shadowed_defs OK -- {scanned} module(s), no top-level "
          f"name defined twice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
