"""Post-deploy probe for MCP 2026-07-28: eight read-only JSON-RPC checks against a running server.

    python scripts/probe_mcp_2026.py                       # api.hatchloop.dev
    python scripts/probe_mcp_2026.py --base https://hatchloop.dev/mcp/agent-broker --door-base https://hatchloop.dev/mcp

Exit 0 only if every check passes. NOTHING HERE PLACES A TOOL CALL OR SENDS A MESSAGE: it asks the server what
it speaks (server/discover, tools/list, resources/templates/list, initialize) and sends deliberately malformed
envelopes to see that they are refused the way the revision says. That is the same discipline as
deploy/caddy/mcp_direct.post_checks ("a probe must never place a call").

The user agent is `hatchloop-mcp-2026-probe/1`. NOTHING in this repository classes it as our own traffic: the
outcome log files these keyless discovery requests as `crawler`, and HatchLoop's traffic audit
(projects/hatchloop/scripts/mcp_traffic_audit.py) recognises its own senders by EXACT string in
OWN_INFRA_UA_EXACT. Register that string there before the first deploy-time run, or each run adds eight
modern-era requests to the very demand metric this change is judged by.

WHY IT EXISTS. The unit tests prove the code; this proves the DEPLOYED thing - the container the gated wrapper
swapped in, behind Caddy, answering with the HTTP statuses (400 / 404) and headers the tests asserted in-process.
The injectable `send` is how tests/unit/test_probe_mcp_2026.py runs the same eight checks against the app
without a network.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

M = "io.modelcontextprotocol/"
V = "2026-07-28"
UA = "hatchloop-mcp-2026-probe/1"
ENVELOPE = {M + "protocolVersion": V, M + "clientInfo": {"name": "hatchloop-probe", "version": "1"},
            M + "clientCapabilities": {}}

# send(url, headers, body) -> (http_status, parsed_json_or_None)
Send = Callable[[str, dict, dict], tuple]


def _http_send(url: str, headers: dict, body: dict) -> tuple:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          "User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _rpc(method: str, params: Optional[dict] = None, rid=1) -> dict:
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _modern(method: str, **headers) -> dict:
    return {"MCP-Protocol-Version": V, "Mcp-Method": method, **headers}


def run_checks(send: Send, base: str, door_base: str, door: str = "sanctions-screening") -> list:
    """[(name, passed, detail)] - never raises."""
    out = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not crash
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        out.append((name, bool(ok), detail))

    def discover():
        st, doc = send(base, _modern("server/discover"), _rpc("server/discover", {"_meta": ENVELOPE}, "d1"))
        res = (doc or {}).get("result") or {}
        ok = (st == 200 and res.get("resultType") == "complete" and res.get("supportedVersions", [None])[0] == V
              and isinstance(res.get("ttlMs"), int) and res.get("cacheScope") == "public"
              and bool(res.get("capabilities")) and "serverInfo" in str(res.get("_meta")))
        return ok, f"HTTP {st}, versions {res.get('supportedVersions')}"

    def discover_door():
        st, doc = send(f"{door_base.rstrip('/')}/{door}", _modern("server/discover"),
                       _rpc("server/discover", {"_meta": ENVELOPE}, "d2"))
        name = (((doc or {}).get("result") or {}).get("_meta") or {}).get(M + "serverInfo", {}).get("name")
        return st == 200 and name == door, f"HTTP {st}, serverInfo.name={name!r}"

    def discover_bogus_version():
        st, doc = send(base, {"MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "server/discover"},
                       _rpc("server/discover", None, "d3"))
        err = (doc or {}).get("error") or {}
        sup = (err.get("data") or {}).get("supported") or []
        return st == 400 and err.get("code") == -32022 and V in sup, f"HTTP {st}, code {err.get('code')}"

    def tools_list_modern():
        st, doc = send(base, _modern("tools/list"), _rpc("tools/list", {"_meta": ENVELOPE}, "t1"))
        res = (doc or {}).get("result") or {}
        ok = st == 200 and res.get("resultType") == "complete" and len(res.get("tools", [])) >= 20 \
            and isinstance(res.get("ttlMs"), int) and res.get("cacheScope") in ("public", "private")
        return ok, f"HTTP {st}, {len(res.get('tools', []))} tools"

    def header_mismatch():
        st, doc = send(base, _modern("tools/call"), _rpc("tools/list", {"_meta": ENVELOPE}, "t2"))
        code = ((doc or {}).get("error") or {}).get("code")
        return st == 400 and code == -32020, f"HTTP {st}, code {code}"

    def unknown_method():
        st, doc = send(base, _modern("no/such/method"), _rpc("no/such/method", {"_meta": ENVELOPE}, "u1"))
        code = ((doc or {}).get("error") or {}).get("code")
        return st == 404 and code == -32601, f"HTTP {st}, code {code}"

    def templates_list():
        st, doc = send(base, {}, _rpc("resources/templates/list", None, "r1"))
        res = (doc or {}).get("result") or {}
        return st == 200 and res.get("resourceTemplates") == [], f"HTTP {st}"

    def legacy_initialize():
        st, doc = send(base, {"MCP-Protocol-Version": "2025-06-18"}, _rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "hatchloop-probe", "version": "1"}}, "i1"))
        res = (doc or {}).get("result") or {}
        ok = st == 200 and res.get("protocolVersion") == "2025-06-18" and "resultType" not in res \
            and "serverInfo" in res
        return ok, f"HTTP {st}, protocolVersion {res.get('protocolVersion')}"

    check("server/discover, full envelope", discover)
    check(f"server/discover on the {door} door names the door", discover_door)
    check("server/discover with version 1999-01-01 -> 400 / -32022 listing 2026-07-28", discover_bogus_version)
    check("tools/list in the 2026-07-28 shape", tools_list_modern)
    check("Mcp-Method that disagrees with the body -> 400 / -32020", header_mismatch)
    check("unknown method in the 2026-07-28 envelope -> 404 / -32601", unknown_method)
    check("resources/templates/list -> 200 and honestly empty", templates_list)
    check("legacy initialize still negotiates the legacy version, unchanged shape", legacy_initialize)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="https://api.hatchloop.dev/mcp", help="the full-server MCP URL")
    ap.add_argument("--door-base", default="https://api.hatchloop.dev/mcp", help="URL prefix of the capability doors")
    args = ap.parse_args(argv)
    results = run_checks(_http_send, args.base, args.door_base)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} ({detail})")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
