#!/usr/bin/env python3
"""Run EVERYTHING CI runs, before committing. One command, no judgement calls.

WHY THIS EXISTS. On 2026-08-29 I pushed a commit having run pytest,
check_gates and check_pricing - and skipped check_encoding, because the change
was to check_pricing and encoding "wasn't relevant". Eight box-drawing
characters in a comment separator turned CI red.

The gate was right. The mistake was deciding which gates were relevant, which
is a decision that cannot be made correctly from inside the change: if I knew
what my diff touched, I would not need the gates. So this script removes the
decision. It runs the same list, in the same order, as `.github/workflows/ci.yml`.

THE LIST IS DERIVED FROM THE WORKFLOW, NOT COPIED FROM IT. A hand-maintained
second copy of CI's steps drifts, and the drift is silent and in the worst
direction: this passes, CI fails, and the script that was supposed to prevent
that is the reason you trusted the push. The workflow file is parsed for its
`run:` lines instead, so a stage added to CI appears here without anyone
remembering.

Usage:
    python scripts/preflight.py          # run everything, exit 1 on any failure
    python scripts/preflight.py --list   # show what it would run
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW = os.path.join(REPO, ".github", "workflows", "ci.yml")

# Steps that cannot run locally, keyed by a fragment of their command. Each one
# is named so that skipping it is a visible decision rather than an omission.
CANNOT_RUN_LOCALLY = {
    "pip install": "dependency install - your working env is already set up",
    "sleep": "post-deploy wait - there is no deploy here",
    "curl": "post-deploy smoke - runs against production after shipping",
}


def steps_from_workflow() -> list[str]:
    """Every `python ...` command CI runs, in workflow order."""
    try:
        with open(WORKFLOW, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"cannot read {WORKFLOW}: {exc}")
        return []
    # BOTH FORMS, and the second one is why this is not a one-liner.
    #
    #   run: python check_gates.py          <- single line
    #   run: |                              <- block
    #     python -m pytest tests/ -q
    #
    # The first version of this parser only matched the single-line form. It
    # happened to find all six steps, so it looked correct - but the moment
    # someone wrote a step as a block (which ci.yml already does elsewhere)
    # this would have silently stopped checking it, while still printing "all
    # green - safe to push". A verifier that quietly narrows is worse than no
    # verifier, because it is the reason you stopped looking.
    found: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)run:\s*(.*)$", line)
        if not m:
            continue
        indent, rest = m.group(1), m.group(2).strip()
        if rest and rest not in ("|", ">", "|-", ">-"):
            if rest.startswith("python"):
                found.append(rest)
            continue
        # Block scalar: take every deeper-indented line until the block ends.
        for body in lines[i + 1:]:
            if body.strip() and not body.startswith(indent + " "):
                break
            cmd = body.strip()
            if cmd.startswith("python"):
                found.append(cmd)
    # Order-preserving dedupe: the same command in two jobs is one check.
    return list(dict.fromkeys(found))


def main(argv: list[str]) -> int:
    steps = steps_from_workflow()
    if not steps:
        print("FOUND NO STEPS in ci.yml - this script is not verifying anything.")
        print("That is a failure, not a pass: fix the parser or the workflow.")
        return 2

    runnable = []
    for cmd in steps:
        skip = next((why for frag, why in CANNOT_RUN_LOCALLY.items() if frag in cmd), None)
        if skip:
            print(f"  skip  {cmd[:58]:58} ({skip})")
        else:
            runnable.append(cmd)

    if "--list" in argv:
        print(f"\nwould run {len(runnable)} step(s):")
        for c in runnable:
            print("   ", c)
        return 0

    print(f"\nrunning {len(runnable)} step(s) from ci.yml\n")
    failed = []
    for cmd in runnable:
        p = subprocess.run(cmd, cwd=REPO, shell=True, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        tail = ((p.stdout or p.stderr).strip().splitlines() or [""])[-1]
        ok = p.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'} {cmd[:44]:44} {tail[:52]}")
        if not ok:
            failed.append((cmd, (p.stdout or "") + (p.stderr or "")))

    if failed:
        print(f"\n{len(failed)} step(s) FAILED - do not push:\n")
        for cmd, out in failed:
            print(f"--- {cmd}")
            print("\n".join(out.strip().splitlines()[-14:]))
            print()
        return 1

    print("\nall green - safe to push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
