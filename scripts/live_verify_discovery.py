#!/usr/bin/env python3
"""Live verification of the discovery documents, against the DEPLOYED server (verdict item A8 and release 1's leftovers).

    python scripts/live_verify_discovery.py [--base https://api.hatchloop.dev] [--site https://hatchloop.dev]
                                            [--out receipt.json] [--only prm,card,documents,glama,x402,not_implemented]

Read-only: public GETs and one `server/discover` / `tools/list` (no tool call, no message, no key). Nothing here prints a
secret. Exit 0 only when every requested check passes.

  prm              the OAuth protected-resource document of every door, asked on the SITE host, names the SITE URL as its
                   `resource` (RFC 9728: it must equal the URL the client connected to), and the API host names itself.
                   This is the check that fails until deploy/caddy/oauth_prm.py is applied (the Next.js rewrite replaces Host).
  card             /.well-known/mcp/server-card.json on both hosts: the required fields, the versions `server/discover`
                   answers, the tools `tools/list` serves, the endpoint /.well-known/mcp.json names, and every OAuth URL it
                   names resolves and describes that endpoint.
  documents        llms.txt, /.well-known/mcp.json and the discovery card say how to sign in and which protocol versions
                   the server speaks, and say the same thing as the card.
  glama            /.well-known/glama.json is either a 404 (no claim token is configured: honest) or exactly
                   {"$schema", "claim"} with a well-formed token, on both hosts.
  x402             /.well-known/x402.json is the same answer as /.well-known/x402: the same status, and byte-identical
                   where that status is 200 (two 404s are the same answer whatever their bodies say).
  not_implemented  mpp and payment-manifest stay 404: protocols we do not implement are not aliased to anything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

UA = "hatchloop-live-verify/1"
PRM = "/.well-known/oauth-protected-resource"
CARD = "/.well-known/mcp/server-card.json"
GLAMA_RE = re.compile(r"^glama_claim_[A-Za-z0-9_-]{32}$")
ASSISTANTS = ("claude", "chatgpt", "openai", "grok", "muse", "gemini", "copilot", "cursor")


def http(method: str, url: str, *, body: Any = None, timeout: float = 30.0) -> tuple:
    """(status, headers(lower-case dict), text, raw bytes). Never raises on an HTTP error status."""
    h = {"User-Agent": UA, "Accept": "application/json, */*"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, raw.decode("utf-8", "replace"), raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, {k.lower(): v for k, v in e.headers.items()}, raw.decode("utf-8", "replace"), raw


def get_json(url: str) -> tuple:
    status, headers, text, _raw = http("GET", url)
    try:
        return status, headers, json.loads(text)
    except ValueError:
        return status, headers, None


def rpc(url: str, method: str, params: Optional[dict] = None) -> Optional[dict]:
    _s, _h, text, _r = http("POST", url, body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    try:
        return json.loads(text)
    except ValueError:
        return None


def check(ok: bool, **evidence: Any) -> dict:
    return {"ok": bool(ok), **evidence}


def _doors(base: str) -> list:
    status, _h, doc = get_json(f"{base}/.well-known/mcp.json")
    if status != 200 or not isinstance(doc, dict):
        return []
    return sorted(e["name"] for e in doc.get("capability_endpoints", []))


# ---------------------------------------------------------------------------------------------- checks

def check_prm(ctx: dict) -> dict:
    base, site = ctx["base"], ctx["site"]
    host = urlsplit(site).netloc
    api_host = urlsplit(base).netloc
    rows = []
    for path in ["/mcp/agent-broker"] + [f"/mcp/{d}" for d in ctx["doors"]] + [""]:
        s_status, _h, s_doc = get_json(f"{site}{PRM}{path}")
        want_site = f"https://{host}{path}"
        a_path = path if path != "/mcp/agent-broker" else "/mcp"
        a_status, _h2, a_doc = get_json(f"{base}{PRM}{a_path}")
        want_api = f"https://{api_host}{a_path}"
        # the full server's site URL exists only on the site, so the API host answers the origin path
        rows.append({
            "path": path or "(bare)",
            "site_status": s_status, "site_resource": (s_doc or {}).get("resource"), "site_ok": s_status == 200 and (s_doc or {}).get("resource") == want_site,
            "api_status": a_status, "api_resource": (a_doc or {}).get("resource"), "api_ok": a_status == 200 and (a_doc or {}).get("resource") == want_api,
        })
    bad = [r["path"] for r in rows if not (r["site_ok"] and r["api_ok"])]
    return check(not bad and len(rows) >= 3, wrong_on_the_site_host=[r["path"] for r in rows if not r["site_ok"]],
                 wrong_on_the_api_host=[r["path"] for r in rows if not r["api_ok"]], rows=rows,
                 hint="a door that names the API host on the site means deploy/caddy/oauth_prm.py is not applied" if bad else "")


def check_card(ctx: dict) -> dict:
    base, site = ctx["base"], ctx["site"]
    out: dict = {"hosts": {}}
    problems: list = []
    discover = rpc(f"{base}/mcp", "server/discover") or {}
    discovered = (discover.get("result") or {}).get("supportedVersions")
    listed = [t.get("name") for t in ((rpc(f"{base}/mcp", "tools/list") or {}).get("result") or {}).get("tools", [])]
    mcp_json = get_json(f"{base}/.well-known/mcp.json")[2] or {}
    endpoint = (mcp_json.get("transport") or {}).get("endpoint")
    cards = {}
    for label, host in (("api", base), ("site", site)):
        status, headers, card = get_json(f"{host}{CARD}")
        out["hosts"][label] = {"status": status, "content_type": headers.get("content-type", "")}
        if status != 200 or not isinstance(card, dict):
            problems.append(f"{label}: status {status}")
            continue
        cards[label] = card
        for k in ("$schema", "version", "protocolVersion", "serverInfo", "transport", "capabilities"):
            if k not in card:
                problems.append(f"{label}: missing {k}")
        if card.get("supportedProtocolVersions") != discovered:
            problems.append(f"{label}: versions differ from server/discover")
        if [t.get("name") for t in card.get("tools", [])] != listed:
            problems.append(f"{label}: tools differ from tools/list")
        if (card.get("transport") or {}).get("endpoint") != endpoint:
            problems.append(f"{label}: endpoint differs from /.well-known/mcp.json")
        blob = json.dumps(card).lower()
        for word in ("credit", "x402", "stripe", "polar", "checkout") + ASSISTANTS:
            if word in blob:
                problems.append(f"{label}: the card carries {word!r}")
    if len(cards) == 2 and cards["api"] != cards["site"]:
        problems.append("the two hosts serve different cards")
    card = cards.get("api") or {}
    oauth = ((card.get("authentication") or {}).get("oauth2")) or None
    out["oauth_in_card"] = oauth is not None
    if oauth:
        prm_status, _h, prm = get_json(oauth["protected_resource_metadata_url"])
        as_status, _h2, asm = get_json(oauth["authorization_server_metadata_url"])
        if prm_status != 200 or (prm or {}).get("resource") != endpoint:
            problems.append("the card's protected-resource URL does not describe the card's endpoint")
        if as_status != 200 or (asm or {}).get("issuer") != oauth.get("authorization_server"):
            problems.append("the card's authorization-server URL does not name the card's issuer")
    out["problems"] = problems
    out["tools"] = len(card.get("tools", []))
    return check(not problems, **out)


def check_documents(ctx: dict) -> dict:
    base = ctx["base"]
    problems: list = []
    llms = http("GET", f"{base}/llms.txt")[2]
    mcp_json = get_json(f"{base}/.well-known/mcp.json")[2] or {}
    svc = get_json(f"{base}/.well-known/agent-service")[2] or {}
    card = get_json(f"{base}{CARD}")[2] or {}
    oauth_card = (card.get("authentication") or {}).get("oauth2")
    oauth_mcp = (mcp_json.get("auth") or {}).get("oauth2")
    oauth_svc = (svc.get("auth") or {}).get("oauth2")
    if oauth_card is not None:
        if oauth_mcp != oauth_card:
            problems.append("mcp.json and the card describe the sign-in differently")
        if oauth_svc != oauth_card:
            problems.append("the discovery card and the server card describe the sign-in differently")
        if "Sign in with OAuth" not in llms or oauth_card["protected_resource_metadata_url"] not in llms:
            problems.append("llms.txt does not explain the sign-in")
    elif any((oauth_mcp, oauth_svc)) or "oauth" in llms.lower():
        problems.append("a document mentions a sign-in the card does not")
    versions = card.get("supportedProtocolVersions") or []
    if not versions or (mcp_json.get("protocol_versions") or {}).get("supported") != versions:
        problems.append("mcp.json protocol_versions differ from the card")
    for v in versions:
        if v not in llms:
            problems.append(f"llms.txt omits protocol version {v}")
    if "server/discover" not in llms:
        problems.append("llms.txt does not mention server/discover")
    return check(not problems, problems=problems, oauth_documents=bool(oauth_card), protocol_versions=versions)


def _glama_one(host: str) -> dict:
    status, headers, text, _raw = http("GET", f"{host}/.well-known/glama.json")
    if status == 404:
        return {"status": 404, "ok": True, "claim": "none configured"}
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    ok = (status == 200 and isinstance(doc, dict) and set(doc) == {"$schema", "claim"}
          and doc["$schema"] == "https://glama.ai/mcp/schemas/connector.json" and bool(GLAMA_RE.fullmatch(str(doc["claim"])))
          and headers.get("content-type", "").startswith("application/json"))
    return {"status": status, "ok": ok, "claim": "served" if ok else "MALFORMED", "token_length": len(str((doc or {}).get("claim", "")))}


def check_glama(ctx: dict) -> dict:
    hosts = {"api": _glama_one(ctx["base"]), "site": _glama_one(ctx["site"])}
    same = hosts["api"]["status"] == hosts["site"]["status"]
    return check(all(h["ok"] for h in hosts.values()) and same, hosts=hosts,
                 note="404 on both is the correct state until the founder reads the claim token off Glama's panel")


def check_x402(ctx: dict) -> dict:
    rows = {}
    for label, host in (("api", ctx["base"]), ("site", ctx["site"])):
        a = http("GET", f"{host}/.well-known/x402")
        b = http("GET", f"{host}/.well-known/x402.json")
        # What a scanner acts on: the status always, and the bytes where there is a document. A 404 carries no claim,
        # so two 404s are the same answer whatever their bodies say (the primary names the host, the framework does not).
        identical = a[0] == b[0] and (a[0] != 200 or a[3] == b[3])
        rows[label] = {"x402": a[0], "x402_json": b[0], "identical": identical}
    ok = all(r["identical"] and r["x402"] in (200, 404) for r in rows.values())
    return check(ok, rows=rows, note="404 on both is correct while the rail is off or this build has no x402 document")


def check_not_implemented(ctx: dict) -> dict:
    paths = ["/.well-known/mpp", "/.well-known/mpp.json", "/.well-known/payment-manifest", "/.well-known/payment-manifest.json"]
    rows = {f"{lab}{p}": http("GET", f"{host}{p}")[0] for lab, host in (("api", ctx["base"]), ("site", ctx["site"])) for p in paths}
    return check(all(s == 404 for s in rows.values()), rows=rows)


CHECKS: dict = {
    "prm": check_prm, "card": check_card, "documents": check_documents, "glama": check_glama, "x402": check_x402,
    "not_implemented": check_not_implemented,
}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="https://api.hatchloop.dev")
    ap.add_argument("--site", default="https://hatchloop.dev")
    ap.add_argument("--out", help="write the receipt here")
    ap.add_argument("--only", default=",".join(CHECKS))
    args = ap.parse_args(argv)
    wanted = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}")
        return 2
    ctx = {"base": args.base.rstrip("/"), "site": args.site.rstrip("/")}
    doors_error = ""
    try:
        ctx["doors"] = _doors(ctx["base"])
    except Exception as exc:  # noqa: BLE001 - an unreachable host is a failed check with its reason, not a traceback
        ctx["doors"] = []
        doors_error = f"{type(exc).__name__}: {exc}"[:300]
    receipt: dict = {"checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "base": ctx["base"], "site": ctx["site"],
                     "doors": ctx["doors"], "results": {}}
    if doors_error:
        receipt["doors_error"] = doors_error
    for name in wanted:
        t0 = time.time()
        try:
            res = CHECKS[name](ctx)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check, with its reason
            res = check(False, error=f"{type(exc).__name__}: {exc}"[:300])
        res["seconds"] = round(time.time() - t0, 1)
        receipt["results"][name] = res
        print(f"[{'PASS' if res['ok'] else 'FAIL'}] {name} ({res['seconds']} s)")
        if not res["ok"]:
            print("       " + json.dumps({k: v for k, v in res.items() if k not in ('ok', 'seconds', 'rows')})[:600])
    receipt["all_passed"] = all(r["ok"] for r in receipt["results"].values())
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(receipt, fh, indent=2)
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
