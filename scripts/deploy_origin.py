#!/usr/bin/env python3
"""Deploy the Render origin, and prove it actually happened.

    python scripts/deploy_origin.py --check   # is the live origin running HEAD?
    python scripts/deploy_origin.py           # trigger, wait, verify

WHY THIS EXISTS. Render's `autoDeploy` does not reliably fire on push. That is
not a suspicion - it is documented in deploy/registry-submissions/
smithery-sync-2026-08-16.md after it served a stale tool count for days, and it
happened again on 2026-09-07: a push landed on origin/main, and forty minutes
later the newest deploy was still the PREVIOUS commit. Nothing reported it.

The failure mode is the dangerous kind. Nothing errors. The push succeeds, CI is
green, the service is healthy, and the origin quietly keeps serving the old build
- so the natural conclusion, "I shipped it", is wrong in a way that only shows up
when someone tests the live URL, which is exactly what nobody does after a green
push. Both times we found it by accident.

So this makes the trigger a command instead of a memory, and refuses to say
"deployed" without checking. A deploy that reports success without verification
is the same class of bug as the autoDeploy it works around.

NOT INCLUDED ON PURPOSE. This does not push, and it does not touch the Cloudflare
edge. The edge answers initialize/tools/list from snapshots compiled into its
bundle, so after this succeeds the canonical host is STILL serving the old
answers until you run refresh_edge_snapshots.py and redeploy the worker. Doing
that silently from here would hide the second half of the same trap.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "srv-d7oqgn9o3t8c7386as0g")
API = "https://api.render.com/v1"
TERMINAL_BAD = {"build_failed", "update_failed", "canceled", "pre_deploy_failed",
                "deactivated"}


def api_key() -> str:
    key = os.environ.get("RENDER_API_KEY")
    if key:
        return key.strip()
    for env in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "..", ".env")):
        if not os.path.exists(env):
            continue
        with open(env, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("RENDER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("RENDER_API_KEY not found in environment or .env")


def call(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": "Bearer " + api_key(),
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Render API {e.code} on {method} {path}: {e.read().decode()[:300]}")


def local_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()


def latest_deploy() -> dict:
    rows = call(f"/services/{SERVICE_ID}/deploys?limit=1")
    if not rows:
        sys.exit("service has no deploys")
    return rows[0].get("deploy", rows[0])


def commit_of(dep: dict) -> str:
    return ((dep.get("commit") or {}).get("id") or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift between local HEAD and the live deploy")
    ap.add_argument("--timeout", type=int, default=900,
                    help="seconds to wait for the deploy to go live")
    args = ap.parse_args(argv)

    head = local_head()
    dep = latest_deploy()
    live_commit = commit_of(dep)

    print(f"local HEAD    {head[:8]}")
    print(f"latest deploy {dep.get('id')} {dep.get('status')} {live_commit[:8]}")

    if live_commit == head and dep.get("status") == "live":
        print("\nOK: the live origin is running local HEAD.")
        return 0

    if args.check:
        # The whole point: say plainly that a push did not deploy itself.
        print("\nDRIFT: the live origin is NOT running local HEAD.")
        print("Run without --check to deploy. (A push alone does not do this.)")
        return 1

    print("\ntriggering deploy...")
    new = call(f"/services/{SERVICE_ID}/deploys", "POST",
               {"clearCache": "do_not_clear"})
    dep_id = new.get("id")
    print(f"deploy {dep_id} for commit {commit_of(new)[:8]}")

    deadline = time.time() + args.timeout
    status = new.get("status")
    while time.time() < deadline:
        time.sleep(15)
        cur = call(f"/services/{SERVICE_ID}/deploys/{dep_id}")
        cur = cur.get("deploy", cur)
        if cur.get("status") != status:
            status = cur.get("status")
            print(f"  {status}")
        if status == "live":
            break
        if status in TERMINAL_BAD:
            print(f"\nFAILED: deploy ended {status}")
            return 1
    else:
        print(f"\nTIMED OUT after {args.timeout}s, last status {status}")
        return 1

    final = latest_deploy()
    if commit_of(final) != head:
        print(f"\nWARNING: live deploy is {commit_of(final)[:8]}, not local HEAD "
              f"{head[:8]} - someone else deployed, or HEAD is unpushed.")
        return 1

    print("\nDEPLOYED: the origin is live on local HEAD.")
    print("The canonical host still serves the OLD initialize/tools-list until:")
    print("  python scripts/refresh_edge_snapshots.py && cd edge && npx wrangler deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
