#!/usr/bin/env python3
"""Before/after/live check for migrations/spine/013_usage_events_door_columns.sql (door, protocol_version, result_count).

    python scripts/verify_spine_013.py --phase before [--env-file PATH] [--json OUT] [--no-boundary]
    python scripts/verify_spine_013.py --phase after  [--env-file PATH] [--json OUT] [--no-boundary]
    python scripts/verify_spine_013.py --phase live   [--env-file PATH] [--json OUT] [--minutes 60]

Run `before` and `after` around the migration, BEFORE deploying the code that uses it (an endpoint deploys cleanly
without its schema and fails on the first real person - the rule recorded in sql/agentbroker/README.md); run `live`
after the deploy to prove the new code is the one writing. Two independent views of the schema, because each lies
on its own (the same pair migrations 009 and 011 used):

  * CATALOG  - straight from Postgres as the DSN's role (SUPABASE_DB_URL, the spine tunnel), in a READ-ONLY session:
               do the three columns exist (nullable, no default); does usage_events_insert_v3 exist exactly once,
               SECURITY DEFINER, owned by a role that bypasses RLS, search_path pinned, executable by `anon` and
               `service_role` and by neither `authenticated` nor `public`; are v1 and v2 still there and unchanged
               in shape; is the table still closed to anon/authenticated/public.
  * BOUNDARY - through the PUBLIC door (https://techmate.om/spine) with the anon JWT, the only credential the
               AgentBroker container holds. Every write is sent with `Prefer: tx=rollback`, so verification leaves
               no row. A malformed door, version and count must each be refused (22023); a direct read of the
               table must answer 401/403, never an empty 200.
  * LIVE     - the rows themselves: in the last --minutes, at least one row carries a door and no MCP row lacks one.

Read-only in effect. No secret value is printed: env values are read, used and dropped.
Exit 0 when the requested phase holds, 1 when it does not, 2 when it could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

NEW_FUNCTION = "usage_events_insert_v3"
LIVE_WRITERS = {"usage_events_insert": 7, "usage_events_insert_v2": 17}
COLUMNS = ["door", "protocol_version", "result_count"]
WANT_TYPES = {"door": "text", "protocol_version": "text", "result_count": "integer"}
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


def catalog(dsn: str, minutes: int = 60) -> dict:
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=20)
    conn.set_session(readonly=True, autocommit=True)       # nothing in this function can write
    cur = conn.cursor()
    out: dict = {}
    cur.execute("select current_user, current_database()")
    out["connected_as"], out["database"] = cur.fetchone()

    cur.execute(
        "select column_name, data_type, is_nullable, column_default from information_schema.columns "
        "where table_schema = 'public' and table_name = 'usage_events' and column_name = any(%s)", (COLUMNS,))
    found = {r[0]: {"type": r[1], "nullable": r[2] == "YES", "default": r[3]} for r in cur.fetchall()}
    out["columns"] = {c: found.get(c) for c in COLUMNS}

    cur.execute(
        "select p.proname, count(*) over (partition by p.proname), p.pronargs, p.prosecdef, r.rolname, r.rolbypassrls, "
        "has_function_privilege('anon', p.oid, 'EXECUTE'), has_function_privilege('authenticated', p.oid, 'EXECUTE'), "
        "has_function_privilege('public', p.oid, 'EXECUTE'), has_function_privilege('service_role', p.oid, 'EXECUTE'), "
        "p.proconfig "
        "from pg_proc p join pg_roles r on r.oid = p.proowner join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.proname = any(%s) order by 1",
        ([NEW_FUNCTION, *LIVE_WRITERS],))
    funcs: dict = {}
    for name, copies, nargs, secdef, owner, bypass, anon, authd, public, svc, config in cur.fetchall():
        funcs[name] = {
            "copies": copies, "args": nargs, "security_definer": secdef, "owner": owner, "owner_bypasses_rls": bypass,
            "anon_execute": anon, "authenticated_execute": authd, "public_execute": public, "service_role_execute": svc,
            "search_path_pinned": bool(config) and any(str(c).startswith("search_path=") for c in config),
        }
    out["functions"] = {n: funcs.get(n) for n in (NEW_FUNCTION, *LIVE_WRITERS)}

    cur.execute("select c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relname = 'usage_events'")
    row = cur.fetchone()
    out["usage_events_rls_enabled"] = bool(row[0]) if row else None
    cur.execute("select grantee, privilege_type from information_schema.role_table_grants "
                "where table_schema = 'public' and table_name = 'usage_events' "
                "and grantee in ('anon', 'authenticated', 'public') order by 1, 2")
    out["direct_table_grants_to_anon_authenticated_public"] = [f"{g}:{p}" for g, p in cur.fetchall()]

    cur.execute("select count(*) from public.usage_events")
    out["row_counts"] = {"usage_events": cur.fetchone()[0]}

    if out["columns"]["door"] and out["columns"]["protocol_version"]:
        cur.execute(
            "select count(*), count(*) filter (where door is not null), "
            "count(*) filter (where method <> 'http' and door is null), "
            "count(*) filter (where method = 'http' and door is null and detail ~ '^[A-Z]+ /mcp(/|$)'), "
            "count(*) filter (where door is null and detail ~ '(^| )door=[a-z0-9]') "
            "from public.usage_events where ts >= now() - make_interval(mins => %s)", (minutes,))
        total, with_door, mcp_no_door, http_no_door, detail_only = cur.fetchone()
        out["recent"] = {"minutes": minutes, "rows": total, "with_door": with_door,
                         "mcp_rows_without_a_door": mcp_no_door, "mcp_http_rows_without_a_door": http_no_door,
                         "rows_with_door_only_in_detail": detail_only}
    else:
        out["recent"] = None
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
    good = {"p_tool": "verify013", "p_args_hash": None, "p_ip_hash": None, "p_user_agent": "verify_spine_013",
            "p_key_id": None, "p_session_kind": "crawler", "p_method": "tools/list", "p_outcome": "ok",
            "p_door": "agent-broker", "p_protocol_version": "2025-06-18", "p_result_count": 1}

    def call(body: dict):
        with httpx.Client(timeout=20.0) as c:
            return c.post(f"{base}/rpc/{NEW_FUNCTION}", headers=rb, json=body)

    def shape(resp):
        entry = {"http": resp.status_code, "pg_code": _code(resp)}
        if resp.status_code == 200:
            try:
                data = resp.json()
                entry["returned_an_id"] = isinstance(data, dict) and "id" in data
            except Exception:  # noqa: BLE001
                entry["unparsed"] = True
        return entry

    res["valid_call_rolled_back"] = shape(call(good))
    for name, change in (("bad_door", {"p_door": "Has Capitals"}), ("bad_protocol_version", {"p_protocol_version": "not-a-date"}),
                         ("bad_result_count", {"p_result_count": -1})):
        res[name] = shape(call({**good, **change}))
    with httpx.Client(timeout=20.0) as c:
        r = c.get(f"{base}/usage_events", headers=h, params={"select": "id", "limit": "1"})
        res["direct_read_usage_events"] = {"http": r.status_code, "pg_code": _code(r)}
    return res


def judge(phase: str, cat: dict, bnd: dict | None) -> list[str]:
    problems: list[str] = []
    cols, funcs = cat["columns"], cat["functions"]
    if phase == "before":
        for c, v in cols.items():
            if v is not None:
                problems.append(f"column {c} already exists")
        if funcs[NEW_FUNCTION] is not None:
            problems.append(f"{NEW_FUNCTION} already exists")
        for name, nargs in LIVE_WRITERS.items():
            v = funcs[name]
            if v is None:
                problems.append(f"{name} is missing (013 is built on it)")
            elif v["args"] != nargs:
                problems.append(f"{name} has {v['args']} parameters, expected {nargs}")
        if bnd is not None and bnd["valid_call_rolled_back"]["http"] == 200:
            problems.append(f"{NEW_FUNCTION} already answers 200 through the public door")
        return problems

    for c, v in cols.items():
        if v is None:
            problems.append(f"column {c} is missing")
            continue
        if v["type"] != WANT_TYPES[c]:
            problems.append(f"column {c} is {v['type']}, expected {WANT_TYPES[c]}")
        if not v["nullable"]:
            problems.append(f"column {c} is NOT NULL (old writers do not send it)")
        if v["default"] is not None:
            problems.append(f"column {c} has a default (a rewrite of a hot table)")
    f = funcs[NEW_FUNCTION]
    if f is None:
        problems.append(f"{NEW_FUNCTION} is missing")
    else:
        if f["copies"] != 1:
            problems.append(f"{NEW_FUNCTION} exists {f['copies']} times (an overload makes PostgREST answer PGRST203)")
        if f["args"] != 20:
            problems.append(f"{NEW_FUNCTION} has {f['args']} parameters, expected 20")
        if not f["security_definer"]:
            problems.append(f"{NEW_FUNCTION} is not SECURITY DEFINER")
        if not f["search_path_pinned"]:
            problems.append(f"{NEW_FUNCTION} does not pin its search_path")
        if not f["owner_bypasses_rls"]:
            problems.append(f"{NEW_FUNCTION} owner {f['owner']} does not bypass RLS")
        if f["owner"] != "spine_owner":
            problems.append(f"{NEW_FUNCTION} is owned by {f['owner']}, not spine_owner")
        if not f["anon_execute"]:
            problems.append(f"{NEW_FUNCTION} is not executable by anon")
        if f["authenticated_execute"] or f["public_execute"]:
            problems.append(f"{NEW_FUNCTION} is executable by authenticated/public")
        if not f["service_role_execute"]:
            problems.append(f"{NEW_FUNCTION} is not executable by service_role")
    for name, nargs in LIVE_WRITERS.items():
        v = funcs[name]
        if v is None or v["args"] != nargs:
            problems.append(f"{name} changed or went missing (013 must leave it alone)")
    if cat["direct_table_grants_to_anon_authenticated_public"]:
        problems.append(f"usage_events has direct grants: {cat['direct_table_grants_to_anon_authenticated_public']}")
    if not cat["usage_events_rls_enabled"]:
        problems.append("usage_events has row-level security OFF")
    if bnd is not None:
        if bnd["valid_call_rolled_back"]["http"] != 200 or not bnd["valid_call_rolled_back"].get("returned_an_id"):
            problems.append(f"public door valid call -> {bnd['valid_call_rolled_back']}")
        for key in ("bad_door", "bad_protocol_version", "bad_result_count"):
            if bnd[key]["http"] not in (400, 422) or bnd[key]["pg_code"] not in ("22023", ""):
                problems.append(f"public door {key} should be refused (22023), got {bnd[key]}")
        if bnd["direct_read_usage_events"]["http"] not in (401, 403):
            problems.append(f"direct read of usage_events answered {bnd['direct_read_usage_events']}")
    if phase == "live":
        rec = cat.get("recent")
        if rec is None:
            problems.append("the columns are missing, so no row can carry a door")
        elif rec["rows"] == 0:
            problems.append(f"no row in the last {rec['minutes']} minutes: cannot tell whether the new code is writing")
        else:
            if rec["with_door"] == 0:
                problems.append(f"{rec['rows']} rows in the last {rec['minutes']} minutes and none has a door "
                                f"(is the new image deployed? {rec['rows_with_door_only_in_detail']} name it only in detail)")
            if rec["mcp_rows_without_a_door"]:
                problems.append(f"{rec['mcp_rows_without_a_door']} MCP rows in the window have no door")
            if rec["mcp_http_rows_without_a_door"]:
                problems.append(f"{rec['mcp_http_rows_without_a_door']} HTTP rows for /mcp paths have no door")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["before", "after", "live"], required=True)
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    ap.add_argument("--json", default="")
    ap.add_argument("--minutes", type=int, default=60, help="the window the live phase reads")
    ap.add_argument("--no-boundary", action="store_true", help="catalog only (no call through the public door)")
    a = ap.parse_args()

    dsn = _env("SUPABASE_DB_URL", a.env_file)
    url = _env("SUPABASE_URL", a.env_file)
    anon = _env("SUPABASE_ANON_KEY", a.env_file)
    if not dsn or (not a.no_boundary and not (url and anon)):
        print("ERROR: SUPABASE_DB_URL (and, unless --no-boundary, SUPABASE_URL and SUPABASE_ANON_KEY) are needed "
              f"(env or {a.env_file})")
        return 2
    if not a.no_boundary and "techmate.om/spine" not in url:
        print("ERROR: SUPABASE_URL does not point at the spine; refusing to run against anything else")
        return 2

    cat = catalog(dsn, a.minutes)
    bnd = None if (a.no_boundary or a.phase == "live") else boundary(url, anon)
    problems = judge(a.phase, cat, bnd)
    report = {"phase": a.phase, "catalog": cat, "boundary": bnd, "problems": problems}
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"phase": a.phase, "connected_as": cat["connected_as"], "database": cat["database"],
                      "columns_present": sum(1 for v in cat["columns"].values() if v),
                      "v3_present": cat["functions"][NEW_FUNCTION] is not None,
                      "recent": cat.get("recent"), "row_counts": cat["row_counts"], "problems": problems},
                     indent=2, default=str))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
