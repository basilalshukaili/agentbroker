"""Record what a REAL MCP client sends when it connects to this server, as a replayable fixture.

    python scripts/record_mcp_handshake.py --out tests/fixtures/mcp_handshakes/python_sdk_legacy.json
    python scripts/record_mcp_handshake.py --root C:\\path\\to\\another\\checkout --out base.json

It drives the official MCP Python SDK client (`pip install mcp`; a dev tool, not a runtime dependency) against
the app in this process through an ASGI transport. Nothing leaves the machine: the usage log and the spine
client are stubbed before the app is imported, so a recording can never write a row anywhere.

WHAT IS RECORDED. At the HTTP layer, for every request the client sends: method, path, the protocol-relevant
headers (never credentials: there are none to send), and the JSON body; and for each response its status and
a SHAPE (result keys, error code, tool names), not the full text - tools/list and `instructions` depend on the
deployment's configuration and would make a fixture that only passes on the machine that recorded it.

WHY `--root`. The legacy fixture in tests/fixtures was recorded from the commit BEFORE the 2026-07-28 work
(1f85885), so the replay test is a true before/after pin on what older clients get: the recorded shapes are
what the old server answered, and the test asserts the new server answers the same.

Provenance is written into the fixture. The installed SDK speaks protocol versions up to 2025-11-25; no
2026-07-28 client SDK was available to record (see tests/fixtures/mcp_handshakes/README.md).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

KEEP_HEADERS = ("content-type", "accept", "mcp-protocol-version", "mcp-method", "mcp-name", "mcp-session-id")


def _shape(status: int, body: bytes, content_type: str) -> dict:
    out: dict = {"status": status}
    if not body:
        out["body"] = "empty"
        return out
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        # Streamable HTTP may answer with SSE; the JSON-RPC message is in the `data:` lines.
        data = "".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
        text = data or text
    try:
        doc = json.loads(text)
    except ValueError:
        out["body"] = "not-json"
        return out
    if isinstance(doc, dict):
        if "error" in doc:
            out["error_code"] = doc["error"].get("code")
        res = doc.get("result")
        if isinstance(res, dict):
            out["result_keys"] = sorted(res.keys())
            if "protocolVersion" in res:
                out["protocolVersion"] = res["protocolVersion"]
            if isinstance(res.get("serverInfo"), dict):
                out["serverInfo_name"] = res["serverInfo"].get("name")
            if isinstance(res.get("tools"), list):
                out["tool_names"] = [t.get("name") for t in res["tools"]]
            if "isError" in res:
                out["isError"] = res["isError"]
    return out


async def record(root: Path, server_commit: str = "") -> dict:
    sys.path.insert(0, str(root))
    os.chdir(root)

    # Stub every outbound path BEFORE the app is imported.
    from billing import usage_logger as ul
    ul.fire_log_outcome = lambda event: None
    import storage.supabase_client as sb

    async def _no_network(*_a, **_k):
        raise RuntimeError("recording must not reach the spine")
    sb.rpc = _no_network

    import httpx
    import main
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    import importlib.metadata as md

    exchanges: list = []
    pending: dict = {}

    async def on_request(req: httpx.Request) -> None:
        body = req.content or b""
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        pending[id(req)] = {
            "method": req.method,
            "path": req.url.path,
            "headers": {k: v for k, v in ((k.lower(), v) for k, v in req.headers.items()) if k in KEEP_HEADERS},
            "json": parsed,
        }

    async def on_response(resp: httpx.Response) -> None:
        await resp.aread()
        entry = pending.pop(id(resp.request), None)
        if entry is not None:
            entry["recorded_response"] = _shape(resp.status_code, resp.content, resp.headers.get("content-type", ""))
            exchanges.append(entry)

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
            headers=headers, timeout=timeout or 30, auth=auth, follow_redirects=True,
            event_hooks={"request": [on_request], "response": [on_response]})

    async with streamablehttp_client("http://testserver/mcp", httpx_client_factory=factory) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()
            await session.call_tool("preview_cost", {"operation": "send_message", "params": {}})
            await session.send_ping()

    try:
        sdk_version = md.version("mcp")
    except Exception:  # noqa: BLE001
        sdk_version = "unknown"
    return {
        "provenance": {
            "client": f"mcp (official Python SDK) {sdk_version}",
            "recorded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "how": "real client driven in-process over an ASGI transport; requests captured at the HTTP layer",
            "era": "legacy (initialize handshake, protocol versions up to 2025-11-25)",
            "server_commit_recorded": server_commit
            or os.popen(f'git -C "{root}" rev-parse --short HEAD').read().strip() or "unknown",
        },
        "exchanges": exchanges,
    }


def main_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="checkout to record against")
    ap.add_argument("--out", required=True)
    ap.add_argument("--server-commit", default="", help="label for a checkout with no .git (e.g. a git archive)")
    args = ap.parse_args()
    out = Path(args.out).resolve()          # BEFORE record() changes directory into --root
    doc = asyncio.run(record(Path(args.root).resolve(), args.server_commit))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"recorded {len(doc['exchanges'])} exchanges from {doc['provenance']['client']} -> {out}")


if __name__ == "__main__":
    main_cli()
