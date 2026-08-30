#!/usr/bin/env python3
"""Registry server.json files must satisfy the registry's own limits locally.

WHY. The MCP registry rejects `description` over 100 characters with a 422.
I rewrote the sanctions listing to advertise our new EU and UK coverage, made
it 166 characters, and found out from a RED CI RUN on the publish job - after
the commit was pushed and deployed.

Every local gate passed, because none of them knew the registry had a limit.
The rule lived only on somebody else's server, so the only way to discover it
was to fail publicly. That is the same shape as everything else this repo
guards against: a constraint that exists, is knowable, and is checked nowhere
we look before shipping.

The limits below are the registry's, mirrored here so the failure happens on
this machine in a second rather than in CI in three minutes.

Usage:
    python scripts/check_registry_limits.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# From the registry's 422 responses. Add a limit here the FIRST time it bites.
LIMITS = {
    "description": 100,
    "name": 200,
}

REQUIRED = ("name", "description", "version")


def _files() -> list[str]:
    out = []
    root = os.path.join(REPO, "server.json")
    if os.path.exists(root):
        out.append(root)
    reg = os.path.join(REPO, "registry")
    if os.path.isdir(reg):
        for dirpath, _d, names in os.walk(reg):
            for n in names:
                if n == "server.json":
                    out.append(os.path.join(dirpath, n))
    return sorted(out)


def main() -> int:
    files = _files()
    if not files:
        print("check_registry_limits: NO server.json FOUND - verifying nothing")
        return 2

    bad = []
    for path in files:
        rel = os.path.relpath(path, REPO)
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as exc:                # noqa: BLE001
            bad.append((rel, f"is not valid JSON: {exc}"))
            continue
        for field in REQUIRED:
            if not str(d.get(field) or "").strip():
                bad.append((rel, f"{field} is missing or empty"))
        for field, cap in LIMITS.items():
            val = str(d.get(field) or "")
            if len(val) > cap:
                bad.append((rel, f"{field} is {len(val)} characters, the "
                                 f"registry rejects anything over {cap} "
                                 f"(HTTP 422)"))

    if bad:
        print(f"check_registry_limits FAILED -- {len(bad)} problem(s) the "
              f"registry would reject:\n")
        for rel, why in bad:
            print(f"  {rel}: {why}")
        print("\nThese are the registry's limits, not ours. Fix them here "
              "rather than finding out from a red publish job.")
        return 1

    print(f"check_registry_limits OK -- {len(files)} manifest(s) within the "
          f"registry's limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
