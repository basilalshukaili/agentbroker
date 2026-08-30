#!/usr/bin/env python3
"""Our own service must never be published under a generic hostname.

FOUNDER, 2026-08-30, seeing agent-broker-edge.techmate.workers.dev in one of
our files: "I do not want generic urls like these, use hatchloop.dev always."

That is a standing rule, so it is enforced here rather than remembered.

WHY IT KEPT HAPPENING. Our service genuinely runs on infrastructure hostnames -
a Cloudflare Worker at *.workers.dev, a Render origin at *.onrender.com, Vercel
deployment URLs. Those are PLUMBING. Every one of them also works if you paste
it in a browser, which is exactly what makes them so easy to copy into a doc, a
submission, or a descriptor - and once there, they are our public identity.

Two live examples found the day this was written:
  * `publicBaseUrlOf()` in the Worker fell back to the REQUEST's host, so with
    PUBLIC_BASE_URL unset it stamped agent-broker-edge.basil-agent.workers.dev
    into every discovery document as our identity;
  * our Smithery submission listed a hostname on a DIFFERENT COMPANY's
    Cloudflare account as this product's vendor, homepage and MCP endpoint.

WHAT THIS DOES NOT FLAG, deliberately:
  * third-party hostnames (a prospect list full of *.onrender.com sites is
    data about other people, not a claim about us);
  * internal plumbing config - wrangler.toml's ORIGIN_URL is the proxy target
    the Worker fetches from and is never shown to a caller;
  * historical records. An audit or a daily log that says "we used to point at
    X" is a true statement about the past, and rewriting it would falsify the
    record rather than fix anything.

So it looks for OUR service names on generic hosts, in files we PUBLISH.

Usage:
    python scripts/check_branding.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROOT = os.path.dirname(REPO)

BRAND = "hatchloop.dev"

# Our service, wherever it is hosted. A generic host is only a problem when it
# is OURS being presented as us.
OURS_ON_GENERIC = re.compile(
    r"(?:agent-broker-edge|smb-broker|agentbroker|hatchloop)"
    r"[a-z0-9.-]*\.(?:workers\.dev|onrender\.com|vercel\.app)",
    re.IGNORECASE)

# Files a buyer, an agent, or a registry reviewer actually reads.
PUBLISHED = [
    "README.md", "llms-install.md", "OUTREACH_KIT.md", "AGENT_DISTRIBUTION.md",
    "PROJECT_OVERVIEW.md", "HANDOFF.md", "LAUNCH_STATUS.md",
    "EXPIRY_CHECKLIST.md", "server.json", "glama.json", "smithery.yaml",
    "manifest/manifest.json", "manifest/openapi.yaml", "manifest/mcp_tools.json",
    "deploy/registry-submissions/smithery.yaml",
]
PUBLISHED_GLOBS = [
    ("docs", "*.md"),
    ("registry", "server.json"),
    (os.path.join("edge", "src", "snapshots"), "*"),
]

# Historical or internal by nature. Named explicitly so a skip is a decision.
EXEMPT = {
    "AUDIT-2026-08-16.md": "an audit ABOUT those hostnames; rewriting it would falsify the record",
    "wrangler.toml": "ORIGIN_URL is the internal proxy target, never shown to a caller",
    "smithery-sync-2026-08-16.md": "a dated sync log; a true statement about the past",
    "websites_new.json": "third-party prospect data, not claims about us",
    "check_branding.py": "this file names the patterns it bans",
    "index.ts": "URL_PATTERNS is the LIST OF ORIGIN URLS TO REWRITE - the hostnames must appear there for the rewrite to find them",
}


def _files() -> list[str]:
    out = []
    for rel in PUBLISHED:
        p = os.path.join(REPO, rel)
        if os.path.exists(p):
            out.append(p)
    for sub, pat in PUBLISHED_GLOBS:
        base = os.path.join(REPO, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, names in os.walk(base):
            for n in names:
                if pat == "*" or n.endswith(pat.lstrip("*")):
                    out.append(os.path.join(dirpath, n))
    # The marketing site's product pages are published too.
    for rel in ("agent-broker/page.tsx", "pricing/page.tsx"):
        p = os.path.join(ROOT, "web_hatchloop_v2", "src", "app", rel)
        if os.path.exists(p):
            out.append(p)
    return sorted(set(out))


def main() -> int:
    files = _files()
    if not files:
        print("check_branding: FOUND NO FILES TO SCAN - this is verifying nothing")
        return 2

    violations = []
    scanned = 0
    for path in files:
        if os.path.basename(path) in EXEMPT:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        scanned += 1
        for n, line in enumerate(lines, 1):
            # A comment explaining the ban is not the ban.
            st = line.strip()
            if st.startswith(("#", "//", "*")):
                continue
            for hit in OURS_ON_GENERIC.findall(line):
                violations.append((os.path.relpath(path, ROOT), n, hit, st[:96]))

    if violations:
        print(f"check_branding FAILED -- {len(violations)} generic hostname(s) "
              f"published as our identity:\n")
        for rel, n, hit, text in violations:
            print(f"  {rel}:{n}  {hit}")
            print(f"    >> {text}")
        print(f"\nUse https://{BRAND} instead. Our infrastructure hostnames are "
              f"plumbing, not the company's name.")
        return 1

    print(f"check_branding OK -- {scanned} published file(s) scanned, "
          f"0 generic hostnames, {len(EXEMPT)} exempt by name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
