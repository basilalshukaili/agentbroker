#!/usr/bin/env python3
"""Regenerate manifest/mcp_tools.json from the manifest, instead of by hand.

WHY. This file is the static tool catalogue registry submissions are built
from. It was maintained by hand, and a sync log from 2026-08-16 already
recorded it as "Never regenerated" after two new tools landed. Two weeks
later it was still stale - 18 tools against 20, missing map_trade_restriction
and get_conversation entirely - and it still carried three capability claims
that check_pricing bans everywhere else, including "curated, verified,
transactable" and a description saying verify_business performs a live probe
when it is a directory lookup that contacts nobody.

Anyone reconciling the website's "20 tools" against this file would have
concluded the website was wrong.

A file that must be "manually regenerated and redeployed" will not be. So it
is derived now, from manifest.json, which is the same source tools/list uses.

Usage:
    python scripts/gen_mcp_tools.py            # rewrite the file
    python scripts/gen_mcp_tools.py --check    # fail if it is stale
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "manifest", "manifest.json")
OUT = os.path.join(REPO, "manifest", "mcp_tools.json")


def _build() -> list:
    with open(SRC, encoding="utf-8") as fh:
        ops = json.load(fh).get("operations") or []
    tools = []
    for op in ops:
        entry = {
            "name": op["name"],
            "description": op.get("description", ""),
            "inputSchema": op.get("input_schema")
                           or {"type": "object", "properties": {}},
        }
        ann = op.get("annotations")
        if ann:
            entry["annotations"] = ann
        tools.append(entry)
    return tools


def main(argv: list[str]) -> int:
    if not os.path.exists(SRC):
        print("gen_mcp_tools: no manifest.json - generating nothing")
        return 2
    built = _build()
    if not built:
        print("gen_mcp_tools: manifest has no operations - refusing to write "
              "an empty catalogue over a populated one")
        return 2

    text = json.dumps(built, indent=2, ensure_ascii=True) + "\n"
    if "--check" in argv:
        current = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if current != text:
            try:
                n_now = len(json.loads(current or "[]"))
            except ValueError:
                n_now = "unparseable"
            print(f"gen_mcp_tools FAILED -- manifest/mcp_tools.json is stale "
                  f"({n_now} tools on disk, {len(built)} in the manifest). "
                  f"Run: python scripts/gen_mcp_tools.py")
            return 1
        print(f"gen_mcp_tools OK -- {len(built)} tools, derived from the manifest")
        return 0

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"gen_mcp_tools: wrote {len(built)} tools to manifest/mcp_tools.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
