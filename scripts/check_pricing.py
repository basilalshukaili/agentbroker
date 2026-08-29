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

    hits: list[str] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
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
