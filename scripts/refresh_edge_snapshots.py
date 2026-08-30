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
    # THESE THREE WERE MISSING, AND THIS SCRIPT REPORTED "IN SYNC" ANYWAY.
    #
    # They exist as snapshot files on disk and are served by the Worker, but
    # they were absent from this map - so the refresher never fetched them,
    # never compared them, and still printed a clean bill of health covering
    # 11 of 14 snapshots.
    #
    # llms.txt was consequently stale by weeks: it carried the old
    # *.onrender.com endpoints while the origin had long since moved to the
    # branded host. Nobody saw it because the Worker rewrites known origin URLs
    # at serve time, so the LIVE output looked right while the source did not -
    # a mask over a stale file, which is the worst of both.
    "llms.txt": "/llms.txt",
    "llms-full.txt": "/llms-full.txt",
    "openapi.yaml": "/openapi.yaml",
}

# JSON-RPC snapshots: file -> method
RPC_ROUTES = {
    "mcp-tools-list.json": "tools/list",
    "mcp-initialize.json": "initialize",
}


def _every_snapshot_is_covered() -> list[str]:
    """Snapshot files on disk that no route in this file refreshes.

    A map maintained by hand drifts from a directory maintained by code. This
    turns that drift into a loud failure instead of a silent gap in a report
    that says "IN SYNC".
    """
    known = set(GET_ROUTES) | set(RPC_ROUTES)
    # Not content: TypeScript glue and type declarations live here too.
    ignore = {"index.ts", "modules.d.ts"}
    on_disk = {f for f in os.listdir(SNAP)
               if f not in ignore and not f.startswith(".")}
    return sorted(on_disk - known)


def _curl(url: str, data: str | None = None) -> str:
    """curl, not urllib: Cloudflare 403s urllib's default agent from here.

    Captures BYTES and decodes UTF-8 explicitly. text=True would decode with
    the platform locale codec (cp1252 on Windows), which silently turns every
    em-dash into mojibake and writes it into the snapshots we ship - it did
    exactly that on the first run of this script, corrupting 155 strings across
    7 files before anything noticed (2026-08-26). JSON on the wire is UTF-8 by
    spec; never let the locale decide.
    """
    cmd = ["curl", "-s", "--max-time", "45", url]
    if data is not None:
        cmd += ["-X", "POST",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json, text/event-stream",
                "-d", data]
    p = subprocess.run(cmd, capture_output=True, timeout=60)  # bytes, not text
    return p.stdout.decode("utf-8", errors="replace")


_MOJIBAKE = ("â€", "Ã©", "Â ")


def _has_mojibake(doc) -> bool:
    """Refuse to WRITE a snapshot that carries locale-corruption signatures."""
    blob = json.dumps(doc, ensure_ascii=False)
    return any(sig in blob for sig in _MOJIBAKE)


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


# Snapshots that are NOT JSON. They are still published, still go stale, and
# were skipped by a JSON-only fetcher that then reported "IN SYNC" - llms.txt
# sat weeks out of date behind that skip.
TEXT_SNAPSHOTS = {"llms.txt", "llms-full.txt", "openapi.yaml"}


def fetch_all() -> dict[str, object]:
    out: dict[str, object] = {}
    failed: list[str] = []
    for fname, route in GET_ROUTES.items():
        body = _curl(ORIGIN + route)
        if fname in TEXT_SNAPSHOTS:
            if body.strip():
                out[fname] = body
            else:
                failed.append(f"{fname}: empty response from {route}")
            continue
        try:
            out[fname] = json.loads(body)
        except json.JSONDecodeError:
            # A SKIP IS A FAILURE, not a shrug. Reporting "in sync" while a
            # file was never fetched is how llms.txt stayed stale.
            failed.append(f"{fname}: non-JSON ({len(body)}B) from {route}")
    if failed:
        print("  ! COULD NOT FETCH:")
        for f in failed:
            print(f"      {f}")
        out["__fetch_failures__"] = failed
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

    # Refuse to claim "in sync" while a snapshot exists that we never look at.
    uncovered = _every_snapshot_is_covered()
    if uncovered:
        print("refresh_edge_snapshots: THESE SNAPSHOTS ARE NOT COVERED BY ANY "
              "ROUTE, so nothing here can tell you whether they are stale:")
        for f in uncovered:
            print(f"    {f}")
        print("Add each to GET_ROUTES or RPC_ROUTES, or delete it if the Worker "
              "no longer serves it.")
        return 2

    fresh = fetch_all()
    fetch_failures = fresh.pop("__fetch_failures__", [])
    if not fresh:
        print("refresh_edge_snapshots: FAILED - origin unreachable")
        return 2

    stale = []
    for fname, doc in fresh.items():
        path = os.path.join(SNAP, fname)
        # llms.txt / llms-full.txt / openapi.yaml are text, not JSON. Reading
        # them with json.load() silently yielded None, which compared unequal
        # forever - or, before they were routed at all, never compared.
        is_text = fname in TEXT_SNAPSHOTS
        old = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    old = fh.read() if is_text else json.load(fh)
            except Exception:  # noqa: BLE001
                old = None
        if old == doc:
            continue
        if _has_mojibake(doc):
            # Never write corruption over a good snapshot. If this fires, the
            # fetch decoded wrongly - fix the fetch, do not "clean" the text.
            print(f"  ! REFUSED {fname}: fetched body contains mojibake "
                  f"(encoding bug upstream of here) - snapshot left untouched")
            continue
        stale.append(fname)
        print(f"  STALE {fname}: edge[{_summarize(fname, old or {})}] "
              f"!= origin[{_summarize(fname, doc)}]")
        if not args.check:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                if is_text:
                    fh.write(doc)
                else:
                    json.dump(doc, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")

    # A FETCH FAILURE IS NOT "IN SYNC". Saying so while a file was never
    # compared is how llms.txt stayed weeks out of date behind a "! skip"
    # line that changed nothing about the verdict.
    if fetch_failures:
        print(f"refresh_edge_snapshots: {len(fetch_failures)} snapshot(s) could "
              f"not be fetched - their freshness is UNKNOWN, not verified")
        return 2

    if not stale:
        print(f"refresh_edge_snapshots: IN SYNC with origin "
              f"({len(fresh)} snapshot(s) compared)")
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
