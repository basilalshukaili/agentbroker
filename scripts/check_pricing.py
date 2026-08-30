#!/usr/bin/env python3
"""
scripts/check_pricing.py -- Pricing copy linter.

Scans documentation, manifests, and skill files for:
  1. BANNED phrases -- retired or misleading pricing claims that must never
     appear in public-facing copy after the freemium launch.
  2. No false-flags -- new honest freemium copy is explicitly allowed.

Exit 0 if clean.  Exit 1 if any banned phrase is found (prints each hit).

Run from the agentbroker/ directory:
    python scripts/check_pricing.py

Or from the repo root:
    python agentbroker/scripts/check_pricing.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve paths relative to this script.
# Script is at  <repo>/agentbroker/scripts/check_pricing.py
# repo root is  <repo>/                   (agentbroker/../..)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_AGENTBROKER_DIR = _SCRIPT_DIR.parent           # agentbroker/
_REPO_ROOT = _AGENTBROKER_DIR.parent            # C:\ai company\

# Files to scan (absolute paths)
_SCAN_FILES = [
    _AGENTBROKER_DIR / "README.md",
    _AGENTBROKER_DIR / "llms-install.md",
    _REPO_ROOT / "agentbroker-skill" / "SKILL.md",
    _REPO_ROOT / "agentbroker-skill" / "README.md",
    _REPO_ROOT / "distribution" / "products" / "agentbroker.json",
    _AGENTBROKER_DIR / "agent_interface" / "mcp_server.py",
    _AGENTBROKER_DIR / "billing" / "pricing.py",
]

# --- AND EVERY DOC, because an enumerated list is a list someone forgets ----
#
# The seven paths above were typed by hand, and a pricing claim in any file NOT
# on that list was invisible to this guard - which is the whole failure mode it
# exists to prevent. `docs/` is public: PRICING.md, the integration guide and
# SECURITY.md are all things a buyer reads, and none of them were covered.
#
# Globbing means a doc added next month is checked without anyone remembering
# to add it here. Duplicates are removed rather than double-reported, and the
# printed total is derived from what was actually scanned so the summary line
# cannot overstate coverage the way a hardcoded count would.
_SCAN_FILES += sorted(
    p for p in (_AGENTBROKER_DIR / "docs").rglob("*.md")
    if p.is_file()
)

# --- AND THE THREE PLACES THE BANNED STRINGS ACTUALLY SURVIVED IN ----------
#
# The globbing argument above was right and stopped one file short. An audit
# found every one of these carrying a string this guard already forbids:
#
#   * manifest/mcp_tools.json - "curated, verified, transactable", the exact
#     phrase banned at line 171, in a file both check_branding and
#     check_sanctions_claims treat as published;
#   * web_hatchloop_v2/src/app/llms.txt/route.ts - three pinned
#     buy.polar.sh/polar_cl_ checkout links, banned at line 175, in the file
#     AGENTS read. They had been removed from the two site pages this guard
#     did scan and left in the one it did not;
#   * mcp-data-tools/page.tsx - "Search verified SMBs" and "against live
#     sources", both banned capability claims.
#
# Two site pages were enumerated by hand; the rest of the site was invisible.
# Same failure, one level out.
_SCAN_FILES += sorted(
    p for p in (_AGENTBROKER_DIR / "manifest").glob("*.json") if p.is_file()
)
_SCAN_FILES += sorted(
    p for p in (_AGENTBROKER_DIR / "edge" / "src" / "snapshots").iterdir()
    if p.is_file() and p.suffix in (".json", ".txt")
)
_SITE_APP = _REPO_ROOT / "web_hatchloop_v2" / "src"
if _SITE_APP.is_dir():
    _SCAN_FILES += sorted(
        p for p in _SITE_APP.rglob("*.ts*")
        if p.is_file() and "node_modules" not in p.parts
    )

# THE EDGE WORKER SOURCES, for the same reason and with a live example.
#
# hatchloop.dev is served by a Cloudflare worker in front of the origin, and it
# generates its own copy. On 2026-08-29 its free-tier quota response told every
# agent that hit the cap: "Upgrade to unlimited at <hardcoded Polar link>" -
# a monthly subscription that docs/PRICING.md says was never in effect at any
# price. It had been live for weeks and this guard could not see it, because
# edge/src was not on the hand-typed list above.
#
# dist*/ is EXCLUDED deliberately: those are build outputs. Flagging a stale
# claim in a bundle nobody edits produces a violation that cannot be fixed by
# editing the file it names, which is how a guard gets marked noisy and then
# ignored. Fix the source; the bundle follows on the next deploy.
_SCAN_FILES += sorted(
    p for p in (_AGENTBROKER_DIR / "edge" / "src").rglob("*.ts")
    if p.is_file()
)

# AND THE MARKETING SITE'S PRODUCT PAGE.
#
# It described find_business as "Search verified businesses" and
# verify_business as "Check listing accuracy against live sources" - the exact
# two claims corrected in the tool descriptions this morning, still live on the
# page a human buyer reads. The page and the tool are two copies of one claim,
# and only one of them was being checked.
_SCAN_FILES += [
    _REPO_ROOT / "web_hatchloop_v2" / "src" / "app" / "agent-broker" / "page.tsx",
    _REPO_ROOT / "web_hatchloop_v2" / "src" / "app" / "pricing" / "page.tsx",
]
_SCAN_FILES = list(dict.fromkeys(_SCAN_FILES))

# ---------------------------------------------------------------------------
# BANNED phrases (case-insensitive substrings).
# These represent retired claims that are now false or misleading.
# ---------------------------------------------------------------------------
_BANNED: list[tuple[str, str]] = [
    # Claiming data tools are unconditionally/always free with no limit
    (
        r"verify_company_record.*always free",
        "BANNED: implies verify_company_record is unconditionally free (no daily limit)"
    ),
    (
        r"screen_sanctions.*always free",
        "BANNED: implies screen_sanctions is unconditionally free (no daily limit)"
    ),
    (
        r"map_trade_restriction.*always free",
        "BANNED: implies map_trade_restriction is unconditionally free (no daily limit)"
    ),
    (
        r"(?:11|eleven)\s+(?:read\s+)?tools\s+always free",
        "BANNED: '11 tools always free' is outdated -- 8 are unconditionally free; 3 have a daily quota"
    ),
    (
        r"unlimited.*verify_company_record",
        "BANNED: 'unlimited' for verify_company_record is false after freemium launch"
    ),
    (
        r"unlimited.*screen_sanctions",
        "BANNED: 'unlimited' for screen_sanctions is false after freemium launch"
    ),
    (
        r"unlimited.*map_trade_restriction",
        "BANNED: 'unlimited' for map_trade_restriction is false after freemium launch"
    ),
    # Old flat-subscription language (fully retired)
    (
        r"\$9\s*/\s*(?:month|mo\b|unlimited|flat)",
        "BANNED: retired flat-rate subscription pricing ($9/month or $9/unlimited)"
    ),
    (
        r"unlimited\s+(?:plan|subscription|access)\s+for\s+\$",
        "BANNED: retired 'unlimited plan for $X' pricing"
    ),
    (
        r"90[\s-]day\s+unlimited",
        "BANNED: retired '$9/90d unlimited' plan language"
    ),
    # WE SELL CREDIT PACKAGES. There is no subscription at any price and there
    # never was - docs/PRICING.md says so in a section written specifically to
    # correct an earlier version of that file. The patterns above were all
    # anchored to the exact retired WORDINGS, so the edge worker's "Upgrade to
    # unlimited at <link>" walked straight past every one of them and shipped
    # to every rate-limited agent for weeks.
    #
    # These match the CLAIM rather than a phrasing of it.
    (
        r"(?i)upgrade\s+to\s+unlimited",
        "BANNED: 'upgrade to unlimited' - we sell credit packages, not an "
        "unlimited tier. Link https://hatchloop.dev/pricing instead."
    ),
    (
        r"(?i)\$\d+\s*/\s*mo(?:nth)?\s+subscription",
        "BANNED: names a monthly subscription. No subscription tier at any "
        "price has ever been in effect (docs/PRICING.md)."
    ),
    # CAPABILITY OVERCLAIMS. This linter began as a PRICING checker, but the
    # defect class is identical - a sentence promising more than the code does -
    # and the same sentence lives on several surfaces at once. The three below
    # were each corrected in one place and left standing in another.
    (
        r"(?i)search\s+verified\s+businesses",
        "BANNED: the supply network is small and largely [DEMO] sample data. "
        "The tool description says so; the site must not say otherwise."
    ),
    (
        r"(?i)(check listing accuracy against|live capability probe)",
        "BANNED: verify_business is a DIRECTORY LOOKUP. It contacts nobody."
    ),
    (
        r"(?i)curated,\s*verified,\s*transactable",
        "BANNED: retired claim about the supply network."
    ),
    (
        r"(?i)buy\.polar\.sh/polar_cl_",
        "BANNED: a hardcoded Polar checkout link. It cannot track a package "
        "change, so it goes stale silently and points paying customers at a "
        "retired product. Link https://hatchloop.dev/pricing, which is "
        "generated from the live packages."
    ),
]

# Pre-compile patterns (case-insensitive, dotall).
_COMPILED = [
    (re.compile(pat, re.IGNORECASE | re.DOTALL), reason)
    for pat, reason in _BANNED
]


def _check_file(path: Path) -> list[str]:
    """Return a list of violation strings for `path`, or empty list if clean."""
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [f"  WARN: cannot read {path}: {exc}"]

    # In CODE, skip comment lines. A comment saying "we must never claim X"
    # is the opposite of claiming X, and flagging it makes the guard fire on
    # the very note explaining the ban - which is how this checker first
    # reported three violations that were all its own documentation.
    #
    # NOT in Markdown or JSON: `#` starts a heading there, and a banned claim
    # in a heading is exactly the kind that gets read.
    is_code = path.suffix.lower() in {".py", ".ts", ".js", ".tsx", ".mjs"}

    hits: list[str] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        if is_code:
            st = line.lstrip()
            if st.startswith(("#", "//", "*", "/*")):
                continue
        for pattern, reason in _COMPILED:
            if pattern.search(line):
                hits.append(
                    f"  {path.relative_to(_REPO_ROOT)} line {lineno}: {reason}\n"
                    f"    >> {line.strip()[:120]}"
                )
    return hits



# ---------------------------------------------------------------------------
# PAYMENT-RAIL CLAIMS MUST BE DERIVED, NEVER ASSERTED
# ---------------------------------------------------------------------------
#
# This checker was built to stop us ADVERTISING a crypto rail we did not offer.
# On 2026-08-29 the failure ran the other way and was worse: the x402 rail was
# LIVE in production (X402_ENABLED=true, real receiver address, returning
# complete payment offers) while every discovery surface hardcoded a denial -
# `"rails": ["credits"]`, "the rail is built and switched off",
# "Crypto is not offered". The one payment path an autonomous agent can
# complete WITHOUT A HUMAN was the one we told everybody did not exist.
#
# Both failures have the same cause: a claim about the rail written as a
# constant. A constant cannot track a runtime flag, so it is wrong the moment
# the flag moves - in whichever direction it moves.
#
# So the rule is not "never mention x402" and not "always mention x402". It is
# that code which GENERATES public copy must ask the gate. Documentation and
# marketing prose may still say whatever is currently true; this only polices
# the generators, which are the surfaces an agent actually reads.

_RAIL_ASSERTIONS = [
    (r'"rails"\s*:\s*\[[^\]]*\]',
     'hardcodes the payment rails - derive them from x402_gate.enabled()'),
    (r'(?i)(x402|crypto)[^\n]{0,40}(is\s+not\s+offered|switched\s+off|is\s+off\b)',
     'asserts the crypto rail is off - it was ON for weeks while this said so'),
    (r'(?i)all tools are (currently )?free to call',
     'asserts everything is free - credits have been live since 2026-08-24'),
]

# Files that GENERATE what an agent reads. Prose files are excluded on purpose:
# a doc page saying "we accept cards" is a statement of fact someone maintains,
# not a constant masquerading as one.
_GENERATORS = [
    "agent_interface/well_known.py",
    "agent_interface/mcp_server.py",
    "agent_interface/discovery.py",
]


def check_rail_claims() -> list[str]:
    problems = []
    for rel in _GENERATORS:
        path = _AGENTBROKER_DIR / rel
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pat, why in _RAIL_ASSERTIONS:
                    if re.search(pat, line):
                        problems.append(f"{rel}:{n} {why} -> {stripped[:80]}")
    return problems


def main() -> int:
    all_hits: list[str] = []
    for fpath in _SCAN_FILES:
        all_hits.extend(_check_file(fpath))

    rail_hits = check_rail_claims()

    if all_hits or rail_hits:
        if all_hits:
            print("check_pricing FAILED -- banned pricing phrases found:")
            for h in all_hits:
                print(h)
        if rail_hits:
            print("check_pricing FAILED -- payment-rail claims are hardcoded:")
            for h in rail_hits:
                print(h)
        return 1

    print(f"check_pricing OK -- {len(_SCAN_FILES)} files scanned, "
          f"{len(_GENERATORS)} generators checked, 0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
