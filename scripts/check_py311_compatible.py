#!/usr/bin/env python3
"""Refuse syntax that this repository's PRODUCTION Python cannot parse.

WHY THIS EXISTS. On 2026-09-02 four deploys in a row failed with
`update_failed` while every build reported success and every gate was green.
The cause was one line in `web/pages.py`:

    f'<tr{" style=\\"color:var(--accent)\\"" if name == plan_key else ""}>'

A backslash inside an f-string expression is legal in Python 3.12 and a
SyntaxError in 3.11. The laptop runs 3.12, CI pinned `python-version: "3.12"`,
and the Dockerfile runs `python:3.11-slim` - so the file parsed on every
machine that checked it and raised at import on the only machine that matters,
where the container exited 1 with the previous build still serving.

The permanent fix is to run CI on the version the image runs; this checker is
the belt to that braces, because the two can drift apart again the next time
either is bumped. It compares them and fails if they disagree, then scans for
the constructs that are known to differ.

Exit 0 = clean. Exit 1 = would not parse in production.

    python scripts/check_py311_compatible.py
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "venv",
             ".venv", "edge", "deploy"}


def _image_python() -> tuple[int, int] | None:
    """The (major, minor) the Dockerfile actually runs."""
    try:
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            m = re.search(r"^FROM\s+python:(\d+)\.(\d+)", fh.read(), re.M)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except OSError:
        return None


def _ci_python() -> str | None:
    p = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    try:
        with open(p, encoding="utf-8") as fh:
            m = re.search(r"python-version:\s*\"?([\d.]+)\"?", fh.read())
        return m.group(1) if m else None
    except OSError:
        return None


def _fstring_backslash_hits(path: str) -> list[tuple[int, str]]:
    """Lines where an f-string EXPRESSION contains a backslash.

    Tokenising rather than regexing the source: a regex over raw lines cannot
    tell an f-string from a normal one, and this check has to be trustworthy
    or it will be switched off the first time it cries wolf.
    """
    hits: list[tuple[int, str]] = []
    try:
        with open(path, "rb") as fh:
            toks = list(tokenize.tokenize(fh.readline))
    except (tokenize.TokenError, SyntaxError, OSError):
        return hits
    for tok in toks:
        if tok.type != tokenize.STRING:
            continue
        prefix = tok.string[:3].lower()
        if "f" not in prefix.split('"')[0].split("'")[0]:
            continue
        # only the {...} parts matter; a backslash in the literal text is fine
        for expr in re.findall(r"\{([^{}]*)\}", tok.string):
            if "\\" in expr:
                hits.append((tok.start[0], tok.line.rstrip()[:120]))
                break
    return hits


def main() -> int:
    img = _image_python()
    ci = _ci_python()
    problems: list[str] = []

    if img and ci and not ci.startswith(f"{img[0]}.{img[1]}"):
        problems.append(
            f"CI runs Python {ci} but the production image runs "
            f"{img[0]}.{img[1]} - a syntax difference between them fails ONLY "
            f"in production, with a green build. Pin ci.yml to "
            f"{img[0]}.{img[1]}.")

    scanned = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            scanned += 1
            for line, text in _fstring_backslash_hits(path):
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                problems.append(
                    f"{rel}:{line}  backslash inside an f-string expression - "
                    f"SyntaxError on Python 3.11, fine on 3.12. Hoist it into "
                    f"a variable.\n      {text}")

    if problems:
        print(f"check_py311_compatible: FAIL -- {len(problems)} problem(s) that "
              f"production would reject:\n")
        for p in problems:
            print("  " + p)
        print("\nThese do not fail locally or in CI. They fail in the container, "
              "after the build reports success.")
        return 1
    print(f"check_py311_compatible: CLEAN -- {scanned} file(s); CI Python {ci} "
          f"matches the image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
