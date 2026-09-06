#!/usr/bin/env python3
"""No page may state a tool count as a digit. It must use a token.

WHY. The founder, 2026-09-06: "in console the payment label says 23 tools which
is wrong, always remember that we will scale so many mcps". He was right, and the
sentence was worse than a typo. The page headed "How you pay" describes the
CREDIT RAILS FOR THE WHOLE PLATFORM, and it asserted a tool count belonging to
one product. HatchLoop is being built to carry fifty servers; that sentence stops
being true the day the second one ships.

Eight more were found in the same two files once anyone looked: 12, 3, 8, 15 and
23 in five different phrasings across the home page, the pricing page and the
default meta description.

The numbers were all correct. That is not the point. The defect is that a product
fact reached a public surface BY BEING TYPED, which is the same disease as the six
manifests that disagreed about our own version number that morning.
`agent_interface/mcp_server.py` already carried a helper whose docstring admits
its author hardcoded "12 of the 23 tools" one commit after building a CI gate
against exactly that. The habit is stronger than the rule. So the rule has to be
mechanical.

WHAT REPLACED THEM. `web/facts.py` derives every count from manifest.json and the
auth set; `page()` in `web/_partials.py` substitutes {n_tools}, {n_keyless},
{n_quota}, {n_needs_key}, {n_no_key} into every body and every description. This
script is the half that stops them coming back.

AND IT CATCHES MORE THAN TYPOS. The first derivation written for `needs_key` was
wrong - it subtracted the daily-quota tools, which are not in the auth set, and
would have published 5 where the truth is 8. Deriving a number is not the same as
deriving it correctly, so this also asserts the partition adds up.

    python scripts/check_no_typed_counts.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

# Only the files that RENDER PUBLIC PAGES. Deliberately narrow: the manifest, the
# tests and the comments explaining this rule all contain counts legitimately, and
# a gate that fires on those gets switched off within a week.
SURFACES = ["web/pages.py", "web/_partials.py"]

# "23 tools", "12 utility tools", "15 of the 23 tools", "8 write tools".
PATTERN = re.compile(r"\b\d{1,3}\s+(?:\w+\s+)?tools?\b", re.I)

# A line that is legitimately about a different number.
EXEMPT = (
    "ops/day",          # "100 free ops/day" is a quota, not a tool count
    "credits",
)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def main() -> int:
    offenders, scanned = [], []
    for rel in SURFACES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            print(f"check_no_typed_counts: SURFACE MISSING: {rel}")
            print("Fix the path or remove it here. A surface nobody scans is not "
                  "a surface that is clean.")
            return 1
        scanned.append(rel)
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if _is_comment(line) or any(w in line for w in EXEMPT):
                    continue
                m = PATTERN.search(line)
                if m:
                    offenders.append((rel, n, m.group(0).strip(), line.strip()[:100]))

    # The gate must also prove the derivation it is protecting is sound. A clean
    # scan over templates fed by a wrong function is a green build over a lie.
    problems = []
    try:
        from web import facts
        total = facts.total_tools()
        parts = facts.keyless() + facts.quota_free() + facts.needs_key()
        if total <= 0:
            problems.append("web.facts derives zero tools - it is reading nothing")
        if parts != total:
            problems.append(
                f"the partition does not add up: keyless {facts.keyless()} + quota "
                f"{facts.quota_free()} + needs-key {facts.needs_key()} = {parts}, "
                f"but there are {total} tools. Every tool must fall in exactly one.")
        if facts.substitute("{n_tools}") != str(total):
            problems.append("substitute() does not replace {n_tools}")
    except Exception as exc:                        # noqa: BLE001
        problems.append(f"web.facts could not be evaluated: {exc}")

    if offenders or problems:
        if offenders:
            print(f"check_no_typed_counts: {len(offenders)} typed tool count(s) "
                  f"on public pages:")
            for rel, n, hit, line in offenders:
                print(f"  {rel}:{n}  {hit!r}")
                print(f"      {line}")
            print("\nUse a token instead: {n_tools}, {n_keyless}, {n_quota}, "
                  "{n_needs_key}, {n_no_key}.")
            print("Inside an f-string body, double the braces: {{n_tools}}.")
        for p in problems:
            print(f"  {p}")
        return 1

    print(f"check_no_typed_counts: OK -- {len(scanned)} surface(s) scanned "
          f"({', '.join(scanned)}), no typed tool counts; derivation partitions "
          f"all {facts.total_tools()} tools ({facts.keyless()} keyless + "
          f"{facts.quota_free()} quota + {facts.needs_key()} keyed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
