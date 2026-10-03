#!/usr/bin/env python3
"""Before/after check for migrations/spine/011_oauth_connect.sql (the OAuth "Connect" sign-in).

    python scripts/verify_spine_011.py --phase before   [--env-file PATH] [--json OUT]
    python scripts/verify_spine_011.py --phase after    [--env-file PATH] [--json OUT]

Run it BEFORE deploying the code that uses the migration (an endpoint deploys cleanly without its schema and
fails on the first real person - the rule recorded in sql/agentbroker/README.md). Two independent views of one
fact, because each lies on its own (the same two migration 009 used):

  * CATALOG  - straight from Postgres as the DSN's role (SUPABASE_DB_URL, the spine tunnel): does each of the
               16 functions exist, is it SECURITY DEFINER, may `anon` execute it and `authenticated`/`public`
               not, and do the 5 tables have row-level security on and NO grant left for anon/authenticated/
               public.
  * BOUNDARY - through the PUBLIC door (https://techmate.om/spine) with the anon JWT, the only credential the
               AgentBroker container holds. Every write is sent with `Prefer: tx=rollback`, so verification
               leaves no row. Direct reads of the tables must answer 401/403, never 200 [] (an empty success
               is the shape that hid the 009 bugs).

Read-only in effect. No secret value is printed: env values are read, used and dropped.
Exit 0 when the requested phase holds, 1 when it does not, 2 when it could not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

FUNCTIONS = [
    "oauth_ready", "oauth_client_register", "oauth_client_get", "oauth_request_create", "oauth_request_get",
    "oauth_request_set_email", "oauth_request_lookup_magic", "oauth_request_decide", "oauth_request_poll",
    "oauth_request_complete", "oauth_code_consume", "oauth_refresh_store", "oauth_refresh_rotate",
    "oauth_refresh_revoke", "oauth_account_link", "oauth_account_for_email",
]
TABLES = ["oauth_clients", "oauth_requests", "oauth_codes", "oauth_refresh_tokens", "oauth_account_links"]
DEFAULT_ENV = os.environ.get("HATCHLOOP_ENV_FILE", "")


def _env(name: str, env_file: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    p = Path(env_file)
    if env_file and p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def catalog(dsn: str) -> dict:
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=20)
    conn.autocommit = True
    cur = conn.cursor()
    out: dict = {}
    cur.execute("select current_user, current_database()")
    out["connected_as"], out["database"] = cur.fetchone()

    cur.execute(
        "select p.proname, p.prosecdef, r.rolname, r.rolbypassrls, "
        "has_function_privilege('anon', p.oid, 'EXECUTE'), "
        "has_function_privilege('authenticated', p.oid, 'EXECUTE'), "
        "has_function_privilege('public', p.oid, 'EXECUTE'), "
        "has_function_privilege('service_role', p.oid, 'EXECUTE'), p.proconfig "
        "from pg_proc p join pg_roles r on r.oid = p.proowner "
        "join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.proname = any(%s) order by 1",
        (FUNCTIONS,),
    )
    found = {}
    for name, secdef, owner, bypass, anon, authd, public, svc, config in cur.fetchall():
        found[name] = {
            "security_definer": secdef, "owner": owner, "owner_bypasses_rls": bypass,
            "anon_execute": anon, "authenticated_execute": authd, "public_execute": public,
            "service_role_execute": svc,
            "search_path_pinned": bool(config) and any(str(c).startswith("search_path=") for c in config),
        }
    out["functions"] = {n: found.get(n) for n in FUNCTIONS}

    cur.execute(
        "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relname = any(%s)", (TABLES,))
    out["tables"] = {r[0]: {"rls_enabled": r[1]} for r in cur.fetchall()}
    for t in TABLES:
        out["tables"].setdefault(t, None)

    cur.execute(
        "select table_name, grantee, privilege_type from information_schema.role_table_grants "
        "where table_schema='public' and table_name = any(%s) "
        "and grantee in ('anon','authenticated','public') order by 1,2,3", (TABLES,))
    grants: dict = {t: [] for t in TABLES}
    for t, g, priv in cur.fetchall():
        grants[t].append(f"{g}:{priv}")
    out["direct_table_grants_to_anon_authenticated_public"] = grants

    counts = {}
    for t in TABLES:
        if out["tables"].get(t):
            cur.execute(f"select count(*) from public.{t}")
            counts[t] = cur.fetchone()[0]
    out["row_counts"] = counts
    conn.close()
    return out


def _code(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("code") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def boundary(url: str, anon: str) -> dict:
    import httpx

    base = url.rstrip("/") + "/rest/v1"
    h = {"apikey": anon, "Authorization": f"Bearer {anon}", "Content-Type": "application/json"}
    rb = {**h, "Prefer": "tx=rollback"}
    res: dict = {}
    marker = "verify011" + secrets.token_hex(6)

    def rpc(name: str, body: dict, headers=rb):
        with httpx.Client(timeout=20.0) as c:
            return c.post(f"{base}/rpc/{name}", headers=headers, json=body)

    def shape(resp, key=None):
        entry = {"http": resp.status_code, "pg_code": _code(resp)}
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict) and key:
                    entry[key] = data.get(key)
            except Exception:  # noqa: BLE001
                entry["unparsed"] = True
        return entry

    zero = "0" * 64
    res["oauth_ready"] = shape(rpc("oauth_ready", {}, h), "ready")
    res["oauth_request_create"] = shape(rpc("oauth_request_create", {
        "p_request_id": marker, "p_client_id": "https://verify.invalid/c.json", "p_redirect_uri": "https://verify.invalid/cb",
        "p_code_challenge": "A" * 43, "p_scope": "agentbroker.tools", "p_state": "", "p_resource": "https://api.hatchloop.dev/mcp",
        "p_ttl_seconds": 900}), "created")
    res["oauth_request_get_absent"] = shape(rpc("oauth_request_get", {"p_request_id": marker}, h), "found")
    res["oauth_client_register"] = shape(rpc("oauth_client_register", {
        "p_client_id": "dcr_" + marker, "p_client_name": "verify", "p_redirect_uris": ["https://verify.invalid/cb"],
        "p_ip_hash": None}), "stored")
    res["oauth_code_consume_absent"] = shape(rpc("oauth_code_consume", {"p_code_hash": zero}, h), "reason")
    res["oauth_refresh_rotate_absent"] = shape(rpc("oauth_refresh_rotate", {
        "p_old_hash": zero, "p_new_hash": hashlib.sha256(marker.encode()).hexdigest(),
        "p_client_id": "x", "p_ttl_seconds": 60}, h), "reason")
    res["oauth_account_for_email_absent"] = shape(rpc("oauth_account_for_email", {
        "p_email_hash": hashlib.sha256(b"verify-011@example.invalid").hexdigest()}, h), "found")
    with httpx.Client(timeout=20.0) as c:
        for t in TABLES:
            r = c.get(f"{base}/{t}", headers=h, params={"select": "*", "limit": "1"})
            res[f"direct_read_{t}"] = {"http": r.status_code, "pg_code": _code(r)}
    return res


def judge(phase: str, cat: dict, bnd: dict | None) -> list[str]:
    problems: list[str] = []
    fns = cat["functions"]
    if phase == "before":
        for n, v in fns.items():
            if v is not None:
                problems.append(f"{n} already exists")
        for t, v in cat["tables"].items():
            if v is not None:
                problems.append(f"table {t} already exists")
        if bnd and bnd["oauth_ready"]["http"] == 200:
            problems.append("oauth_ready already answers 200 through the public door")
        return problems
    for n, v in fns.items():
        if v is None:
            problems.append(f"{n} is missing")
            continue
        if not v["security_definer"]:
            problems.append(f"{n} is not SECURITY DEFINER")
        if not v["search_path_pinned"]:
            problems.append(f"{n} does not pin its search_path")
        if not v["owner_bypasses_rls"]:
            problems.append(f"{n} owner {v['owner']} does not bypass RLS")
        if not v["anon_execute"]:
            problems.append(f"{n} is not executable by anon")
        if v["authenticated_execute"] or v["public_execute"]:
            problems.append(f"{n} is executable by authenticated/public")
        if not v["service_role_execute"]:
            problems.append(f"{n} is not executable by service_role")
    for t, v in cat["tables"].items():
        if v is None:
            problems.append(f"table {t} is missing")
        elif not v["rls_enabled"]:
            problems.append(f"table {t} has row-level security OFF")
    for t, g in cat["direct_table_grants_to_anon_authenticated_public"].items():
        if g:
            problems.append(f"{t} still has direct grants: {g}")
    if bnd is not None:
        if bnd["oauth_ready"].get("ready") is not True:
            problems.append(f"oauth_ready through the public door -> {bnd['oauth_ready']}")
        for key, field, want in (("oauth_request_create", "created", True), ("oauth_request_get_absent", "found", False),
                                 ("oauth_client_register", "stored", True), ("oauth_code_consume_absent", "reason", "invalid"),
                                 ("oauth_refresh_rotate_absent", "reason", "invalid"), ("oauth_account_for_email_absent", "found", False)):
            if bnd[key]["http"] != 200 or bnd[key].get(field) != want:
                problems.append(f"public door {key} -> {bnd[key]} (wanted {field}={want!r})")
        for t in TABLES:
            if bnd[f"direct_read_{t}"]["http"] not in (401, 403):
                problems.append(f"direct read of {t} answered {bnd[f'direct_read_{t}']}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["before", "after"], required=True)
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    dsn = _env("SUPABASE_DB_URL", a.env_file)
    url = _env("SUPABASE_URL", a.env_file)
    anon = _env("SUPABASE_ANON_KEY", a.env_file)
    if not (dsn and url and anon):
        print("ERROR: SUPABASE_DB_URL, SUPABASE_URL and SUPABASE_ANON_KEY are all needed "
              f"(env or {a.env_file})")
        return 2
    if "techmate.om/spine" not in url:
        print("ERROR: SUPABASE_URL does not point at the spine; refusing to run against anything else")
        return 2

    cat = catalog(dsn)
    bnd = boundary(url, anon)
    problems = judge(a.phase, cat, bnd)
    report = {"phase": a.phase, "catalog": cat, "boundary": bnd, "problems": problems}
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"phase": a.phase, "connected_as": cat["connected_as"], "database": cat["database"],
                      "functions_present": sum(1 for v in cat["functions"].values() if v),
                      "tables_present": sum(1 for v in cat["tables"].values() if v),
                      "row_counts": cat["row_counts"], "problems": problems}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
