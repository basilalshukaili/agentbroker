#!/usr/bin/env python3
"""Refresh the edge worker's embedded discovery snapshots from the live origin.

WHY. hatchloop.dev/mcp/agent-broker routes Vercel -> Cloudflare edge worker ->
Render origin, and the worker answers `initialize`, `tools/list`, `/manifest`
and every `.well-known/*` route from snapshots compiled INTO its bundle. Only
`tools/call` reaches the origin. So any origin change to a schema, a price, a
tool count or the service identity is invisible on the canonical host until the
snapshots are regenerated and the worker redeployed — the canonical host keeps
serving the old answer while the origin is correct, and nothing reports it.

That has bitten us repeatedly (stale serverInfo, 17-vs-19 tool counts, the
manifest advertising prices we do not charge). This script makes the refresh a
command instead of a memory.

    python scripts/refresh_edge_snapshots.py --check    # report drift only
    python scripts/refresh_edge_snapshots.py            # rewrite snapshots

Then deploy:
    cd agentbroker/edge && CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... \
        npx wrangler deploy
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "edge", "src", "snapshots")
ORIGIN = os.environ.get("AGENTBROKER_ORIGIN", "https://api.hatchloop.dev")

# snapshot file -> how to fetch it from the origin
GET_ROUTES = {
    "manifest.json": "/manifest",
    "agents.json": "/.well-known/agents.json",
    "ai-plugin.json": "/.well-known/ai-plugin.json",
    "openai-tools.json": "/.well-known/openai-tools.json",
    "anthropic-tools.json": "/.well-known/anthropic-tools.json",
    "mcp.json": "/.well-known/mcp.json",
    "agent-service.json": "/.well-known/agent-service",
    "supply-platforms.json": "/supply/platforms",
    "jurisdictions.json": "/compliance/jurisdictions",
}

# JSON-RPC snapshots: file -> method
RPC_ROUTES = {
    "mcp-tools-list.json": "tools/list",
    "mcp-initialize.json": "initialize",
}


def _curl(url: str, data: str | None = None) -> str:
    """curl, not urllib: Cloudflare 403s urllib's default agent from here."""
    cmd = ["curl", "-s", "--max-time", "45", url]
    if data is not None:
        cmd += ["-X", "POST",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json, text/event-stream",
                "-d", data]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return p.stdout


def _parse_maybe_sse(raw: str) -> dict | None:
    """The origin may answer JSON-RPC as SSE. Accept either shape."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    return None


def fetch_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for fname, route in GET_ROUTES.items():
        body = _curl(ORIGIN + route)
        try:
            out[fname] = json.loads(body)
        except json.JSONDecodeError:
            print(f"  ! skip {fname}: origin returned non-JSON "
                  f"({len(body)}B) from {route}")
    for fname, method in RPC_ROUTES.items():
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if method == "initialize":
            payload["params"] = {"protocolVersion": "2025-06-18",
                                 "capabilities": {},
                                 "clientInfo": {"name": "snapshot-refresh",
                                                "version": "1"}}
        parsed = _parse_maybe_sse(_curl(ORIGIN + "/mcp", json.dumps(payload)))
        if parsed and "result" in parsed:
            out[fname] = parsed
        else:
            print(f"  ! skip {fname}: no usable result for {method}")
    return out


def _summarize(fname: str, doc: dict) -> str:
    """A short fingerprint of the things that actually drift."""
    if fname == "manifest.json":
        svc = doc.get("service", {})
        return (f"{svc.get('id')}/{svc.get('version')} "
                f"{len(doc.get('operations', []))} ops")
    if fname == "mcp-tools-list.json":
        return f"{len(doc.get('result', {}).get('tools', []))} tools"
    if fname == "mcp-initialize.json":
        si = doc.get("result", {}).get("serverInfo", {})
        return f"{si.get('name')}/{si.get('version')}"
    return f"{len(json.dumps(doc))}B"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift, write nothing, exit 1 if stale")
    args = ap.parse_args()

    print(f"origin: {ORIGIN}")
    fresh = fetch_all()
    if not fresh:
        print("refresh_edge_snapshots: FAILED - origin unreachable")
        return 2

    stale = []
    for fname, doc in fresh.items():
        path = os.path.join(SNAP, fname)
        old = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    old = json.load(fh)
            except Exception:  # noqa: BLE001
                old = None
        if old == doc:
            continue
        stale.append(fname)
        print(f"  STALE {fname}: edge[{_summarize(fname, old or {})}] "
              f"!= origin[{_summarize(fname, doc)}]")
        if not args.check:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                json.dump(doc, fh, indent=2, ensure_ascii=False)
                fh.write("\n")

    if not stale:
        print("refresh_edge_snapshots: IN SYNC with origin")
        return 0
    if args.check:
        print(f"refresh_edge_snapshots: {len(stale)} snapshot(s) STALE "
              f"- the canonical host is serving old answers")
        return 1
    print(f"refresh_edge_snapshots: rewrote {len(stale)} snapshot(s). "
          f"Now run: cd edge && npx wrangler deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
