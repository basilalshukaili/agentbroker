#!/usr/bin/env python3
"""Who is calling AgentBroker, through which door, under which protocol version - from usage_events, read-only.

    python scripts/door_usage_report.py [--days 7] [--hours 1] [--lookback-days 30] [--json] [--env-file PATH]
                                        [--own-ua UA ...]

Verdict item A7 (docs/reviews/2026-10-03-mcp-focus-verdict.md): "without it we cannot see the first buyer. Done
when: the first Muse, Dots or Grok call is visible within the same hour." This is the part that reads what
migration 013 and the door instrumentation write. Five questions, each a single read-only query:

  door_by_day              Rows per day, door and protocol version, with how many callers and how many were ours.
  organic_callers_by_day   Legacy key: distinct observed callers (address hash + user agent) that sent an MCP request
                           to a LIVE door, excluding our own infrastructure's user agents. `callers_that_worked` is
                           the subset not classified as crawler. Neither count proves organic demand or success;
                           named probes and unlisted internal callers may remain, and older library calls can be
                           misclassified as crawler. Compare method and outcome before claiming useful work.
  new_clients_in_window    Clients (client name from the handshake, else the user agent) that called a live door in
                           the last --hours and had not been seen in the --lookback-days before that. This is the
                           "first Muse call, within the hour" view.
  result_counts_by_tool    For tools/call: calls, how many carry a result_count, how many returned zero results, how
                           many did not succeed. Excludes configured own user agents, not all probes.
  instrumentation_gaps     Rows in the last --hours that SHOULD carry a door and do not: a non-zero count means a
                           door is not instrumented (or the old image is still the one writing).

DEFINITIONS, so the number can be argued with:
  live door      door is a capability door or `agent-broker`, or door is NULL on an MCP row (every row written before
                 migration 013 has none, and the retired doors wrote no rows before it - so a NULL-door MCP row came
                 from a live door). `retired:<slug>` and `unknown` are excluded; so are method = 'http' rows (a
                 failure at the HTTP layer is not a caller doing MCP).
  own            user agents in OWN_INFRA_UAS. Keep it in step with OWN_INFRA_UA_EXACT in scripts/mcp_traffic_audit.py
                 (the health checks, the keepalive, the Door Reliability Run, the MCP probe, the live verification).
  caller         one (ip_hash, user_agent) pair. A person behind two networks is two callers; two people behind one
                 NAT with one client are one. It is an approximate source count, not a head count or buyer count.

The connection MUST be a read-only session; `report()` refuses any other, and `connect()` makes one.
No secret is printed: the DSN is read from the environment or the env file and never echoed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

OWN_INFRA_UAS = (
    "HatchLoopHealth/1.0", "hatchloop-health/1", "HatchLoopOS/1.0", "hatchloop-keepalive/1",
    "HatchLoop-DRR/1", "hatchloop-mcp-2026-probe/1", "hatchloop-live-verify/1",
)

LIVE_DOOR = ("((door is null and method <> 'http') or "
             "(door is not null and door not like 'retired:%%' and door <> 'unknown'))")
NOT_OWN = "coalesce(user_agent, '') <> all(%(own)s::text[])"
CLIENT = "coalesce(nullif(client_name, ''), nullif(user_agent, ''), '(none)')"

QUERIES = {
    "door_by_day": f"""
        select (ts at time zone 'UTC')::date as day, door, protocol_version,
               count(*) as rows,
               count(distinct (ip_hash, user_agent)) as callers,
               count(*) filter (where coalesce(user_agent, '') = any(%(own)s::text[])) as own_infra_rows
          from public.usage_events
         where ts >= now() - make_interval(days => %(days)s)
         group by 1, 2, 3
         order by 1 desc, rows desc""",
    "organic_callers_by_day": f"""
        select (ts at time zone 'UTC')::date as day,
               count(distinct (ip_hash, user_agent)) as callers,
               count(distinct (ip_hash, user_agent)) filter (where session_kind <> 'crawler') as callers_that_worked
          from public.usage_events
         where ts >= now() - make_interval(days => %(days)s)
           and method <> 'http' and {LIVE_DOOR} and {NOT_OWN}
         group by 1
         order by 1""",
    "new_clients_in_window": f"""
        with seen as (
            select {CLIENT} as client, door, min(ts) as first_seen, count(*) as rows
              from public.usage_events
             where ts >= now() - make_interval(hours => %(hours)s)
               and method <> 'http' and {LIVE_DOOR} and {NOT_OWN}
             group by 1, 2)
        select s.client, s.door, s.first_seen, s.rows
          from seen s
         where not exists (
               select 1 from public.usage_events e
                where e.ts <  now() - make_interval(hours => %(hours)s)
                  and e.ts >= now() - make_interval(hours => %(hours)s) - make_interval(days => %(lookback)s)
                  and coalesce(nullif(e.client_name, ''), nullif(e.user_agent, ''), '(none)') = s.client)
         order by s.first_seen""",
    "result_counts_by_tool": f"""
        select tool,
               count(*) as calls,
               count(result_count) as with_count,
               count(*) filter (where result_count = 0) as zero_results,
               round(avg(result_count)::numeric, 2) as avg_results,
               count(*) filter (where outcome is distinct from 'ok') as not_ok
          from public.usage_events
         where ts >= now() - make_interval(days => %(days)s)
           and method = 'tools/call' and {LIVE_DOOR} and {NOT_OWN}
         group by tool
         order by calls desc""",
    "instrumentation_gaps": """
        select count(*) filter (where method <> 'http' and door is null) as mcp_rows_without_a_door,
               count(*) filter (where method = 'http' and door is null and detail ~ '^[A-Z]+ /mcp(/|$)')
                   as mcp_http_rows_without_a_door,
               count(*) as rows
          from public.usage_events
         where ts >= now() - make_interval(hours => %(hours)s)""",
}


def connect(dsn: str):
    """A read-only, autocommit session: the only kind report() accepts."""
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=20)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def report(conn, *, days: int = 7, hours: int = 1, lookback_days: int = 30, own_uas=OWN_INFRA_UAS) -> dict:
    """Run every query and return {name: [row dicts]} (instrumentation_gaps is one dict)."""
    if conn.readonly is not True:
        raise ValueError("door_usage_report only runs on a read-only session (use connect())")
    params = {"days": int(days), "hours": int(hours), "lookback": int(lookback_days), "own": list(own_uas)}
    out: dict = {}
    for name, sql in QUERIES.items():
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        out[name] = rows[0] if name == "instrumentation_gaps" else rows
    return out


def _env(name: str, env_file: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    p = Path(env_file)
    if env_file and p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _print(rep: dict, a) -> None:
    print("== observed callers per day (live doors, excluding configured own user agents) ==")
    print("  Counts include probes; neither caller count nor non-crawler classification proves successful work.")
    for r in rep["organic_callers_by_day"]:
        print(f"  {r['day']}  callers={r['callers']}  classified_non_crawler={r['callers_that_worked']}")
    print(f"\n== new clients in the last {a.hours}h (not seen in the {a.lookback_days}d before) ==")
    for r in rep["new_clients_in_window"] or [{"client": "(none)", "door": "", "first_seen": "", "rows": 0}]:
        print(f"  {r['client']}  door={r['door']}  first_seen={r['first_seen']}  rows={r['rows']}")
    print(f"\n== rows by day, door, protocol version (last {a.days}d) ==")
    for r in rep["door_by_day"]:
        print(f"  {r['day']}  {str(r['door']):<28} pv={str(r['protocol_version']):<11} rows={r['rows']:<6} "
              f"callers={r['callers']:<4} own={r['own_infra_rows']}")
    print(f"\n== tools/call, excluding configured own user agents (last {a.days}d; probes may remain) ==")
    for r in rep["result_counts_by_tool"]:
        print(f"  {r['tool']:<28} calls={r['calls']:<5} with_count={r['with_count']:<5} zero={r['zero_results']:<4} "
              f"avg={r['avg_results']}  not_ok={r['not_ok']}")
    g = rep["instrumentation_gaps"]
    print(f"\n== instrumentation gaps in the last {a.hours}h: {g['mcp_rows_without_a_door']} MCP rows and "
          f"{g['mcp_http_rows_without_a_door']} /mcp HTTP rows without a door (of {g['rows']} rows) ==")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--hours", type=int, default=1)
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--own-ua", action="append", default=None, help="a user agent that is ours (repeatable); default list in OWN_INFRA_UAS")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--env-file", default=os.environ.get("HATCHLOOP_ENV_FILE", ""))
    a = ap.parse_args()
    dsn = _env("SUPABASE_DB_URL", a.env_file)
    if not dsn:
        print(f"ERROR: SUPABASE_DB_URL is needed (env or {a.env_file})")
        return 2
    conn = connect(dsn)
    try:
        rep = report(conn, days=a.days, hours=a.hours, lookback_days=a.lookback_days,
                     own_uas=tuple(a.own_ua) if a.own_ua else OWN_INFRA_UAS)
    finally:
        conn.close()
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print(rep, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
