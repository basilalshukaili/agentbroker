#!/usr/bin/env python3
"""A number in published copy that names a countable thing must match the count.

WHY. An audit of published surfaces found the same defect a dozen times, in
the same shape every time: a count typed by hand into prose, correct on the
day it was written, silently false afterwards.

  * "22 jurisdictions" on 15+ surfaces including the send_message tool
    description every MCP client reads at connect time. The real number is 26,
    and mcp_server.py says 26 four lines away from a line saying 22. We were
    UNDER-claiming our own compliance coverage by four countries.
  * "12 tools", "13 tools", "14 operation handlers", "18 tools" - all in
    published files, all wrong, against 20.

None of these is dangerous on its own. Together they are the reason a buyer
checking one number against another concludes we do not know our own product.

WHAT THIS DOES. Derives each count from the artefact itself, then reads every
published surface for a number attached to that noun. A mismatch fails.

WHAT IT DELIBERATELY DOES NOT DO is parse English. The first version flagged
"100 operations/day" (a quota), "~20-100 tools" (what an agent sees in
general) and "12 tools require no key" (a correct SUBSET count). A checker
that reports correct numbers as wrong gets switched off, and then it protects
nothing. So a match counts only when it is not part of a range, not a rate,
and not qualified as a subset - and anything ambiguous is left alone.

Usage:
    python scripts/check_counts.py
    python scripts/check_counts.py --show      # print the derived truth
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _derive() -> dict:
    """The ground truth, computed from the artefacts themselves."""
    truth: dict[str, int] = {}

    with open(os.path.join(REPO, "manifest", "manifest.json"),
              encoding="utf-8") as fh:
        truth["tools"] = len(json.load(fh).get("operations") or [])

    sys.path.insert(0, REPO)
    try:
        from compliance.jurisdiction_rules import _RULES
        # Country-level only. The US state rules are a finer grain and are not
        # what "jurisdictions" means in the copy.
        truth["jurisdictions"] = len(
            [k for k in _RULES if not str(k).upper().startswith("US-")])
    except Exception:                           # noqa: BLE001
        pass

    try:
        from core.models import BookingPlatform
        truth["booking platforms"] = len(
            [p for p in BookingPlatform if p.name != "CUSTOM"])
    except Exception:                           # noqa: BLE001
        pass

    return truth


# Published noun -> the derived key it must agree with.
NOUNS = {
    "tools": "tools",
    "operation handlers": "tools",
    "jurisdictions": "jurisdictions",
    "booking platforms": "booking platforms",
}

SURFACES = [
    "README.md", "llms-install.md", "PROJECT_OVERVIEW.md", "HANDOFF.md",
    "AGENT_DISTRIBUTION.md", "LAUNCH_STATUS.md", "OUTREACH_KIT.md",
    "docs/PRICING.md", "docs/BENCHMARKS.md", "docs/mission.md",
    "server.json", "glama.json", "manifest/manifest.json",
    "manifest/mcp_tools.json",
    "deploy/registry-submissions/smithery.yaml",
    "edge/src/snapshots/llms.txt", "edge/src/snapshots/llms-full.txt",
    "edge/src/snapshots/mcp.json",
    # The cookbook prompt and the compliance docstring are read by agents and
    # by whoever edits next; both carried "22 jurisdictions" four lines from
    # a line saying 26.
    "agent_interface/mcp_server.py", "core/check_compliance.py",
]

# A line that is deliberately about a different number, or about the past.
EXEMPT_LINES = ("AUDIT-", "changelog", "used to", "previously", "no longer",
                "grew from", "up from", "was ", "were ")

# Qualifiers that make the number a SUBSET rather than our total.
SUBSET = re.compile(
    r"\b(?:free|no key|without a key|require|read[- ]only|paid|write|"
    r"premium|keyless|anonymous|quota|of our|of the)\b", re.IGNORECASE)

# "100 operations/day" is a rate, not an inventory.
RATE = re.compile(r"\s*(?:/|per\s)\s*(?:day|hour|month|min|second)",
                  re.IGNORECASE)


def main(argv: list[str]) -> int:
    truth = _derive()
    if not truth:
        print("check_counts: DERIVED NOTHING - verifying nothing")
        return 2
    if "--show" in argv:
        for k, v in sorted(truth.items()):
            print(f"  {k}: {v}")
        return 0

    # (?<![-\d]) keeps "20-100 tools" from reading as a claim of 100.
    patterns = {
        noun: re.compile(r"(?<![-\d])\b(\d{1,4})\+?\s+" + re.escape(noun) + r"\b",
                         re.IGNORECASE)
        for noun in NOUNS
    }

    wrong, scanned = [], 0
    for rel in SURFACES:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        scanned += 1
        for n, line in enumerate(lines, 1):
            if any(m in line for m in EXEMPT_LINES):
                continue
            if SUBSET.search(line):
                continue
            for noun, pat in patterns.items():
                key = NOUNS[noun]
                if key not in truth:
                    continue
                for m in pat.finditer(line):
                    found = m.group(1)
                    if int(found) == truth[key]:
                        continue
                    if RATE.match(line[m.end():m.end() + 12]):
                        continue
                    wrong.append((rel, n, found, noun, truth[key],
                                  line.strip()[:70]))

    if not scanned:
        print("check_counts: NO SURFACES FOUND - verifying nothing")
        return 2

    if wrong:
        print(f"check_counts FAILED -- {len(wrong)} published count(s) "
              f"disagree with reality:\n")
        for rel, n, found, noun, real, text in wrong:
            print(f"  {rel}:{n}  says {found} {noun}, actual is {real}")
            print(f"    >> {text}")
        print("\nDerive the number, or fix it. A count typed by hand is true "
              "only on the day it is typed.")
        return 1

    shown = ", ".join(f"{v} {k}" for k, v in sorted(truth.items()))
    print(f"check_counts OK -- {scanned} surface(s) agree with reality ({shown})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
