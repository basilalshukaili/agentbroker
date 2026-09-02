#!/usr/bin/env python3
r"""Refuse syntax that this repository's PRODUCTION Python cannot parse.

WHY THIS EXISTS. On 2026-09-02 four deploys in a row failed with
`update_failed` while every build reported success and every gate was green.
The cause was one line in `web/pages.py`:

    f'<tr{" style=\"color:var(--accent)\"" if name == plan_key else ""}>'

A backslash inside an f-string EXPRESSION is legal in Python 3.12 and a
SyntaxError in 3.11. The laptop runs 3.12, CI pinned `python-version: "3.12"`,
and the Dockerfile runs `python:3.11-slim` - so the file parsed on every
machine that checked it and raised at import on the only machine that matters,
where the container exited 1 with the previous build still serving.

The permanent fix is to run CI on the version the image runs; this checker is
the belt to that braces, because the two can drift apart again the next time
either is bumped.

TWO WAYS THIS CHECKER WAS ITSELF WRONG, both found on its first real run, and
both worth keeping written down because they are the two ways any linter dies:

  1. IT CRIED WOLF. It matched `{...}` with a regex, so in

         f"-d '{{\"email\":\"you@example.com\"}}'"

     it matched from the second brace to the first closing one and found the
     escaped quotes inside. But `{{` and `}}` are how an f-string writes a
     LITERAL brace - there is no expression there at all, and production parses
     that line perfectly well. It failed CI on correct code, and a gate that
     fails correct code is a gate somebody switches off.

  2. IT SILENTLY PASSED. `tokenize` used to emit one STRING token per
     f-string; PEP 701 changed that in 3.12 to FSTRING_START / FSTRING_MIDDLE /
     FSTRING_END. Running on 3.12 - which is what the laptop has - the old
     `tok.type == STRING` test matched NOTHING, so it scanned 243 files, found
     no f-strings whatsoever, and printed CLEAN. A green check that inspects
     nothing is worse than no check, because it is trusted.

So this version reads f-strings on both tokenizations, parses them with a small
state machine that understands doubled braces and nested quotes rather than a
regex, and REFUSES TO REPORT CLEAN unless it can prove on its own known-bad
sample that it can still see what it is looking for.

Exit 0 = clean. Exit 1 = would not parse in production.

    python scripts/check_py311_compatible.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "venv",
             ".venv", "edge", "deploy"}

# A line that must always be flagged, and one that must never be. The checker
# runs itself against these before trusting its own verdict.
_KNOWN_BAD = 'x = f\'<tr{" style=\\"c\\"" if a == b else ""}>\'\n'
_KNOWN_GOOD = 'y = f"-d \'{{\\"email\\":\\"you@example.com\\"}}\' {v}"\n'


def _image_python() -> "tuple[int, int] | None":
    """The (major, minor) the Dockerfile actually runs."""
    try:
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            m = re.search(r"^FROM\s+python:(\d+)\.(\d+)", fh.read(), re.M)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except OSError:
        return None


def _ci_python() -> "str | None":
    p = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    try:
        with open(p, encoding="utf-8") as fh:
            m = re.search(r"python-version:\s*\"?([\d.]+)\"?", fh.read())
        return m.group(1) if m else None
    except OSError:
        return None


def _expression_has_backslash(fstring_text: str) -> bool:
    """True if any {expression} inside this f-string literal contains a `\\`.

    A state machine rather than a regex, because the three things that matter
    here are exactly the three a regex cannot see:

      * `{{` and `}}` are literal braces and open no expression;
      * expressions nest - `f"{d['k'] if x else f(y)}"`;
      * an expression may contain quotes, and a brace inside those quotes is
        not structural.

    Only the expression text is examined. Backslashes in the literal parts of
    an f-string are legal on every version and always have been.
    """
    depth = 0
    quote = ""
    i = 0
    n = len(fstring_text)
    while i < n:
        c = fstring_text[i]
        if depth == 0:
            if c == "{" and i + 1 < n and fstring_text[i + 1] == "{":
                i += 2
                continue
            if c == "}" and i + 1 < n and fstring_text[i + 1] == "}":
                i += 2
                continue
            if c == "{":
                depth = 1
            i += 1
            continue
        # inside an expression
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\":
                return True
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
        elif c == "\\":
            return True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return False


def _fstrings_in(source: str) -> "list[tuple[int, str]]":
    """(line, literal text) for every f-string, on either tokenization.

    3.11 and earlier emit one STRING token per f-string. 3.12+ splits it into
    FSTRING_START / FSTRING_MIDDLE / FSTRING_END, so the literal has to be
    reassembled from the source between the start and end positions.
    """
    out: "list[tuple[int, str]]" = []
    lines = source.splitlines(keepends=True)

    def _slice(start, end) -> str:
        (sr, sc), (er, ec) = start, end
        if sr == er:
            return lines[sr - 1][sc:ec]
        buf = [lines[sr - 1][sc:]]
        buf.extend(lines[sr:er - 1])
        buf.append(lines[er - 1][:ec])
        return "".join(buf)

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return out

    fstart = getattr(tokenize, "FSTRING_START", None)
    fend = getattr(tokenize, "FSTRING_END", None)

    open_stack = []
    for tok in toks:
        if fstart is not None and tok.type == fstart:
            open_stack.append(tok)
            continue
        if fend is not None and tok.type == fend and open_stack:
            start = open_stack.pop()
            out.append((start.start[0], _slice(start.start, tok.end)))
            continue
        if tok.type == tokenize.STRING:
            prefix = tok.string[:3].split('"')[0].split("'")[0].lower()
            if "f" in prefix:
                out.append((tok.start[0], tok.string))
    return out


def _hits(source: str) -> "list[int]":
    return [line for line, text in _fstrings_in(source)
            if _expression_has_backslash(text)]


def _self_test() -> "list[str]":
    """Prove the scanner can still see what it is looking for, on THIS Python.

    This is the guard against failure mode 2 in the module docstring: a
    tokenizer change made the previous version match nothing at all and report
    every file clean. A checker that cannot fail its own known-bad sample has
    no business passing anyone else's code.
    """
    problems = []
    if not _hits(_KNOWN_BAD):
        problems.append(
            "SELF-TEST FAILED: the scanner did not flag its own known-bad "
            "sample, so it cannot see f-string expressions on Python "
            f"{sys.version_info.major}.{sys.version_info.minor} and every "
            "'clean' verdict it gives is meaningless. Do not trust this run.")
    if _hits(_KNOWN_GOOD):
        problems.append(
            "SELF-TEST FAILED: the scanner flagged its own known-GOOD sample "
            "(doubled braces are literal text, not an expression). It would "
            "fail CI on correct code.")
    return problems


def main() -> int:
    problems = _self_test()

    img = _image_python()
    ci = _ci_python()
    if img and ci and not ci.startswith(f"{img[0]}.{img[1]}"):
        problems.append(
            f"CI runs Python {ci} but the production image runs "
            f"{img[0]}.{img[1]} - a syntax difference between them fails ONLY "
            f"in production, with a green build. Pin ci.yml to "
            f"{img[0]}.{img[1]}.")

    scanned = seen = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            scanned += 1
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                continue
            found = _fstrings_in(source)
            seen += len(found)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            for line, text in found:
                if _expression_has_backslash(text):
                    problems.append(
                        f"{rel}:{line}  backslash inside an f-string "
                        f"expression - SyntaxError on Python 3.11, fine on "
                        f"3.12. Hoist it into a variable.\n      "
                        f"{text.strip()[:110]}")

    if problems:
        print(f"check_py311_compatible: FAIL -- {len(problems)} problem(s) "
              f"that production would reject:\n")
        for p in problems:
            print("  " + p)
        print("\nThese do not fail locally or in CI. They fail in the "
              "container, after the build reports success.")
        return 1
    print(f"check_py311_compatible: CLEAN -- {scanned} file(s), {seen} "
          f"f-string(s) inspected; CI Python {ci} matches the image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
