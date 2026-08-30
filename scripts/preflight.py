#!/usr/bin/env python3
"""Run EVERYTHING CI runs, before committing. One command, no judgement calls.

WHY THIS EXISTS. I pushed a commit having run pytest, check_gates and
check_pricing - and skipped check_encoding, because the change was to
check_pricing and encoding "wasn't relevant". Eight box-drawing characters in a
comment turned CI red. That decision cannot be made correctly from inside the
change: if I knew what my diff touched, I would not need the gates. So this
script removes the decision.

THE FIRST VERSION OF THIS FILE WAS A CAUTIONARY TALE ABOUT ITS OWN THESIS.

It scraped `run:` lines with a regex. Against the real workflow it found all six
steps, so it looked correct. An external reviewer tested it against a workflow
using other perfectly ordinary shapes and it found TWO of six:

    - run: python gate.py                 MISSED  (no `name:`, so `- ` precedes `run:`)
    - run: |\n    python gate.py          MISSED  (same, block form)
    run: PYTHONPATH=. python gate.py      MISSED  (env prefix defeats startswith("python"))
    run: ./scripts/run_all.sh             MISSED  (shell wrapper that runs python)
    working-directory: edge / run: ...    FOUND but executed from the wrong directory

...and then printed "all green - safe to push". A verifier that quietly narrows
is worse than none, because it is the reason you stopped looking.

So it now parses the workflow as YAML instead of guessing at its text, honours
`working-directory`, and - the part that matters most - REPORTS EVERY STEP IT
CANNOT RUN rather than silently dropping it. "I ran 6 of 8, here are the 2 I
skipped and why" is a true statement. "All green" was not.

Usage:
    python scripts/preflight.py          # run everything, exit 1 on any failure
    python scripts/preflight.py --list   # show what it would run
"""
from __future__ import annotations

import os
import shutil
import tempfile
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORKFLOW = os.path.join(REPO, ".github", "workflows", "ci.yml")

# Steps that genuinely cannot run here. Each is matched against the command and
# reported by name, so a skip is a visible decision rather than an omission.
CANNOT_RUN_LOCALLY = [
    ("pip install", "dependency install - your working env is already set up"),
    ("sleep ", "post-deploy wait - there is no deploy here"),
    ("curl ", "post-deploy smoke - runs against production after shipping"),
]


def _steps(job: dict) -> list[dict]:
    out = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict) or "run" not in step:
            continue
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for line in run.splitlines():
            cmd = line.strip()
            if not cmd or cmd.startswith("#"):
                continue
            out.append({
                "cmd": cmd,
                "cwd": step.get("working-directory") or ".",
                "job_optional": bool(step.get("continue-on-error")),
            })
    return out


def collect() -> tuple[list[dict], list[str]]:
    """(runnable steps, reasons for everything skipped)."""
    try:
        import yaml
    except ImportError:
        return [], ["PyYAML is not installed - cannot read the workflow. "
                    "`pip install pyyaml`, or run the gates by hand."]
    try:
        with open(WORKFLOW, encoding="utf-8") as fh:
            wf = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001
        return [], [f"cannot parse {WORKFLOW}: {exc}"]

    runnable, skipped = [], []
    for job_name, job in (wf.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        # `continue-on-error` steps report but do not gate; a whole job of them
        # (the post-deploy smoke) is not something a pre-push check should run.
        for st in _steps(job):
            cmd = st["cmd"]
            why = next((w for frag, w in CANNOT_RUN_LOCALLY if frag in cmd), None)
            if why:
                skipped.append(f"{cmd[:56]} ({why})")
            elif st["job_optional"]:
                skipped.append(f"{cmd[:56]} (continue-on-error in job '{job_name}')")
            else:
                st["job"] = job_name
                runnable.append(st)
    return runnable, skipped


def _ci_checkout() -> str:
    """Run the gates against a REPO-ONLY COPY, the way CI sees the code.

    WHY. I have pushed a red commit three times, and the last one was caused by
    this exact gap: a gate passed locally and failed in CI because it reads
    files that live in the surrounding workspace - the marketing site, the
    public skill repo, the distribution feed - which exist on this machine and
    do NOT exist in a repo-only checkout. Preflight ran in the workspace, saw
    them, and said "safe to push".

    A pre-push check that runs in a more generous environment than the thing it
    is predicting is not a check; it is a second opinion from someone with more
    information. So the gates run in a temp copy containing ONLY the repo.

    `--no-sandbox` runs them in place, for when a gate genuinely needs the
    workspace and you want to see that result too.
    """
    tmp = tempfile.mkdtemp(prefix="preflight-")
    dst = os.path.join(tmp, os.path.basename(REPO))
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(
        ".git", "node_modules", "__pycache__", "*.pyc", ".pytest_cache",
        ".venv", "dist", "distX"))
    print(f"  (gates run against a repo-only copy, as CI sees it; "
          f"--no-sandbox to run in place)")
    return dst


def main(argv: list[str]) -> int:
    runnable, skipped = collect()

    if not runnable:
        print("FOUND NO RUNNABLE STEPS in ci.yml - this is verifying nothing.")
        for s in skipped:
            print("  skipped:", s)
        print("That is a failure, not a pass.")
        return 2

    for s in skipped:
        print(f"  skip  {s}")

    if "--list" in argv:
        print(f"\nwould run {len(runnable)} step(s):")
        for st in runnable:
            loc = "" if st["cwd"] == "." else f"   [in {st['cwd']}]"
            print(f"    {st['cmd']}{loc}")
        return 0

    root = REPO if "--no-sandbox" in argv else _ci_checkout()
    print(f"\nrunning {len(runnable)} step(s) from ci.yml\n")
    failed = []
    for st in runnable:
        cwd = os.path.join(root, st["cwd"]) if st["cwd"] != "." else root
        p = subprocess.run(st["cmd"], cwd=cwd, shell=True, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        tail = ((p.stdout or p.stderr).strip().splitlines() or [""])[-1]
        ok = p.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'} {st['cmd'][:44]:44} {tail[:50]}")
        if not ok:
            failed.append((st["cmd"], (p.stdout or "") + (p.stderr or "")))

    if failed:
        print(f"\n{len(failed)} step(s) FAILED - do not push:\n")
        for cmd, out in failed:
            print(f"--- {cmd}")
            print("\n".join(out.strip().splitlines()[-14:]))
            print()
        return 1

    # Deliberately NOT the words "all green". This ran what it could run, and
    # says so with a number the reader can compare against the workflow.
    print(f"\n{len(runnable)} gating step(s) passed, {len(skipped)} not runnable "
          f"here (listed above). Safe to push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
