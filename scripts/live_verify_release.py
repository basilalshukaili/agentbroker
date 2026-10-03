#!/usr/bin/env python3
"""Live verification of the 2026-10-03 "fix what we have" release, against the DEPLOYED server.

    python scripts/live_verify_release.py --expect-commit <sha> [--base https://api.hatchloop.dev]
                                           [--site https://hatchloop.dev] [--env-file PATH] [--out receipt.json]
                                           [--only health,mcp2026,legacy,oauth,sanctions,find_business,labels,retired,payments]

What it checks, in this order (each is a function returning {ok, ...evidence}; nothing here prints a secret):

  health         /health reports the commit that was deployed.
  mcp2026        scripts/probe_mcp_2026.py: server/discover, supported versions, the 400/404 refusals, legacy initialize
                 unchanged - on the API host and on the site's /mcp door.
  legacy         the OFFICIAL Python SDK client (an old-protocol client): initialize, list tools, call a keyless tool.
  oauth          discovery documents; the 401 for a known connector and the readable answer for everyone else; then
                 the whole Connect flow with an address we control that is NOT a person: Resend's `delivered+tag@resend.dev`
                 test inbox. The sign-in email is read back through Resend's API (the account's own key, from --env-file),
                 the link is opened, Confirm is pressed in the same cookie jar, the code is exchanged with PKCE, the
                 access token is used at /mcp, refreshed, and the spent refresh token is shown to be dead.
  sanctions      screen_sanctions with a published Arabic alias is a finding; its Latin spelling variant is a candidate;
                 an ordinary English name is clean.
  find_business  the fixed measurement sample (scripts/measure_find_business.py) over HTTP: outcome classes, cold p50/p95.
  labels         tools/list carries the [beta]/[limited] markers the readiness table says, and the keyless count is the
                 one the manifests print.
  retired        the six retired doors answer MCP with a tombstone on the site host, and GET is 410.
  payments       the payment claims agree: the descriptor's status and rails, /.well-known/x402, the tool descriptions,
                 the auth_required options, /keys/request and preview_cost for a premium data tool tell one story
                 (nothing offers a rail the descriptor does not list; every listed rail is offered).

Exit 0 only when every requested check passes. Calls place no paid operation and send no message to a person.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

UA = "hatchloop-live-verify/1"
M = "io.modelcontextprotocol/"
V = "2026-07-28"
LEGACY_V = "2025-06-18"
SIX = ["ai-visibility", "data-enrichment", "driftwatch", "email-sending", "pdf-generator", "url-shortener"]
# A published EU/UK Arabic-script alias (the party is on both lists) and one of its Latin spellings.
ARABIC_ALIAS = "حامد عبد الله أحمد العلي"
ARABIC_LATIN_VARIANT = "Hamed Abdullah Ahmed Al-Ali"


# ---------------------------------------------------------------------------------------------- plumbing

def http(method: str, url: str, *, body: Any = None, headers: Optional[dict] = None, timeout: float = 40.0,
         raw: Optional[bytes] = None) -> tuple:
    """(status, headers(lower-case dict), text). Never raises on an HTTP error status."""
    h = {"User-Agent": UA, "Accept": "application/json, text/event-stream, */*"}
    h.update(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")


def rpc(url: str, method: str, params: Optional[dict] = None, *, headers: Optional[dict] = None, rid: int = 1,
        timeout: float = 60.0) -> tuple:
    status, hdrs, text = http("POST", url, body={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                              headers=headers, timeout=timeout)
    try:
        doc = json.loads(text) if text.strip() else None
    except ValueError:
        doc = None
    return status, hdrs, doc


def tool_body(doc: Optional[dict]) -> dict:
    try:
        return json.loads(doc["result"]["content"][0]["text"])
    except Exception:  # noqa: BLE001
        return {}


def read_env(path: Optional[str], name: str) -> str:
    if not path:
        return ""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def pkce() -> tuple:
    verifier = secrets.token_urlsafe(48)[:64]
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def check(ok: bool, **evidence: Any) -> dict:
    return {"ok": bool(ok), **evidence}


# ---------------------------------------------------------------------------------------------- checks

def check_health(ctx: dict) -> dict:
    status, _, text = http("GET", ctx["base"] + "/health")
    doc = json.loads(text) if status == 200 else {}
    return check(status == 200 and doc.get("build_commit") == ctx["expect"], status=status,
                 build_commit=doc.get("build_commit"), expected=ctx["expect"], health=doc.get("status"),
                 degraded=doc.get("degraded"))


def check_mcp2026(ctx: dict) -> dict:
    out = {}
    runs = {"api": [sys.executable, str(HERE / "probe_mcp_2026.py"), "--base", ctx["base"] + "/mcp"],
            "site": [sys.executable, str(HERE / "probe_mcp_2026.py"), "--base", ctx["site"] + "/mcp/agent-broker",
                     "--door-base", ctx["site"] + "/mcp"]}
    for name, cmd in runs.items():
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
        out[name] = {"exit": p.returncode, "tail": lines[-3:]}
    # the revision's handshake, spelled out: discover, then a modern tools/list, then the legacy initialize
    st, _, d = rpc(ctx["base"] + "/mcp", "server/discover", {"_meta": {M + "protocolVersion": V}},
                   headers={"MCP-Protocol-Version": V, "Mcp-Method": "server/discover"})
    res = (d or {}).get("result") or {}
    out["discover"] = {"status": st, "supportedVersions": res.get("supportedVersions"), "resultType": res.get("resultType")}
    st2, _, t = rpc(ctx["base"] + "/mcp", "tools/list",
                    {"_meta": {M + "protocolVersion": V, M + "clientCapabilities": {}}},
                    headers={"MCP-Protocol-Version": V, "Mcp-Method": "tools/list"})
    tools = ((t or {}).get("result") or {}).get("tools") or []
    out["modern_tools_list"] = {"status": st2, "tools": len(tools), "resultType": ((t or {}).get("result") or {}).get("resultType")}
    st3, _, i = rpc(ctx["base"] + "/mcp", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "live-verify", "version": "1"}})
    out["legacy_initialize"] = {"status": st3, "protocolVersion": ((i or {}).get("result") or {}).get("protocolVersion")}
    ok = (all(r["exit"] == 0 for r in (out["api"], out["site"]))
          and st == 200 and res.get("supportedVersions", [None])[0] == V and res.get("resultType") == "complete"
          and st2 == 200 and len(tools) >= 20 and out["modern_tools_list"]["resultType"] == "complete"
          and st3 == 200 and out["legacy_initialize"]["protocolVersion"] in ("2025-06-18", "2025-03-26", "2024-11-05", "2025-11-25"))
    return check(ok, **out)


def check_legacy(ctx: dict) -> dict:
    """The official Python SDK client = a client that only knows the old protocol."""
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except Exception as exc:  # noqa: BLE001
        return check(False, error=f"mcp SDK not importable: {type(exc).__name__}")

    async def go():
        async with streamablehttp_client(ctx["base"] + "/mcp", headers={"User-Agent": UA}) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = (await session.list_tools()).tools
                quota = await session.call_tool("check_quota", {})
                return init, tools, quota
    init, tools, quota = asyncio.run(go())
    names = {t.name for t in tools}
    text = quota.content[0].text if quota.content else ""
    return check(len(tools) >= 20 and not quota.isError and "screen_sanctions" in names,
                 protocol=str(init.protocolVersion), tools=len(tools), quota_is_error=bool(quota.isError),
                 server=str(getattr(init.serverInfo, "name", "")), quota_text_chars=len(text))


def _resend_get(key: str, path: str) -> Any:
    st, _, text = http("GET", "https://api.resend.com" + path, headers={"Authorization": "Bearer " + key})
    if st != 200:
        raise RuntimeError(f"resend {path.split('?')[0]} -> {st}")
    return json.loads(text)


def check_oauth(ctx: dict) -> dict:
    base = ctx["base"]
    out: dict = {}
    # --- discovery ---
    st, _, t = http("GET", base + "/.well-known/oauth-protected-resource/mcp")
    prm = json.loads(t) if st == 200 else {}
    st2, _, t2 = http("GET", base + "/.well-known/oauth-authorization-server")
    asm = json.loads(t2) if st2 == 200 else {}
    st3, _, _ = http("GET", base + "/.well-known/openid-configuration")
    out["discovery"] = {"prm_status": st, "resource": prm.get("resource"), "authorization_servers": prm.get("authorization_servers"),
                        "asm_status": st2, "pkce": asm.get("code_challenge_methods_supported"),
                        "endpoints_ok": all(asm.get(k, "").startswith(base) for k in
                                            ("authorization_endpoint", "token_endpoint", "registration_endpoint")),
                        "cimd": asm.get("client_id_metadata_document_supported"), "openid_status": st3}
    disc_ok = (st == 200 and prm.get("resource") == base + "/mcp" and prm.get("authorization_servers") == [base]
               and st2 == 200 and asm.get("code_challenge_methods_supported") == ["S256"] and out["discovery"]["endpoints_ok"]
               and st3 == 200)
    # --- the challenge ---
    call = {"name": "get_conversation", "arguments": {}}
    s1, h1, d1 = rpc(base + "/mcp", "tools/call", call, headers={"User-Agent": "Claude-User"})
    s2, h2, d2 = rpc(base + "/mcp", "tools/call", call, headers={"User-Agent": "python-httpx/0.27.0"})
    s3, h3, d3 = rpc(base + "/mcp", "tools/call", {"name": "check_quota", "arguments": {}}, headers={"User-Agent": "Claude-User"})
    out["challenge"] = {"connector_status": s1, "www_authenticate": "Bearer" in (h1.get("www-authenticate") or ""),
                        "other_agent_status": s2, "other_agent_hint": bool((((d2 or {}).get("result") or {}).get("_meta") or {}).get("mcp/www_authenticate")),
                        "other_agent_code": tool_body(d2).get("error_code") or tool_body(d2).get("reason_code"),
                        "keyless_tool_status": s3, "keyless_tool_challenged": "www-authenticate" in h3}
    chal_ok = (s1 == 401 and out["challenge"]["www_authenticate"] and s2 == 200 and out["challenge"]["other_agent_hint"]
               and s3 == 200 and not out["challenge"]["keyless_tool_challenged"])
    out["discovery_ok"], out["challenge_ok"] = disc_ok, chal_ok
    if ctx["skip_email"]:
        return check(disc_ok and chal_ok, **out)

    # --- the whole Connect flow ---
    import httpx
    key = read_env(ctx["env_file"], "RESEND_API_KEY")
    if not key:
        return check(False, error="no RESEND_API_KEY in --env-file: the sign-in email cannot be read back", **out)
    tag = f"hl-verify-{int(time.time())}-{secrets.token_hex(3)}"
    address = f"delivered+{tag}@resend.dev"
    redirect = "http://127.0.0.1:8765/hatchloop-live-verify/callback"
    verifier, challenge = pkce()
    flow: dict = {"address_is_a_resend_test_inbox": True}
    with httpx.Client(base_url=base, follow_redirects=False, timeout=40.0, headers={"User-Agent": UA}) as browser:
        reg = browser.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "HatchLoop live verification",
                                                    "token_endpoint_auth_method": "none"})
        client_id = reg.json().get("client_id", "")
        flow["register"] = reg.status_code
        page = browser.get("/oauth/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "live-verify", "resource": base + "/mcp"})
        rid = re.search(r'name="rid" value="([^"]+)"', page.text)
        poll = re.search(r'name="poll_secret" value="([^"]+)"', page.text)
        flow["authorize_page"] = page.status_code
        flow["cookie_set_by_the_page"] = any(c.startswith("hl_oauth_") for c in browser.cookies.keys()) if hasattr(browser.cookies, "keys") else None
        if not (rid and poll):
            return check(False, flow=flow, **out)
        sent = browser.post("/oauth/authorize/email", data={"rid": rid.group(1), "poll_secret": poll.group(1), "email": address})
        flow["email_step"] = sent.status_code
        flow["email_step_says_check_inbox"] = "Check your email" in sent.text
        # a forged cross-site post must be refused (the review's P1) - same browser, so only the header is wrong
        forged = browser.post("/oauth/authorize/email", headers={"Sec-Fetch-Site": "cross-site"},
                              data={"rid": rid.group(1), "poll_secret": poll.group(1), "email": address})
        flow["cross_site_post_refused"] = forged.status_code == 403
        if sent.status_code != 200:
            return check(False, flow=flow, **out)
        # read the email back from Resend (the account's own key)
        link = None
        deadline = time.time() + 90
        while time.time() < deadline and not link:
            try:
                for item in _resend_get(key, "/emails?limit=20").get("data", []):
                    to = item.get("to") or []
                    if any(tag in str(a) for a in to):
                        full = _resend_get(key, "/emails/" + item["id"])
                        m = re.search(r"https://[^\s\"'<>]+/oauth/verify\?t=[A-Za-z0-9_-]+", (full.get("html") or "") + (full.get("text") or ""))
                        if m:
                            link = m.group(0).replace("&amp;", "&")
                            flow["email_subject"] = full.get("subject")
                            flow["email_from"] = full.get("from")
                            break
            except Exception as exc:  # noqa: BLE001
                flow["resend_read_error"] = type(exc).__name__
            if not link:
                time.sleep(4)
        flow["link_found"] = bool(link)
        if not link:
            return check(False, flow=flow, **out)
        magic = parse_qs(urlsplit(link).query)["t"][0]
        opened = browser.get("/oauth/verify", params={"t": magic})
        flow["verify_page"] = opened.status_code
        flow["verify_page_names_the_host_only"] = "127.0.0.1" in opened.text and "HatchLoop live verification" not in (full.get("html") or "")
        flow["asks_for_code"] = 'name="code"' in opened.text           # same browser: it must NOT
        done = browser.post("/oauth/verify", data={"t": magic, "decision": "approve"})
        flow["confirm_status"] = done.status_code
        loc = done.headers.get("location", "")
        q = parse_qs(urlsplit(loc).query)
        code = (q.get("code") or [""])[0]
        flow["redirected_to_the_registered_address"] = loc.startswith(redirect)
        flow["iss_present"] = (q.get("iss") or [""])[0] == base
        flow["state_echoed"] = (q.get("state") or [""])[0] == "live-verify"
        tok = browser.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                                 "client_id": client_id, "code_verifier": verifier, "resource": base + "/mcp"})
        tj = tok.json() if tok.status_code == 200 else {}
        flow["token_status"] = tok.status_code
        flow["token_type"] = tj.get("token_type")
        flow["expires_in"] = tj.get("expires_in")
        flow["has_refresh_token"] = bool(tj.get("refresh_token"))
        access = tj.get("access_token", "")
        # the token is a working Agent-Identity key at /mcp: a key-requiring tool no longer asks for sign-in
        s4, h4, d4 = rpc(base + "/mcp", "tools/call", {"name": "get_conversation", "arguments": {"reference": "live-verify", "business_number": "+15550001111"}},
                         headers={"Authorization": f"Bearer {access}", "User-Agent": "Claude-User"})
        body4 = tool_body(d4)
        flow["token_at_mcp"] = {"status": s4, "challenged": "www-authenticate" in h4,
                                "refused_for_identity": (body4.get("error_code") or body4.get("reason_code")) in ("identity_required", "auth_required")}
        # refresh rotates, and the spent token is dead
        r1 = browser.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tj.get("refresh_token", ""), "client_id": client_id})
        r1j = r1.json() if r1.status_code == 200 else {}
        flow["refresh_status"] = r1.status_code
        flow["refresh_rotated"] = bool(r1j.get("refresh_token")) and r1j.get("refresh_token") != tj.get("refresh_token")
        r2 = browser.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tj.get("refresh_token", ""), "client_id": client_id})
        flow["spent_refresh_token_dead"] = r2.status_code == 400
        # replaying the SPENT code is refused (and, by design, kills what it started - so it comes last)
        replay = browser.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                                    "client_id": client_id, "code_verifier": verifier})
        flow["code_replay_refused"] = replay.status_code == 400
        # revoke the chain we created (a clean test leaves nothing usable behind)
        browser.post("/oauth/revoke", data={"token": r1j.get("refresh_token") or "", "client_id": client_id})
    flow_ok = (flow.get("register") == 201 and flow.get("authorize_page") == 200 and flow.get("email_step") == 200
               and flow.get("cross_site_post_refused") and flow.get("link_found") and flow.get("confirm_status") == 303
               and not flow.get("asks_for_code") and flow.get("redirected_to_the_registered_address") and flow.get("iss_present")
               and flow.get("state_echoed") and flow.get("token_status") == 200 and flow.get("token_type", "").lower() == "bearer"
               and flow.get("has_refresh_token") and flow.get("code_replay_refused") and flow["token_at_mcp"]["status"] == 200
               and not flow["token_at_mcp"]["challenged"] and flow.get("refresh_rotated") and flow.get("spent_refresh_token_dead"))
    return check(disc_ok and chal_ok and flow_ok, flow=flow, **out)


def check_sanctions(ctx: dict) -> dict:
    base = ctx["base"]
    out: dict = {}

    def screen(name: str) -> dict:
        st, _, d = rpc(base + "/mcp", "tools/call", {"name": "screen_sanctions", "arguments": {"name": name}}, timeout=90)
        return {"status": st, "body": tool_body(d)}

    a = screen(ARABIC_ALIAS)
    matches = a["body"].get("matches") or (a["body"].get("result") or {}).get("matches") or []
    res = a["body"].get("result") if isinstance(a["body"].get("result"), dict) else a["body"]
    matches = res.get("matches") or []
    out["arabic_alias"] = {"status": a["status"], "matched": res.get("matched"), "screening_status": res.get("screening_status"),
                           "bases": sorted({m.get("match_basis") for m in matches}),
                           "lists": sorted({str(m.get("list", ""))[:12] for m in matches})}
    v = screen(ARABIC_LATIN_VARIANT)
    res_v = v["body"].get("result") if isinstance(v["body"].get("result"), dict) else v["body"]
    cands = res_v.get("possible_matches_unverified") or []
    out["latin_variant"] = {"status": v["status"], "matched": res_v.get("matched"), "screening_status": res_v.get("screening_status"),
                            "candidate_confidences": sorted({c.get("match_confidence") for c in cands + (res_v.get("matches") or [])
                                                             if c.get("match_confidence")}),
                            "has_alignment": any(c.get("token_alignment") for c in cands + (res_v.get("matches") or []))}
    e = screen("David Evans")
    res_e = e["body"].get("result") if isinstance(e["body"].get("result"), dict) else e["body"]
    out["ordinary_english_name"] = {"status": e["status"], "matched": res_e.get("matched"), "screening_status": res_e.get("screening_status"),
                                    "arabic_matching_applied": bool(res_e.get("arabic_matching"))}
    m = screen(ARABIC_ALIAS + " Smith")
    res_m = m["body"].get("result") if isinstance(m["body"].get("result"), dict) else m["body"]
    out["alias_plus_latin_word"] = {"status": m["status"], "matched": res_m.get("matched"),
                                    "exact_findings": [x for x in (res_m.get("matches") or []) if x.get("match_basis") == "arabic_script_exact"]}
    ok = (a["status"] == 200 and res.get("matched") is True and "arabic_script_exact" in out["arabic_alias"]["bases"]
          and out["latin_variant"]["candidate_confidences"] and out["latin_variant"]["has_alignment"]
          and res_e.get("matched") is False and not out["ordinary_english_name"]["arabic_matching_applied"]
          and res_m.get("matched") is False and not out["alias_plus_latin_word"]["exact_findings"])
    out["alias_plus_latin_word"].pop("exact_findings")
    return check(ok, **out)


def check_find_business(ctx: dict) -> dict:
    import measure_find_business as mfb
    base = ctx["base"]
    rows: list = []
    lat_cold: list = []

    def one(label: str, args: dict, rid: int) -> tuple:
        time.sleep(ctx.get("pace", 0.0))
        t0 = time.monotonic()
        st, _, d = rpc(base + "/mcp", "tools/call", {"name": "find_business", "arguments": args}, rid=rid, timeout=90)
        dt = time.monotonic() - t0
        row = mfb.apply_place_check(mfb.classify(d if isinstance(d, dict) else {}), args)
        row.update({"label": label, "seconds": round(dt, 2), "http": st})
        return dt, row

    for i, (label, args, _recon) in enumerate(mfb.OUTCOME_SAMPLE):
        dt, row = one(label, args, i + 1)
        rows.append(row)
    classes: dict = {}
    for r in rows:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    for i, (label, args, _recon) in enumerate(mfb.LATENCY_SAMPLE):
        dt, row = one(label, args, 100 + i)
        lat_cold.append(dt)
        row["phase"] = "latency"
        rows.append(row)
    budget = 5.0
    p50, p95, worst = mfb.percentile(lat_cold, 50), mfb.percentile(lat_cold, 95), round(max(lat_cold), 2)
    carried = [r for r in rows[:len(mfb.OUTCOME_SAMPLE)] if not r["label"].startswith("no-args")]
    usable = sum(1 for r in carried if r["class"] in ("complete", "partial"))
    unavailable = sum(1 for r in carried if r["class"] == "unavailable")
    wrong = sum(1 for r in rows if r["class"] == "wrong_place")
    bare = sum(1 for r in rows if r["class"] == "bare_error")
    unclassified = sum(1 for r in rows if r["class"] == "unclassified")
    noarg_guided = sum(1 for r in rows if r["label"].startswith("no-args") and r["class"] == "guided_error")
    # What the DEPLOYED CODE controls: it answers inside its budget, never for the wrong place, never with a bare
    # error, and every call that carried a place and a kind got a usable answer or an honest 'upstream unavailable'
    # (the public OpenStreetMap servers rate-limit and fail; that is reported, not hidden, and not ours to pass).
    ok = (p95 is not None and p95 <= budget + 1.5 and wrong == 0 and bare == 0 and unclassified == 0
          and noarg_guided == 11 and usable + unavailable == len(carried))
    return check(ok, outcome_classes=classes, usable_of_calls_with_a_place_and_a_kind=f"{usable}/{len(carried)}",
                 honest_upstream_unavailable=unavailable, wrong_place=wrong, bare_errors=bare, unclassified=unclassified,
                 no_argument_calls_with_a_worked_example=f"{noarg_guided}/11",
                 latency_calls=len(lat_cold), latency_p50_s=p50, latency_p95_s=p95, latency_max_s=worst, budget_s=budget,
                 note="p95 allows 1.5 s of network on top of the 5 s server-side budget")


def check_labels(ctx: dict) -> dict:
    """The labels a caller sees are the labels the manifest stores, and a label is never typed in two places."""
    from core import tool_auth, tool_readiness
    manifest = json.loads((ROOT / "manifest" / "manifest.json").read_text(encoding="utf-8"))
    stored = tool_readiness.all_labelled(manifest.get("operations", []))
    st, _, d = rpc(ctx["base"] + "/mcp", "tools/list")
    tools = ((d or {}).get("result") or {}).get("tools") or []
    severity = {"beta": 1, "limited": 2, "unavailable": 3}
    live: dict = {}
    problems: list = []
    for t in tools:
        desc = str(t.get("description", ""))
        m = re.search(r" \[(beta|limited|unavailable)\]$", desc)
        meta_state = (((t.get("_meta") or {}).get(tool_readiness.META_KEY)) or {}).get("state")
        shown = m.group(1) if m else None
        # the delivery-channel tools carry the state as a sentence in brackets (core/channel_status.annotate_tools)
        if shown is None and desc.startswith("[UNAVAILABLE on this deployment"):
            shown = "unavailable"
        if shown is None and "[Not available on this deployment" in desc:
            shown = "beta"
        if shown:
            live[t["name"]] = shown
        if shown != meta_state:
            problems.append((t["name"], "label and _meta disagree", shown, meta_state))
    for name, state in stored.items():
        if name not in live or severity[live[name]] < severity[state]:
            problems.append((name, "stored label missing or weaker live", state, live.get(name)))
    keyless = sum(1 for t in tools if not tool_auth.requires_key(t["name"]))
    ok = st == 200 and len(tools) == tool_auth.total_tools() and not problems and keyless == 14 and len(stored) >= 5
    return check(ok, tools=len(tools), live_labels=live, stored_labels=stored, problems=problems,
                 keyless_tools_in_the_live_list=keyless, expected_total=tool_auth.total_tools())


def check_retired(ctx: dict) -> dict:
    site = ctx["site"]
    out: dict = {}
    ok = True
    for slug in SIX:
        gs, gh, gt = http("GET", f"{site}/mcp/{slug}")
        ps, _, init = rpc(f"{site}/mcp/{slug}", "initialize", {"protocolVersion": LEGACY_V, "capabilities": {},
                                                               "clientInfo": {"name": "live-verify", "version": "1"}})
        ts, _, tl = rpc(f"{site}/mcp/{slug}", "tools/list")
        cs, _, call = rpc(f"{site}/mcp/{slug}", "tools/call", {"name": "server_retired", "arguments": {}})
        name = ((init or {}).get("result") or {}).get("serverInfo", {}).get("name", "")
        tools = [t.get("name") for t in (((tl or {}).get("result") or {}).get("tools") or [])]
        body = tool_body(call)
        row_ok = (gs == 410 and ps == 200 and "RETIRED" in name and tools == ["server_retired"]
                  and cs == 200 and body.get("error") == "server_retired")
        out[slug] = {"GET": gs, "initialize": ps, "server_name_says_retired": "RETIRED" in name, "tools": tools, "call": cs}
        ok = ok and row_ok
    ds, _, dd = rpc(f"{site}/mcp/email-sending", "server/discover")
    out["modern_discover_on_a_tombstone"] = {"status": ds, "retired_in_name": "RETIRED" in json.dumps(dd or {})}
    return check(ok and ds == 200 and out["modern_discover_on_a_tombstone"]["retired_in_name"], **out)


# The three premium data tools: answered free before any billing branch while DATA_METERING_ENABLED is off.
_PREMIUM_DATA_TOOLS = ("screen_sanctions", "verify_company_record", "map_trade_restriction")
# A cost tag in a tool description that quotes a price: "[$0.05/per_call...]", "[from $0.02/call...]",
# "[free in quota, then $0.02/call...]".
_PRICE_TAG = re.compile(r"\[(?:\$|from \$|free in quota, then \$)[^\]]*\]")


def payments_problems(*, descriptor: dict, x402_status: int, tools_status: int, tool_descriptions: dict,
                      auth_text: str, keys_status: int, keys_text: str,
                      screen_sanctions_cost_usd: Optional[float], expect_rails: Optional[list] = None) -> list:
    """Do the server's claims about how to pay tell ONE story? Pure, so it is tested without a network.

    The switches (CREDITS_ENABLED, DATA_METERING_ENABLED, X402_ENABLED) cannot be read from outside, but every
    surface that mentions them can: the descriptor's rails and status, /.well-known/x402, each tool description,
    the auth_required text, /keys/request, and preview_cost for a premium data tool. If the descriptor says no
    rail is on, nothing else may offer one; if it says a rail is on, the rest must offer it; if it says the daily
    quota is not enforced, no tool may promise one; and while no rail is on no price may be quoted as a charge.

    EVIDENCE THAT CANNOT BE READ IS A PROBLEM, NEVER A PASS. Every argument is required and keyword-only: a
    surface that answered 500, an empty tools/list, an unreadable preview_cost all used to become "" or None and
    match "nothing offers a rail" (review of feat/x402-honesty-20261004, F6).

    Agreement is not truth: every surface can agree on a rail that is off (credits were advertised everywhere
    while CREDITS_ENABLED was off). `expect_rails`, when the operator passes it (--expect-rails), is the intended
    state taken from what was staged for the container; the descriptor must name exactly those rails.
    """
    from billing.switches import NOT_CHARGED
    status, rails = descriptor.get("status"), descriptor.get("rails")
    if not status or not isinstance(rails, list):
        return ["payments.status / payments.rails are missing from /.well-known/mcp.json"]
    problems: list = []
    if expect_rails is not None and sorted(rails) != sorted(expect_rails):
        problems.append(f"the descriptor lists rails {sorted(rails)} but {sorted(expect_rails)} were expected")
    if (status == "active") != bool(rails):
        problems.append(f"payments.status is {status!r} but rails is {rails}")
    quota = descriptor.get("premium_data_quota_enforced")
    if not isinstance(quota, bool):
        problems.append("payments.premium_data_quota_enforced is missing or not a boolean")
    credits_on, x402_on = "credits" in rails, "x402" in rails

    # ---- the evidence has to be there before it can agree with anything
    descs = tool_descriptions if (tools_status == 200 and isinstance(tool_descriptions, dict)) else {}
    if not descs:
        problems.append(f"tools/list did not return a tool list (HTTP {tools_status}), so the tool descriptions "
                        "could not be judged")
    if keys_status != 200 or not (keys_text or "").strip():
        problems.append(f"/keys/request did not answer 200 with guidance (HTTP {keys_status}), so its text could "
                        "not be judged")
    if "Option 1" not in (auth_text or ""):
        problems.append("the auth_required text carries no option list (an anonymous send_message was not refused "
                        "for identity), so the offered options could not be read")
    tools_text = " ".join(str(d) for d in descs.values())

    def says_x402(text: str) -> bool:
        return "x402" in (text or "").lower()

    surfaces = (("a tool description", tools_text), ("the auth_required text", auth_text),
                ("/keys/request", keys_text))
    if x402_on:
        if x402_status != 200:
            problems.append(f"x402 is a listed rail but /.well-known/x402 answered {x402_status}")
        for label, text in surfaces:
            if not says_x402(text):
                problems.append(f"x402 is a listed rail but {label} never offers it")
    else:
        if x402_status != 404:
            problems.append(f"/.well-known/x402 answered {x402_status} while x402 is not a listed rail "
                            "(a rail that is off must answer 404)")
        for label, text in surfaces:
            if says_x402(text):
                problems.append(f"{label} offers x402 while x402 is not a listed rail")

    offers_credits = "(credits)" in (auth_text or "") or "credit package" in (auth_text or "").lower()
    if credits_on and not offers_credits:
        problems.append("credits is a listed rail but the auth_required text never offers credits")
    if not credits_on and offers_credits:
        problems.append("the auth_required text offers credits while credits is not a listed rail")

    # ---- the premium data tools: the quota, and the x402 mention, per tool
    cost = screen_sanctions_cost_usd
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        problems.append("preview_cost for screen_sanctions could not be read, so the quota claim was not "
                        "cross-checked")
    elif isinstance(quota, bool):
        if quota and not cost > 0:
            problems.append("the premium-data quota is reported as enforced but preview_cost says screen_sanctions is free")
        if not quota and cost > 0:
            problems.append("the premium-data quota is reported as not enforced but preview_cost prices screen_sanctions")
    if descs and isinstance(quota, bool):
        for name in _PREMIUM_DATA_TOOLS:
            d = descs.get(name)
            if d is None:
                problems.append(f"tools/list has no {name}, so its quota claim could not be judged")
                continue
            tagged = "free in quota" in d
            if quota and not tagged:
                problems.append(f"{name}: the quota is reported as enforced but tools/list does not tag it "
                                "'free in quota'")
            if not quota and tagged:
                problems.append(f"{name}: tools/list promises a quota ('free in quota') while "
                                "premium_data_quota_enforced is false")
            if x402_on and not quota and says_x402(d):
                problems.append(f"{name}: tools/list offers x402, but while the quota is not enforced this tool is "
                                "answered free before any payment is read")
            if x402_on and quota and not says_x402(d):
                problems.append(f"{name}: x402 is a listed rail and the quota is enforced, but tools/list does not "
                                "offer x402 on it")

    # ---- a quoted price is a charge only while a rail can charge it
    for name, d in sorted(descs.items()):
        tag = _PRICE_TAG.search(d)
        if not rails and tag and NOT_CHARGED not in tag.group(0):
            problems.append(f"{name}: quoted as charged ({tag.group(0)}) while no payment rail is on")
        if rails and NOT_CHARGED in d:
            problems.append(f"{name}: says it is not charged while {rails} is a live rail")
    return problems


def edge_snapshot_payments_in_step(descriptor: dict) -> Optional[bool]:
    """Does the committed edge snapshot (edge/src/snapshots/mcp.json) say what the live descriptor says about
    status, rails and the quota flag? None when the snapshot cannot be read.

    Informational, not a gate: the edge worker serves that file only when it has no fresh KV overlay, and it is
    not in the live path today (neither api.hatchloop.dev nor hatchloop.dev answers with x-edge-source). But a
    switch flipped without regenerating it leaves a block that contradicts the origin the moment the worker is
    deployed, so the receipt says so (review F9). Refresh with `python scripts/refresh_edge_snapshots.py
    --local-routes mcp.json` (no --check rewrites it) after flipping any switch.
    """
    try:
        snap = json.loads((ROOT / "edge" / "src" / "snapshots" / "mcp.json").read_text(encoding="utf-8"))["payments"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return all(snap.get(k) == descriptor.get(k) for k in ("status", "rails", "premium_data_quota_enforced"))


def check_payments(ctx: dict) -> dict:
    """The payment claims of the DEPLOYED server agree with each other (see payments_problems). Reads only: the
    send_message call carries no arguments and is refused for identity before anything could be sent."""
    base = ctx["base"]
    ds, _, dtext = http("GET", base + "/.well-known/mcp.json")
    try:
        descriptor = (json.loads(dtext).get("payments") or {}) if ds == 200 else {}
    except (ValueError, AttributeError):
        descriptor = {}
    xs, _, _ = http("GET", base + "/.well-known/x402")
    ts, _, tl = rpc(base + "/mcp", "tools/list")
    listed = (((tl or {}).get("result") or {}).get("tools") or []) if isinstance(tl, dict) else []
    tool_descriptions = {str(t.get("name")): str(t.get("description", "")) for t in listed if isinstance(t, dict)}
    _, _, auth = rpc(base + "/mcp", "tools/call", {"name": "send_message", "arguments": {}})
    auth_body = tool_body(auth)
    auth_text = json.dumps({"human_message": auth_body.get("human_message"), "how_to_resolve": auth_body.get("how_to_resolve")})
    ks, _, keys_text = http("GET", base + "/keys/request")
    _, _, pc = rpc(base + "/mcp", "tools/call", {"name": "preview_cost", "arguments": {"operation": "screen_sanctions"}})
    cost = tool_body(pc).get("estimated_cost_usd")
    problems = payments_problems(descriptor=descriptor, x402_status=xs, tools_status=ts,
                                 tool_descriptions=tool_descriptions, auth_text=auth_text, keys_status=ks,
                                 keys_text=keys_text if ks == 200 else "", screen_sanctions_cost_usd=cost,
                                 expect_rails=ctx.get("expect_rails"))
    if auth_body.get("error_code") != "auth_required":
        problems.append(f"an anonymous send_message was not refused for identity (error_code {auth_body.get('error_code')!r}), "
                        "so the offered options could not be read")
    return check(ds == 200 and not problems, status=descriptor.get("status"), rails=descriptor.get("rails"),
                 premium_data_quota_enforced=descriptor.get("premium_data_quota_enforced"), x402_discovery_status=xs,
                 tools_listed=len(tool_descriptions), keys_request_status=ks,
                 edge_snapshot_payments_in_step=edge_snapshot_payments_in_step(descriptor),
                 screen_sanctions_preview_usd=cost, problems=problems)


CHECKS: dict = {"health": check_health, "mcp2026": check_mcp2026, "legacy": check_legacy, "oauth": check_oauth,
                "sanctions": check_sanctions, "find_business": check_find_business, "labels": check_labels,
                "retired": check_retired, "payments": check_payments}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-commit", required=True)
    ap.add_argument("--base", default="https://api.hatchloop.dev")
    ap.add_argument("--site", default="https://hatchloop.dev")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default=",".join(CHECKS))
    ap.add_argument("--skip-email", action="store_true", help="oauth: discovery and the challenge only; send nothing")
    ap.add_argument("--pace", type=float, default=0.0, help="find_business: seconds to wait before each call")
    ap.add_argument("--expect-rails", default=None,
                    help="payments: the rails that should be listed, comma separated; an empty string means none "
                         "(CREDITS_ENABLED and X402_ENABLED off). Omit to check only that the claims agree.")
    a = ap.parse_args(argv)
    expect_rails = None if a.expect_rails is None else [r.strip() for r in a.expect_rails.split(",") if r.strip()]
    ctx = {"base": a.base.rstrip("/"), "site": a.site.rstrip("/"), "expect": a.expect_commit, "env_file": a.env_file,
           "skip_email": a.skip_email, "pace": a.pace, "expect_rails": expect_rails}
    report: dict = {"checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "expect_commit": a.expect_commit, "results": {}}
    failed = []
    for name in [n.strip() for n in a.only.split(",") if n.strip()]:
        fn: Callable = CHECKS[name]
        t0 = time.time()
        try:
            res = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            res = check(False, exception=f"{type(exc).__name__}: {str(exc)[:200]}")
        res["seconds"] = round(time.time() - t0, 1)
        report["results"][name] = res
        print(f"{'PASS' if res['ok'] else 'FAIL'}  {name}  ({res['seconds']} s)", flush=True)
        if not res["ok"]:
            failed.append(name)
    report["all_passed"] = not failed
    report["failed"] = failed
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("all passed" if not failed else "FAILED: " + ", ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
