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

    # BookingPlatform moved, and the `except: pass` meant "booking platforms"
    # was silently never verified - the guard printed OK while one of its
    # three nouns was unchecked. Try the real locations, and if none of them
    # answer, say so instead of skipping.
    for mod, attr in (("supply.booking_page_importer", "BookingPlatform"),
                      ("core.models", "BookingPlatform"),
                      ("core.enums", "BookingPlatform")):
        try:
            m = __import__(mod, fromlist=[attr])
            bp = getattr(m, attr)
            truth["booking platforms"] = len(
                [x for x in bp if getattr(x, "name", "") != "CUSTOM"])
            break
        except Exception:                       # noqa: BLE001
            continue

    return truth


# Published noun -> the derived key it must agree with.
NOUNS = {
    "tools": "tools",
    "operation handlers": "tools",
    "jurisdictions": "jurisdictions",
    "booking platforms": "booking platforms",
}

SURFACES = [
    "README.md", "llms-install.md", "PROJECT_OVERVIEW.md",
    # HANDOFF.md, AGENT_DISTRIBUTION.md, LAUNCH_STATUS.md and OUTREACH_KIT.md
    # were stale pre-pivot operational docs, removed in 4a78f11. A surface that
    # no longer exists must be dropped here, not left to fail as "never checked".
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
    # The storefront and legal pages. They are rewritten onto hatchloop.dev
    # and read immediately before payment, and were in no guard at all.
    "web/pages.py", "web/_partials.py",
]

# A line that is deliberately about a different number, or about the past.
#
# "was " AND "were " USED TO BE ON THIS LIST AND EXEMPTED ALMOST ANYTHING.
# They were meant to catch past tense ("the count was 18"), but they matched
# any sentence containing the word - "18 tools were added this week", "there
# were 18 tools in the first release", "our API was built around 18 tools".
# An exemption this broad is indistinguishable from not scanning the line, and
# a guard that silently stops looking is worse than no guard, because the
# green tick is read as coverage.
EXEMPT_LINES = ("AUDIT-", "changelog", "used to", "previously", "no longer",
                "grew from", "up from")

# Qualifiers that make the number a SUBSET rather than our total.
SUBSET = re.compile(
    r"\b(?:free|no key|without a key|require|read[- ]only|paid|write|"
    r"premium|keyless|anonymous|quota|of our|of the)\b", re.IGNORECASE)

# "100 operations/day" is a rate, not an inventory.
RATE = re.compile(r"\s*(?:/|per\s)\s*(?:day|hour|month|min|second)",
                  re.IGNORECASE)

# A COUNT FOR A DIFFERENT SERVER IS NOT OUR COUNT.
#
# We publish several MCP servers. llms.txt lists each with its own tool count
# - "appointment-booking ... 9 tools", "sms-whatsapp-messaging ... 10 tools" -
# and those are correct statements about those servers. Reporting them as
# disagreeing with AgentBroker's 20 is the guard crying wolf, which gets it
# switched off and then it protects nothing.
OTHER_SERVER = re.compile(
    r"/mcp/(?!agent-broker)[a-z0-9-]+", re.IGNORECASE)


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
    #
    # THE NUMBER AND THE NOUN ARE OFTEN NOT ADJACENT. This required them to
    # touch, so "18 MCP tools" - the exact phrasing our own copy uses - was
    # invisible to the guard written to catch a wrong tool count. Up to two
    # qualifier words are allowed between them; SUBSET still exempts the
    # phrasings that make the number a legitimate subset ("9 free tools"),
    # and RATE still exempts "100 operations per day".
    patterns = {
        noun: re.compile(
            r"(?<![-\d])\b(\d{1,4})\+?\s+(?:[A-Za-z][\w-]*\s+){0,2}"
            + re.escape(noun) + r"\b",
            re.IGNORECASE)
        for noun in NOUNS
    }

    wrong, scanned, missing = [], 0, []
    for rel in SURFACES:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            # A DECLARED SURFACE THAT DOES NOT EXIST IS A HOLE, NOT A PASS.
            # check_sanctions_claims was hardened against exactly this on the
            # same day and this sibling still had the silent `continue`.
            missing.append(rel)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        scanned += 1
        for n, line in enumerate(lines, 1):
            if any(m in line for m in EXEMPT_LINES):
                continue
            # NOT `SUBSET.search(line)`. Exempting the whole line let any
            # sentence containing "free", "of the", "require" etc. carry a
            # wrong count: "AgentBroker exposes 47 tools on the free plan"
            # passed clean. The qualifier only matters if it is NEAR the
            # number, so the window is checked at the match instead.
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
                    window = line[max(0, m.start() - 40):m.end() + 40]
                    if SUBSET.search(window):
                        continue
                    if OTHER_SERVER.search(line):
                        continue                # a different server's count
                    wrong.append((rel, n, found, noun, truth[key],
                                  line.strip()[:70]))

    if not scanned:
        print("check_counts: NO SURFACES FOUND - verifying nothing")
        return 2
    if missing:
        print(f"check_counts FAILED -- {len(missing)} declared surface(s) do "
              f"not exist, so they were never checked:\n")
        for rel in missing:
            print(f"  {rel}")
        return 1

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
