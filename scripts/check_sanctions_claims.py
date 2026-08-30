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
#
# THE TRIGGER USED TO BE THE LITERAL WORD "sanctions" WITHIN 40 CHARACTERS.
# That is one phrasing out of many, and the ones it missed are the natural
# ones:
#
#     "Screened against OFAC, EU, UN and UK"      <- no "sanctions"
#     "watchlist coverage: OFAC/EU/UN/UK"         <- no "sanctions"
#     "we check OFAC, EU, UN, UK before sending"  <- no "sanctions"
#
# Every one of those claims the UN list, which we deliberately do not screen
# because it carries no commercial licence. The guard existed to stop exactly
# that sentence being published and could not see it written normally.
#
# So: find the run of list names first, then require screening CONTEXT
# anywhere on the line. A false positive here costs one reworded sentence; a
# false negative is a licence claim we cannot back.
LIST_RUN = re.compile(
    r"\b((?:OFAC|EU|UN|UK)(?:\s*[/,]\s*(?:OFAC|EU|UN|UK)){1,3})\b")
SCREENING_CONTEXT = re.compile(
    r"\b(?:sanction\w*|screen\w*|watch\s?list\w*|designat\w*|embargo\w*|"
    r"denied[- ]part\w*|checked against|coverage)\b", re.IGNORECASE)


class _Claim:
    """Keeps the old CLAIM.findall(line) shape for the scanner below."""

    @staticmethod
    def findall(line: str) -> list:
        if not SCREENING_CONTEXT.search(line):
            return []
        return [m.group(1) for m in LIST_RUN.finditer(line)]


CLAIM = _Claim

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
    # THE CODE ITSELF IS A SURFACE. Two false OpenSanctions claims survived the
    # removal sweep by sitting in module docstrings under core/ - one of them
    # still telling a reader we required an API key for a vendor we had just
    # dropped. A comment is not published, but it is what the next person (or
    # the next model) believes before touching the code, and both of those
    # claims stopped anybody re-checking the thing they described.
    "core/screen_sanctions.py", "core/map_trade_restriction.py",
    "agent_interface/mcp_server.py", "agent_interface/profiles.py",
    # The storefront and legal pages. They are rewritten onto hatchloop.dev
    # and read immediately before payment, and were in no guard at all.
    "web/pages.py", "web/_partials.py",
]

# Published outside the agentbroker repo, but read by the same buyers.
OUTSIDE = [
    "agentbroker-skill/README.md", "agentbroker-skill/SKILL.md",
    "distribution/products/agentbroker.json",
    "web_hatchloop_v2/src/app/pricing/page.tsx",
    "web_hatchloop_v2/src/app/agent-broker/page.tsx",
]


# Present-tense capability claims about a retired source, as they appear in
# code. "We used to query OpenSanctions" is history; "queries OpenSanctions"
# and "OPENSANCTIONS_API_KEY required" are promises.
_CODE_CAPABILITY_CLAIM = re.compile(
    r"(?:via|through|using|queries|querying|calls?|screened against|"
    r"powered by)\s+opensanctions"
    r"|opensanctions_api_key"
    r"|opensanctions\s*\(\s*(?:aggregat|cover|40\+|eu|un|ofac)",
    re.IGNORECASE)


def main() -> int:
    checked, bad, missing = 0, [], []
    # TWO KINDS OF ABSENCE, and conflating them broke CI on the very commit
    # that added this check.
    #
    # An IN-REPO path that does not exist is a typo, and it is the bug this
    # guard was hardened for: "smithery.yaml" was declared at a root path that
    # does not exist and skipped silently for as long as it stood, which is
    # exactly how that listing kept a false claim through every run.
    #
    # An OUT-OF-REPO path that does not exist usually means we are running in a
    # repo-only checkout - which is what CI is. Failing there would make the
    # gate depend on the whole workspace being present, so it is reported and
    # skipped instead. It is still checked on any machine that has them.
    targets = [(rel, os.path.join(REPO, rel), True) for rel in SURFACES]
    targets += [(rel, os.path.join(os.path.dirname(REPO), rel), False)
                for rel in OUTSIDE]
    outside_absent = []
    for rel, path, in_repo in targets:
        if not os.path.exists(path):
            if not in_repo:
                outside_absent.append(rel)
                continue
            # A DECLARED SURFACE THAT DOES NOT EXIST IS A HOLE, NOT A PASS.
            # This used to `continue`, so a typo'd path read as "checked and
            # clean" for ever - which is exactly how the Smithery listing kept
            # its UN claim through every run of this gate.
            missing.append(rel)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        checked += 1
        is_code = rel.endswith(".py")
        for n, line in enumerate(lines, 1):
            # THE UN CHECK RUNS EVERYWHERE, INCLUDING IN CODE.
            #
            # The code branch below used to `continue` after its
            # OpenSanctions-specific test, so the UN overclaim check never ran
            # on the four .py surfaces - which are in SURFACES precisely
            # because tool descriptions live in code. Measured: the sentence
            # "Free sanctions screening against OFAC/EU/UN/UK lists" passed
            # clean in profiles.py and failed correctly in README.md.
            for claim in CLAIM.findall(line):
                named = {t.strip().upper() for t in re.split(r"[/,]", claim)}
                for over in sorted(named & set(EXCLUDED)):
                    bad.append((rel, n, claim, over, EXCLUDED[over]))
            if is_code:
                # IN CODE, ONLY PRESENT-TENSE CAPABILITY CLAIMS COUNT.
                #
                # My first version flagged every mention and produced 19 hits,
                # of which most were this file's own record of WHY the
                # dependency was dropped and what we gave up by dropping it.
                # That history is worth more than the guard: it is the reason
                # nobody re-adds the vendor. A checker that fires on it gets
                # switched off, and then the real claims come back.
                #
                # So in code, flag only the shapes that assert we USE it now.
                if not _CODE_CAPABILITY_CLAIM.search(line):
                    continue
                bad.append((rel, n, line.strip()[:70], "opensanctions",
                            RETIRED_SOURCES["opensanctions"]))
                continue
            # A LEADING "#" IS A COMMENT IN PYTHON AND A HEADING IN MARKDOWN.
            # Skipping it wholesale meant "# Sanctions coverage: OFAC/EU/UN/UK"
            # passed clean in a README - a heading is about as published as
            # text gets. Only skip it where it really is a comment.
            if not is_code and line.strip().startswith("#"):
                pass                            # markdown heading - still checked
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
    # One line can trip both the UN rule and the retired-source rule; report
    # each (file, line, subject) once.
    seen_bad = set()
    deduped = []
    for item in bad:
        key = (item[0], item[1], item[3])
        if key in seen_bad:
            continue
        seen_bad.add(key)
        deduped.append(item)
    bad = deduped

    if bad:
        print(f"check_sanctions_claims FAILED -- {len(bad)} claim(s) advertise a "
              f"list we do not screen:\n")
        for rel, n, claim, over, why in bad:
            print(f"  {rel}:{n}  claims \"{claim}\"")
            print(f"    >> {over} is NOT screened: {why}")
        print(f"\nWe screen {'/'.join(sorted(SCREENED))}. Say that, or add the "
              f"list to the screening code first.")
        return 1
    note = ""
    if outside_absent:
        note = (f"; {len(outside_absent)} workspace surface(s) not in this "
                f"checkout, so unchecked here: {', '.join(outside_absent)}")
    print(f"check_sanctions_claims OK -- {checked} surface(s), no claim to a "
          f"list we do not screen ({'/'.join(sorted(SCREENED))} screened, "
          f"{'/'.join(sorted(EXCLUDED))} correctly absent){note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
