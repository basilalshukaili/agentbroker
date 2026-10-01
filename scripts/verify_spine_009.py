#!/usr/bin/env python3
"""Before/after check for migrations/spine/009_keyholder_outcome_logging_and_compliance_rpcs.sql.

    python scripts/verify_spine_009.py --phase before   [--env-file PATH] [--json OUT]
    python scripts/verify_spine_009.py --phase after    [--env-file PATH] [--json OUT]

Two independent views of the same fact, because each lies on its own:

  * CATALOG  - straight from Postgres as the DSN's role (SUPABASE_DB_URL, the spine tunnel):
               does each function exist, is it SECURITY DEFINER, who owns it, may `anon` execute it,
               and which table privileges do anon/authenticated/public still hold.
  * BOUNDARY - through the PUBLIC door (https://techmate.om/spine) with the anon JWT, the only
               credential the AgentBroker container holds. Every write is sent with
               `Prefer: tx=rollback` (cutover doc section 3 item 7), so verification leaves no row.

A migration that exists in the repo is not a migration that is applied, and a catalog row is not a
working door; this prints both. No secret value is ever printed: env values are read, used, dropped.
Exit 0 when the requested phase holds, 1 when it does not.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

NEW_FUNCTIONS = [
    "usage_events_insert_v2",
    "compliance_audit_insert",
    "pending_keys_upsert",
    "pending_keys_consume",
    "consent_optouts_hydrate",
    "consent_optouts_record",
]
CLOSED_TABLES = ["compliance_audit", "pending_keys", "consent_optouts"]
NEW_USAGE_COLUMNS = [
    "outcome", "error_code", "http_status", "latency_ms", "client_name",
    "client_version", "key_state", "arg_names", "requested_name", "detail",
]
# No path is baked in: pass --env-file, or export HATCHLOOP_ENV_FILE (or the variables themselves).
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
        "has_function_privilege('service_role', p.oid, 'EXECUTE') "
        "from pg_proc p join pg_roles r on r.oid = p.proowner "
        "join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.proname = any(%s) order by 1",
        (NEW_FUNCTIONS,),
    )
    found = {}
    for name, secdef, owner, bypass, anon, authd, public, svc in cur.fetchall():
        found[name] = {
            "security_definer": secdef, "owner": owner, "owner_bypasses_rls": bypass,
            "anon_execute": anon, "authenticated_execute": authd,
            "public_execute": public, "service_role_execute": svc,
        }
    out["functions"] = {n: found.get(n) for n in NEW_FUNCTIONS}

    cur.execute(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='usage_events' and column_name = any(%s)",
        (NEW_USAGE_COLUMNS,),
    )
    have = {r[0] for r in cur.fetchall()}
    out["usage_events_new_columns_present"] = sorted(have)
    out["usage_events_new_columns_missing"] = sorted(set(NEW_USAGE_COLUMNS) - have)

    cur.execute(
        "select table_name, grantee, privilege_type from information_schema.role_table_grants "
        "where table_schema='public' and table_name = any(%s) "
        "and grantee in ('anon','authenticated','public') order by 1,2,3",
        (CLOSED_TABLES,),
    )
    grants: dict = {t: [] for t in CLOSED_TABLES}
    for t, g, priv in cur.fetchall():
        grants[t].append(f"{g}:{priv}")
    out["direct_table_grants_to_anon_authenticated_public"] = grants

    cur.execute("select count(*) from public.usage_events")
    out["usage_events_rows"] = cur.fetchone()[0]
    cur.execute("select count(*) from public.compliance_audit")
    out["compliance_audit_rows"] = cur.fetchone()[0]
    cur.execute("select count(*) from public.pending_keys")
    out["pending_keys_rows"] = cur.fetchone()[0]
    cur.execute("select count(*) from public.consent_optouts")
    out["consent_optouts_rows"] = cur.fetchone()[0]
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

    def rpc(name: str, body: dict):
        with httpx.Client(timeout=20.0) as c:
            return c.post(f"{base}/rpc/{name}", headers=rb, json=body)

    def shape(resp, key=None):
        entry = {"http": resp.status_code, "pg_code": _code(resp)}
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict) and key:
                    entry[key] = data.get(key)
                if isinstance(data, list):
                    entry["rows_returned"] = len(data)
                    entry["row_keys"] = sorted(data[0].keys()) if data else []
            except Exception:  # noqa: BLE001
                entry["unparsed"] = True
        return entry

    res["usage_events_insert_v2"] = shape(rpc("usage_events_insert_v2", {
        "p_tool": "verify-009", "p_args_hash": None, "p_ip_hash": None, "p_user_agent": "verify-009",
        "p_key_id": None, "p_session_kind": "anon_agent", "p_method": "tools/call",
        "p_outcome": "tool_failure", "p_error_code": "verify", "p_http_status": 200,
        "p_latency_ms": 12, "p_client_name": "verify", "p_client_version": "0",
        "p_key_state": "placeholder", "p_arg_names": ["a", "b"], "p_requested_name": None,
        "p_detail": "verify-009 rollback"}), "id")
    res["usage_events_insert_v2_rejects_bad_outcome"] = shape(rpc("usage_events_insert_v2", {
        "p_tool": "verify-009", "p_args_hash": None, "p_ip_hash": None, "p_user_agent": "x",
        "p_key_id": None, "p_session_kind": "anon_agent", "p_method": "tools/call",
        "p_outcome": "not-an-outcome"}))
    res["usage_events_insert_v1_still_works"] = shape(rpc("usage_events_insert", {
        "p_tool": "verify-009", "p_args_hash": None, "p_ip_hash": None, "p_user_agent": "x",
        "p_key_id": None, "p_session_kind": "anon_agent", "p_method": "tools/call"}))
    res["compliance_audit_insert"] = shape(rpc("compliance_audit_insert", {
        "p_audit_id": "verify-009-rollback", "p_event_type": "authorization_allow",
        "p_ts": "2026-10-01T00:00:00+00:00", "p_agent_id": None, "p_principal_kind": None,
        "p_principal_id": None, "p_operation": "verify", "p_smb_id": None,
        "p_recipient_id_hash": None, "p_channel": "sms", "p_use_case": None,
        "p_jurisdiction": None, "p_decision": "allow", "p_reason": "verify",
        "p_token_hash": None, "p_trace_id": None, "p_metadata": {}}), "inserted")
    res["pending_keys_upsert"] = shape(rpc("pending_keys_upsert", {
        "p_email": "verify-009@example.invalid", "p_token": "verify-009-not-a-token",
        "p_expires_at": "2030-01-01T00:00:00+00:00"}), "stored")
    res["pending_keys_consume_absent_row"] = shape(rpc("pending_keys_consume", {
        "p_email": "verify-009-absent@example.invalid"}), "found")
    res["consent_optouts_hydrate"] = shape(rpc("consent_optouts_hydrate", {"p_limit": 1000, "p_offset": 0}))
    res["consent_optouts_record"] = shape(rpc("consent_optouts_record", {
        "p_recipient_id": "verify-009-synthetic", "p_channel": "sms",
        "p_revocation_method": "verify", "p_source": "verify_spine_009"}), "recorded")

    # The old direct doors must now answer 401/403 (permission denied), not 200 []:
    # an empty-but-successful read is the very shape that hid these bugs.
    with httpx.Client(timeout=20.0) as c:
        for t in CLOSED_TABLES:
            r = c.get(f"{base}/{t}", headers=h, params={"select": "*", "limit": "1"})
            res[f"direct_read_{t}"] = {"http": r.status_code, "pg_code": _code(r)}
    return res


def judge(phase: str, cat: dict, bnd: dict) -> list[str]:
    problems: list[str] = []
    fns = cat["functions"]
    if phase == "before":
        for n, v in fns.items():
            if v is not None:
                problems.append(f"{n} already exists")
        for key in ("usage_events_insert_v2", "compliance_audit_insert", "pending_keys_upsert",
                    "consent_optouts_hydrate", "consent_optouts_record"):
            if bnd[key]["http"] == 200:
                problems.append(f"{key} already answers 200 through the public door")
        return problems
    for n, v in fns.items():
        if v is None:
            problems.append(f"{n} is missing")
            continue
        if not v["security_definer"]:
            problems.append(f"{n} is not SECURITY DEFINER")
        if not v["owner_bypasses_rls"]:
            problems.append(f"{n} owner {v['owner']} does not bypass RLS")
        if not v["anon_execute"]:
            problems.append(f"{n} is not executable by anon")
        if v["authenticated_execute"] or v["public_execute"]:
            problems.append(f"{n} is executable by authenticated/public")
        if not v["service_role_execute"]:
            problems.append(f"{n} is not executable by service_role")
    if cat["usage_events_new_columns_missing"]:
        problems.append(f"usage_events columns missing: {cat['usage_events_new_columns_missing']}")
    for t, g in cat["direct_table_grants_to_anon_authenticated_public"].items():
        if g:
            problems.append(f"{t} still has direct grants: {g}")
    for key in ("usage_events_insert_v2", "compliance_audit_insert", "pending_keys_upsert",
                "pending_keys_consume_absent_row", "consent_optouts_hydrate",
                "consent_optouts_record", "usage_events_insert_v1_still_works"):
        if bnd[key]["http"] != 200:
            problems.append(f"public door {key} -> {bnd[key]}")
    if bnd["usage_events_insert_v2_rejects_bad_outcome"]["http"] not in (400, 422):
        problems.append(f"bad outcome was not rejected: {bnd['usage_events_insert_v2_rejects_bad_outcome']}")
    if bnd["consent_optouts_hydrate"].get("rows_returned", 0) > 0:
        keys = bnd["consent_optouts_hydrate"].get("row_keys")
        if keys != ["channel", "recipient_id"]:
            problems.append(f"hydrate row shape is {keys}")
    if bnd["compliance_audit_insert"].get("inserted") is not True:
        problems.append("compliance_audit_insert did not report inserted=true")
    for t in CLOSED_TABLES:
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
    report = {"phase": a.phase, "catalog": cat, "public_door": bnd}
    problems = judge(a.phase, cat, bnd)
    report["problems"] = problems
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if a.json:
        Path(a.json).write_text(text, encoding="utf-8")
    if a.phase == "before":
        # "before" documents the starting state; it only fails if the migration is somehow
        # already in place (in which case the 'after' proof would prove nothing new).
        return 1 if any("already" in p for p in problems) else 0
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
