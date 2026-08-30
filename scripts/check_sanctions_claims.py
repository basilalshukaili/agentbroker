#!/usr/bin/env python3
"""We must never advertise a sanctions list we do not screen.

WHY THIS EXISTS. `screen_sanctions` screens OFAC, EU and UK. The UN
consolidated list is deliberately absent - it is just as easy to fetch, but it
carries NO open licence and no commercial carve-out, unlike OFAC (US
Government, public domain), the EU consolidated list (European Commission
open-data) and the UK Sanctions List (FCDO, OGL v3.0).

Five published surfaces said "OFAC/EU/UN/UK" anyway - the MCP tool description
that every agent reads in tools/list, its edge snapshot, both llms.txt files,
and the install doc. The string was written when OpenSanctions (which does
carry UN) was our only route, and it stayed put after the code changed.

That is the same defect as an OVERCLAIM in the other direction, found in the
same hour: an unavailability notice still saying "EU/UK NOT screened on this
call" after EU and UK moved to our own index. A compliance tool that misstates
its own coverage is worse than one with less coverage - a customer cannot
correct for a number they were told wrongly.

So coverage is asserted in ONE place, here, and checked against what we print.

Usage:
    python scripts/check_sanctions_claims.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The lists we actually screen. Changing this is a claim about compliance
# coverage: change the screening code first, then this, then run the check.
SCREENED = {"OFAC", "EU", "UK"}

# Lists we could screen and deliberately do not, with the reason.
EXCLUDED = {
    "UN": "no open licence and no commercial carve-out on the UN consolidated list",
}

# Any run of list names joined by / or , inside a coverage claim.
CLAIM = re.compile(
    r"(?:sanctions[^.\n]{0,40}?)\b((?:OFAC|EU|UN|UK)(?:\s*[/,]\s*(?:OFAC|EU|UN|UK)){1,3})\b",
    re.IGNORECASE)

# A RETIRED VENDOR IS A CLAIM TOO. We stopped using OpenSanctions on
# 2026-08-30; a sweep found the name still published on thirteen surfaces,
# including the tool description every MCP client reads in tools/list, the
# Smithery listing a registry reviewer reads, and the public dashboard - which
# said "screen_sanctions now uses the full 40+ list API". Naming a data source
# we do not query is the same defect as naming a list we do not screen.
RETIRED_SOURCES = {
    "opensanctions": "we removed it 2026-08-30 (its data is CC-BY-NonCommercial "
                     "and we sell screening)",
}

SURFACES = [
    "agent_interface/profiles.py", "llms-install.md", "README.md",
    "server.json", "glama.json", "docs/PRICING.md",
    # The real Smithery listing. This used to be declared as "smithery.yaml" at
    # the repo root - a path that does not exist - and the script's
    # "skip if missing" quietly verified nothing on it for as long as it stood.
    "deploy/registry-submissions/smithery.yaml",
    "manifest/manifest.json", "manifest/mcp_tools.json",
    "edge/src/snapshots/mcp.json", "edge/src/snapshots/llms.txt",
    "edge/src/snapshots/llms-full.txt", "edge/src/snapshots/manifest.json",
    "edge/src/snapshots/mcp-tools-list.json",
    "edge/src/snapshots/openai-tools.json",
    "edge/src/snapshots/anthropic-tools.json",
    "edge/src/snapshots/agents.json",
]

# Published outside the agentbroker repo, but read by the same buyers.
OUTSIDE = [
    "agentbroker-skill/README.md", "agentbroker-skill/SKILL.md",
    "distribution/products/agentbroker.json",
    "web_hatchloop_v2/src/app/pricing/page.tsx",
    "web_hatchloop_v2/src/app/agent-broker/page.tsx",
]


def main() -> int:
    checked, bad, missing = 0, [], []
    targets = [(rel, os.path.join(REPO, rel)) for rel in SURFACES]
    targets += [(rel, os.path.join(os.path.dirname(REPO), rel)) for rel in OUTSIDE]
    for rel, path in targets:
        if not os.path.exists(path):
            # A DECLARED SURFACE THAT DOES NOT EXIST IS A HOLE, NOT A PASS.
            # This used to `continue`, so a typo'd path read as "checked and
            # clean" for ever - which is exactly how the Smithery listing kept
            # its UN claim through every run of this gate.
            missing.append(rel)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        checked += 1
        for n, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue                        # a comment explaining the rule
            for claim in CLAIM.findall(line):
                named = {t.strip().upper() for t in re.split(r"[/,]", claim)}
                for over in sorted(named & set(EXCLUDED)):
                    bad.append((rel, n, claim, over, EXCLUDED[over]))
            low = line.lower()
            for vendor, why in RETIRED_SOURCES.items():
                if vendor in low:
                    bad.append((rel, n, line.strip()[:70], vendor, why))

    if not checked:
        print("check_sanctions_claims: NO SURFACES FOUND - verifying nothing")
        return 2
    if missing:
        print(f"check_sanctions_claims FAILED -- {len(missing)} declared "
              f"surface(s) do not exist, so they were never checked:\n")
        for rel in missing:
            print(f"  {rel}")
        print("\nFix the path or remove it from SURFACES. A surface that "
              "silently does not exist is worse than one that is not listed.")
        return 1
    if bad:
        print(f"check_sanctions_claims FAILED -- {len(bad)} claim(s) advertise a "
              f"list we do not screen:\n")
        for rel, n, claim, over, why in bad:
            print(f"  {rel}:{n}  claims \"{claim}\"")
            print(f"    >> {over} is NOT screened: {why}")
        print(f"\nWe screen {'/'.join(sorted(SCREENED))}. Say that, or add the "
              f"list to the screening code first.")
        return 1
    print(f"check_sanctions_claims OK -- {checked} surface(s), no claim to a "
          f"list we do not screen ({'/'.join(sorted(SCREENED))} screened, "
          f"{'/'.join(sorted(EXCLUDED))} correctly absent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
