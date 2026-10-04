"""migrations/spine/013_usage_events_door_columns.sql, run against a real PostgreSQL - not read, not mocked.

A migration that has only ever been read by its author is a hypothesis. This one is applied, with the real 009 and
010 files before it, to a throwaway database in a throwaway container (docker, the postgres:18.6 image already on
the machine, a random loopback port, random passwords that are never printed) that is built to look like the live
spine (tests/spine_pg.py): the same roles, the same default privileges, the same usage_events table. Then it is
driven the way the service drives it - as the `anon` role, through the function, with the table closed.

Nothing here touches the spine, the VPS or any real database. The module is skipped, with the reason, when docker,
the image or asyncpg is missing, and it is opt-in (SPINE_PG_TESTS=1) because a container start can take minutes on
a busy machine; a skipped test says so in the summary, it does not pass silently.

What is pinned, in the order the rollout relies on it:
  * the file applies after 009 and 010, applies a SECOND time, and refuses to apply as the wrong role;
  * the columns exist, are nullable and have no default; the live writers (v1, v2) are byte-for-byte unchanged and
    still write; the function that is new is the only function that is new, once, with no overload to confuse
    PostgREST;
  * as `anon` the new function writes a full row; the table is still closed; `authenticated` and `public` cannot
    call it; a malformed door, version or count is refused (22023) and writes nothing;
  * history: the old free-text detail is copied into the columns when it is exactly the shape the server wrote,
    nothing is inferred, a value already set is never overwritten, re-running changes nothing;
  * the container's own writer, unmodified, writes door / version / count into the real table, and degrades to v2
    and then v1 when the database has not been migrated;
  * the read-only queries (scripts/door_usage_report.py) answer the question the verdict asks - who came, through
    which door, and is a brand-new client visible within the hour - and cannot write.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg is not installed")

from tests import spine_pg  # noqa: E402

_WHY_NOT = spine_pg.docker_unavailable_reason()
pytestmark = pytest.mark.skipif(bool(_WHY_NOT), reason=_WHY_NOT or "ok")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def spine():
    with spine_pg.start_spine() as s:
        yield s


def q(dsn, sql, *args):
    return run(spine_pg.query(dsn, sql, *args))


def rpc_as_anon(spine, db, fn, **kw):
    """Call a function the way PostgREST does for the container: as `anon`, named arguments."""
    async def _go():
        import asyncpg as pg
        conn = await pg.connect(spine.admin(db))
        try:
            await conn.execute("set role anon")
            args = ", ".join(f"{k} := ${i + 1}" for i, k in enumerate(kw))
            return await conn.fetchval(f"select public.{fn}({args})", *kw.values())
        finally:
            await conn.close()
    return run(_go())


BASE = dict(p_tool="tools/list", p_args_hash=None, p_ip_hash="ab12cd34", p_user_agent="pytest/1", p_key_id=None,
            p_session_kind="anon_agent", p_method="tools/list")


def functions_fingerprint(dsn):
    rows = q(dsn, "select p.oid::regprocedure::text as sig, md5(pg_get_functiondef(p.oid)) as h "
                  "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                  "where n.nspname = 'public' order by 1")
    return {r["sig"]: r["h"] for r in rows}


# ---------------------------------------------------------------------------
# the file itself
# ---------------------------------------------------------------------------

def test_the_starting_point_is_what_the_live_spine_is(spine):
    """If this fails the throwaway is not a faithful stand-in and every other test here proves nothing."""
    cols = q(spine.admin("spine_before"), "select column_name from information_schema.columns "
                                            "where table_schema='public' and table_name='usage_events' order by ordinal_position")
    assert [c["column_name"] for c in cols] == [
        "id", "ts", "tool", "args_hash", "ip_hash", "user_agent", "key_id", "session_kind", "method", "outcome",
        "error_code", "http_status", "latency_ms", "client_name", "client_version", "key_state", "arg_names",
        "requested_name", "detail"]
    v2 = q(spine.admin("spine_before"), "select pg_get_function_identity_arguments(p.oid) as a, r.rolname as owner, p.prosecdef "
                                          "from pg_proc p join pg_roles r on r.oid=p.proowner "
                                          "where p.proname='usage_events_insert_v2'")
    assert len(v2) == 1 and v2[0]["owner"] == "spine_owner" and v2[0]["prosecdef"] is True
    assert v2[0]["a"].count(",") == 16, "v2 has 17 parameters, as measured live"
    assert q(spine.admin("spine_before"), "select count(*) as n from pg_proc where proname = 'usage_events_insert_v3'")[0]["n"] == 0


def test_it_applies_a_second_time_without_error_or_change(spine):
    before = functions_fingerprint(spine.admin("spine_after"))
    run(spine_pg.run_sql(spine.owner("spine_after"), spine_pg.migration(spine_pg.MIGRATION_013)))
    assert functions_fingerprint(spine.admin("spine_after")) == before


def test_it_refuses_to_run_as_the_superuser_and_leaves_nothing_behind(spine):
    """A superuser-owned SECURITY DEFINER callable by anon is the failure the cutover notes warn about."""
    sql = spine_pg.migration(spine_pg.MIGRATION_013)
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as exc:
        run(spine_pg.run_sql(spine.admin("spine_before"), sql))
    assert "spine_owner" in str(exc.value)
    assert q(spine.admin("spine_before"), "select count(*) as n from pg_proc where proname = 'usage_events_insert_v3'")[0]["n"] == 0
    cols = {r["column_name"] for r in q(spine.admin("spine_before"), "select column_name from information_schema.columns "
                                                                      "where table_name='usage_events'")}
    assert "door" not in cols, "the transaction rolled back whole"


def test_the_columns_are_nullable_with_no_default_and_the_right_types(spine):
    rows = q(spine.admin("spine_after"), "select column_name, data_type, is_nullable, column_default "
                                          "from information_schema.columns where table_name='usage_events' "
                                          "and column_name in ('door','protocol_version','result_count') order by 1")
    assert [(r["column_name"], r["data_type"], r["is_nullable"], r["column_default"]) for r in rows] == [
        ("door", "text", "YES", None), ("protocol_version", "text", "YES", None), ("result_count", "integer", "YES", None)]
    assert q(spine.admin("spine_after"), "select count(*) as n from pg_description d join pg_attribute a on a.attrelid = d.objoid and a.attnum = d.objsubid "
                                          "where d.objoid = 'public.usage_events'::regclass and a.attname in ('door','protocol_version','result_count')")[0]["n"] == 3


def test_the_only_function_that_changed_is_the_one_that_is_new(spine):
    before, after = functions_fingerprint(spine.admin("spine_before")), functions_fingerprint(spine.admin("spine_after"))
    new = set(after) - set(before)
    assert len(new) == 1 and next(iter(new)).startswith("usage_events_insert_v3(")
    assert {k: v for k, v in after.items() if k in before} == before, "every pre-existing function is byte-for-byte the same"
    assert q(spine.admin("spine_after"), "select count(*) as n from pg_proc where proname = 'usage_events_insert_v3'")[0]["n"] == 1, \
        "one function, no overload: PostgREST would answer PGRST203 for an ambiguous pair"


def test_the_new_function_is_owned_by_spine_owner_and_granted_to_anon_and_service_role_only(spine):
    r = q(spine.admin("spine_after"),
          "select p.prosecdef, o.rolname as owner, o.rolbypassrls, p.proconfig, "
          "has_function_privilege('anon', p.oid, 'EXECUTE') as anon, "
          "has_function_privilege('authenticated', p.oid, 'EXECUTE') as authenticated, "
          "has_function_privilege('public', p.oid, 'EXECUTE') as public, "
          "has_function_privilege('service_role', p.oid, 'EXECUTE') as service_role "
          "from pg_proc p join pg_roles o on o.oid = p.proowner where p.proname = 'usage_events_insert_v3'")[0]
    assert (r["prosecdef"], r["owner"], r["rolbypassrls"]) == (True, "spine_owner", True)
    assert any(c.startswith("search_path=") for c in r["proconfig"])
    assert (r["anon"], r["authenticated"], r["public"], r["service_role"]) == (True, False, False, True)


def test_the_table_is_still_closed_to_anon(spine):
    for sql in ("select count(*) from public.usage_events", "insert into public.usage_events (tool, session_kind) values ('x','crawler')"):
        async def _go(sql=sql):
            import asyncpg as pg
            c = await pg.connect(spine.admin("spine_after"))
            try:
                await c.execute("set role anon")
                await c.execute(sql)
            finally:
                await c.close()
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(_go())


# ---------------------------------------------------------------------------
# writing, as anon
# ---------------------------------------------------------------------------

def test_a_full_row_is_written_through_the_new_function(spine):
    out = rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, p_outcome="ok", p_error_code=None,
                      p_http_status=200, p_latency_ms=12, p_client_name="Claude", p_client_version="1.2",
                      p_key_state="none", p_arg_names=["b", "a"], p_requested_name=None, p_detail="door=sanctions-screening",
                      p_door="sanctions-screening", p_protocol_version="2025-06-18", p_result_count=8)
    row_id = json.loads(out)["id"]
    r = q(spine.admin("spine_after"), "select door, protocol_version, result_count, outcome, client_name, arg_names, detail, "
                                       "session_kind, method from public.usage_events where id = $1", row_id)[0]
    assert (r["door"], r["protocol_version"], r["result_count"]) == ("sanctions-screening", "2025-06-18", 8)
    assert (r["outcome"], r["client_name"], list(r["arg_names"]), r["session_kind"]) == ("ok", "Claude", ["b", "a"], "anon_agent")


def test_a_call_with_only_the_original_seventeen_arguments_still_works_and_leaves_the_new_columns_null(spine):
    out = rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, p_outcome="ok")
    r = q(spine.admin("spine_after"), "select door, protocol_version, result_count from public.usage_events where id = $1", json.loads(out)["id"])[0]
    assert (r["door"], r["protocol_version"], r["result_count"]) == (None, None, None)


def test_every_label_the_server_can_produce_is_accepted(spine):
    from agent_interface import door_label, profiles, retired_doors
    labels = [door_label.for_profile(None), door_label.UNKNOWN_DOOR, *profiles.PROFILES,
              *[door_label.for_retired(s) for s in retired_doors.RETIRED_DOORS]]
    for label in labels:
        out = rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, p_door=label)
        assert json.loads(out)["id"], label


@pytest.mark.parametrize("field,value", [
    ("p_door", "Has Capitals"), ("p_door", "x" * 80), ("p_door", ""), ("p_door", "retired:"), ("p_door", "a b"),
    ("p_door", "door\nwith newline"), ("p_door", "'; drop table usage_events;--"), ("p_door", "retired:retired:x"),
    ("p_protocol_version", "not-a-date"), ("p_protocol_version", "2025-6-18"), ("p_protocol_version", "2025-06-18\n"),
    ("p_protocol_version", ""), ("p_protocol_version", "20250618"),
    ("p_result_count", -1), ("p_result_count", 1000001), ("p_result_count", 2147483647),
])
def test_a_malformed_door_version_or_count_is_refused_and_writes_nothing(spine, field, value):
    before = q(spine.admin("spine_after"), "select count(*) as n from public.usage_events")[0]["n"]
    with pytest.raises(asyncpg.DataError) as exc:           # SQLSTATE 22023 invalid_parameter_value
        rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, **{field: value})
    assert getattr(exc.value, "sqlstate", "") == "22023"
    assert str(value)[:20] not in str(exc.value) or value == "", "the refused value is not echoed back"
    assert q(spine.admin("spine_after"), "select count(*) as n from public.usage_events")[0]["n"] == before


def test_the_boundaries_themselves_are_accepted(spine):
    for kw in ({"p_result_count": 0}, {"p_result_count": 1000000}, {"p_door": "a"}, {"p_door": "a" * 63},
               {"p_door": "retired:" + "a" * 63}, {"p_protocol_version": "2026-07-28"}):
        assert json.loads(rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, **kw))["id"], kw


def test_the_checks_v2_made_are_still_made(spine):
    for kw in ({"p_session_kind": "made-up"}, {"p_outcome": "made-up"}, {"p_key_state": "made-up"}):
        args = {**BASE, **kw}
        with pytest.raises(asyncpg.DataError):
            rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **args)
    assert json.loads(rpc_as_anon(spine, "spine_after", "usage_events_insert_v3", **BASE, p_outcome="notification"))["id"]


def test_authenticated_and_public_cannot_call_it(spine):
    async def _go(role):
        import asyncpg as pg
        c = await pg.connect(spine.admin("spine_after"))
        try:
            await c.execute(f"set role {role}")
            await c.fetchval("select public.usage_events_insert_v3(p_tool := 'x', p_args_hash := null, p_ip_hash := null, "
                             "p_user_agent := null, p_key_id := null, p_session_kind := 'crawler', p_method := 'x')")
        finally:
            await c.close()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(_go("authenticated"))


# ---------------------------------------------------------------------------
# the live writers are untouched
# ---------------------------------------------------------------------------

def test_v2_and_v1_still_write_and_leave_the_new_columns_null(spine):
    v2 = rpc_as_anon(spine, "spine_after", "usage_events_insert_v2", **BASE, p_outcome="ok", p_http_status=200)
    v1 = rpc_as_anon(spine, "spine_after", "usage_events_insert", **BASE)
    for out in (v2, v1):
        r = q(spine.admin("spine_after"), "select door, protocol_version, result_count from public.usage_events where id = $1", json.loads(out)["id"])[0]
        assert (r["door"], r["protocol_version"], r["result_count"]) == (None, None, None)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def test_the_old_detail_text_is_copied_into_the_columns_only_where_it_is_exactly_the_shape_we_wrote(spine):
    db = spine.clone_before("spine_history")          # its own copy: this test migrates it
    admin = spine.admin(db)
    seed = {
        "legacy_door": "door=sanctions-screening",
        "modern_both": "door=compliance-check pv=2026-07-28",
        "modern_only": "pv=2026-07-28",
        "none": None,
        "http_row": "POST /mcp/not-a-door",
        "evil_door": "door=EVIL<script>",
        "evil_and_real": "door=a door=b",
        "embedded": "xdoor=sneaky",
        "trailing": "door=appointment-booking extra words",
        "bad_pv": "pv=2026-7-28",
        "long_door": "door=" + "a" * 70,
    }
    for tool, detail in seed.items():
        run(spine_pg.run_sql(admin, f"insert into public.usage_events (tool, session_kind, method, detail, outcome) values "
                                    f"('{tool}', 'crawler', 'tools/list', {'null' if detail is None else repr(detail)}, 'ok')"))
    ts_before = {r["tool"]: r["ts"] for r in q(admin, "select tool, ts from public.usage_events")}
    run(spine_pg.run_sql(spine.owner(db), spine_pg.migration(spine_pg.MIGRATION_013)))
    got = {r["tool"]: (r["door"], r["protocol_version"], r["outcome"], r["detail"], r["ts"]) for r in
           q(admin, "select tool, door, protocol_version, outcome, detail, ts from public.usage_events")}
    assert got["legacy_door"][:2] == ("sanctions-screening", None)
    assert got["modern_both"][:2] == ("compliance-check", "2026-07-28")
    assert got["modern_only"][:2] == (None, "2026-07-28")
    for k in ("none", "http_row", "evil_door", "embedded", "bad_pv", "long_door"):
        assert got[k][:2] == (None, None), k
    assert got["evil_and_real"][0] in ("a", None), "the first well-formed token, never a blend"
    assert got["trailing"][0] == "appointment-booking"
    for k, v in got.items():
        assert v[2] == "ok" and v[4] == ts_before[k], "nothing but door and protocol_version changed"
        assert v[3] == seed[k], "the detail text itself is untouched"


def test_a_value_that_is_already_set_is_never_overwritten_and_a_rerun_changes_nothing(spine):
    admin = spine.admin("spine_after")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    run(spine_pg.run_sql(admin, "insert into public.usage_events (tool, session_kind, method, detail, door, protocol_version) "
                                "values ('x', 'crawler', 'tools/list', 'door=sanctions-screening pv=2026-07-28', 'agent-broker', '2025-06-18')"))
    run(spine_pg.run_sql(spine.owner("spine_after"), spine_pg.migration(spine_pg.MIGRATION_013)))
    r = q(admin, "select door, protocol_version from public.usage_events")[0]
    assert (r["door"], r["protocol_version"]) == ("agent-broker", "2025-06-18")


def test_rows_written_between_the_apply_and_the_deploy_are_picked_up_by_running_it_again(spine):
    admin = spine.admin("spine_after")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    old_image = rpc_as_anon(spine, "spine_after", "usage_events_insert_v2", **BASE, p_outcome="ok", p_detail="door=sanctions-screening")
    row_id = json.loads(old_image)["id"]
    assert q(admin, "select door from public.usage_events where id = $1", row_id)[0]["door"] is None
    run(spine_pg.run_sql(spine.owner("spine_after"), spine_pg.migration(spine_pg.MIGRATION_013)))
    assert q(admin, "select door from public.usage_events where id = $1", row_id)[0]["door"] == "sanctions-screening"


# ---------------------------------------------------------------------------
# the container's own writer, unmodified, against the real SQL
# ---------------------------------------------------------------------------

def _drive_dispatcher(rpc, payload, *, profile=None, headers=None):
    from agent_interface.mcp_server import handle_mcp_request
    from billing import usage_logger as ul

    async def _go():
        out = await handle_mcp_request(payload, headers=headers or {"user-agent": "pytest-pg/1"}, profile=profile)
        for _ in range(200):
            if not ul._pending_tasks:
                break
            await asyncio.sleep(0.05)
        return out
    return run(_go())


def test_the_dispatcher_writes_door_version_and_count_into_the_real_table(spine, monkeypatch):
    import storage.supabase_client as sb
    from billing import usage_logger as ul
    admin = spine.admin("spine_after")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    rpc = spine_pg.PgRpc(admin)
    monkeypatch.setattr(sb, "rpc", rpc)
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    _drive_dispatcher(rpc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, profile="sanctions-screening",
                      headers={"user-agent": "pytest-pg/1", "mcp-protocol-version": "2025-06-18"})
    _drive_dispatcher(rpc, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-03-26",
                                                                                       "clientInfo": {"name": "PgClient", "version": "1"}}})
    rows = q(admin, "select method, door, protocol_version, result_count, client_name, outcome, detail from public.usage_events order by id")
    assert [dict(r) for r in rows] == [
        {"method": "tools/list", "door": "sanctions-screening", "protocol_version": "2025-06-18", "result_count": 8,
         "client_name": None, "outcome": "ok", "detail": "door=sanctions-screening"},
        {"method": "initialize", "door": "agent-broker", "protocol_version": "2025-03-26", "result_count": None,
         "client_name": "PgClient", "outcome": "ok", "detail": None},
    ]
    assert {c for c in rpc.calls if c.startswith("usage_events")} == {"usage_events_insert_v3"}


def test_a_retired_door_over_http_writes_a_retired_crawler_row_into_the_real_table(spine, monkeypatch):
    """The route and the HTTP layer produce the events (TestClient closes its loop after each request, which would
    cancel the logger's fire-and-forget task, so they are captured and then written with the REAL logger)."""
    from fastapi.testclient import TestClient
    import main
    import storage.supabase_client as sb
    from billing import usage_logger as ul
    admin = spine.admin("spine_after")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    events = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: events.append(e))
    monkeypatch.setattr(sb, "rpc", spine_pg.PgRpc(admin))
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    main._rl_buckets.clear()
    client = TestClient(main.app)
    assert client.post("/mcp/data-enrichment", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"user-agent": "a-scorer/1"}).status_code == 200
    assert client.get("/mcp/pdf-generator").status_code == 410
    main._rl_buckets.clear()
    assert len(events) == 2
    for e in events:
        run(ul.log_usage_outcome(e))
    rows = q(admin, "select method, door, session_kind, result_count, outcome, http_status from public.usage_events order by id")
    assert [dict(r) for r in rows] == [
        {"method": "tools/list", "door": "retired:data-enrichment", "session_kind": "crawler", "result_count": 1,
         "outcome": "ok", "http_status": 200},
        {"method": "http", "door": "retired:pdf-generator", "session_kind": "crawler", "result_count": None,
         "outcome": "http_error", "http_status": 410},
    ]


def test_against_a_database_that_has_not_been_migrated_the_writer_degrades_to_v2_and_keeps_every_outcome_column(spine, monkeypatch, caplog):
    import storage.supabase_client as sb
    from billing import usage_logger as ul
    admin = spine.admin("spine_before")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    rpc = spine_pg.PgRpc(admin)
    monkeypatch.setattr(sb, "rpc", rpc)
    ul._v2_missing_until = ul._v3_missing_until = 0.0
    with caplog.at_level("ERROR"):
        _drive_dispatcher(rpc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, profile="compliance-check",
                          headers={"user-agent": "pytest-pg/1", "mcp-protocol-version": "2025-06-18"})
    rows = q(admin, "select method, outcome, http_status, detail from public.usage_events")
    assert [dict(r) for r in rows] == [{"method": "tools/list", "outcome": "ok", "http_status": 200, "detail": "door=compliance-check"}]
    assert [c for c in rpc.calls if c.startswith("usage_events")] == ["usage_events_insert_v3", "usage_events_insert_v2"]
    assert "usage_log_v3_missing" in caplog.text
    ul._v3_missing_until = 0.0


# ---------------------------------------------------------------------------
# the verification script
# ---------------------------------------------------------------------------

def test_the_verification_script_tells_before_from_after(spine):
    pytest.importorskip("psycopg2")
    v = spine_pg.load_script("verify_spine_013")
    before = v.catalog(spine.owner("spine_before"))
    after = v.catalog(spine.owner("spine_after"))
    assert v.judge("before", before, None) == []
    assert v.judge("after", after, None) == []
    assert any("door" in p for p in v.judge("after", before, None))
    assert any("already" in p for p in v.judge("before", after, None))
    assert after["functions"]["usage_events_insert_v3"]["security_definer"] is True
    assert after["row_counts"]["usage_events"] >= 0
    assert "token" not in json.dumps(after).lower()


# ---------------------------------------------------------------------------
# the read-only queries
# ---------------------------------------------------------------------------

async def _exec(dsn, sql, *args):
    import asyncpg as pg
    c = await pg.connect(dsn)
    try:
        await c.execute(sql, *args)
    finally:
        await c.close()


def _seed_report(spine):
    """Rows with the intended meaning written by hand next to each one (organic / worked / external), so the
    expectations below are not the query logic restated."""
    admin = spine.admin("spine_after")
    run(spine_pg.run_sql(admin, "truncate public.usage_events"))
    now = dt.datetime.now(dt.timezone.utc)
    seeded = []

    def row(ts, *, organic=False, worked=False, **kw):
        base = dict(tool="x", session_kind="anon_agent", method="tools/call", user_agent="ua", ip_hash="ip", door="agent-broker",
                    outcome="ok", client_name=None, result_count=None, protocol_version=None, detail=None)
        base.update(kw)
        cols = ", ".join(["ts"] + list(base))
        vals = ", ".join(["$1"] + [f"${i + 2}" for i in range(len(base))])
        run(_exec(admin, f"insert into public.usage_events ({cols}) values ({vals})", ts, *base.values()))
        seeded.append(dict(base, ts=ts, organic=organic, worked=worked))

    day1 = now - dt.timedelta(days=1)
    claude = dict(user_agent="ClaudeBot/x", ip_hash="aaaa1111")
    row(day1, organic=True, worked=True, tool="find_business", result_count=3, client_name="Claude", **claude)
    row(day1, organic=True, method="initialize", session_kind="crawler", tool="initialize", client_name="Claude", **claude)
    row(day1, organic=True, user_agent="Grok/1", ip_hash="bbbb2222", door="sanctions-screening", method="tools/list",
        session_kind="crawler", tool="tools/list", result_count=8)
    row(day1, user_agent="hatchloop-live-verify/1", ip_hash="dddd4444", tool="find_business", result_count=2)            # ours
    row(day1, user_agent="mcpbeat/1", ip_hash="cccc3333", door="retired:data-enrichment", method="tools/list",
        session_kind="crawler", tool="tools/list", result_count=1)                                                       # retired
    row(day1, user_agent="x", ip_hash="eeee5555", door="unknown", method="http", outcome="http_error", tool="http")       # probe
    row(day1, user_agent="x", ip_hash="ffff6666", door=None, method="http", outcome="http_error", tool="http", detail="POST /ops/nope")
    row(day1, organic=True, worked=True, tool="find_business", result_count=0, **claude)
    row(day1, organic=True, worked=True, tool="find_business", result_count=None, outcome="tool_failure", **claude)
    # a client that is new in the last hour, and one that was already known
    row(now - dt.timedelta(minutes=10), organic=True, user_agent="Muse/0.1", ip_hash="gggg7777", client_name="Muse",
        method="tools/list", session_kind="crawler", tool="tools/list", result_count=23)
    row(now - dt.timedelta(minutes=5), organic=True, worked=True, client_name="Claude", tool="find_business", result_count=1, **claude)
    # two rows the old image would write: an MCP row with no door, and an /mcp HTTP row with no door
    row(now - dt.timedelta(minutes=3), organic=True, door=None, method="tools/list", tool="tools/list", session_kind="crawler", **claude)
    row(now - dt.timedelta(minutes=2), door=None, method="http", outcome="http_error", tool="http", detail="POST /mcp/x", **claude)
    return seeded


def test_the_report_answers_who_came_through_which_door(spine):
    pytest.importorskip("psycopg2")
    r = spine_pg.load_script("door_usage_report")
    seeded = _seed_report(spine)
    conn = r.connect(spine.owner("spine_after"))
    try:
        rep = r.report(conn, days=7, hours=1, lookback_days=30)
    finally:
        conn.close()

    rows_by_door: dict = {}
    for d in rep["door_by_day"]:
        rows_by_door[d["door"]] = rows_by_door.get(d["door"], 0) + d["rows"]
    assert rows_by_door == {"agent-broker": 7, "sanctions-screening": 1, "retired:data-enrichment": 1, "unknown": 1, None: 3}
    assert sum(d["own_infra_rows"] for d in rep["door_by_day"]) == 1

    expect_days: dict = {}
    for s in seeded:
        if s["organic"]:
            day = s["ts"].astimezone(dt.timezone.utc).date()
            callers, workers = expect_days.setdefault(day, (set(), set()))
            callers.add((s["ip_hash"], s["user_agent"]))
            if s["worked"]:
                workers.add((s["ip_hash"], s["user_agent"]))
    assert {o["day"]: (o["callers"], o["callers_that_worked"]) for o in rep["organic_callers_by_day"]} == \
        {d: (len(c), len(w)) for d, (c, w) in expect_days.items()}, \
        "own-infrastructure, retired-door, unknown-door and HTTP-layer rows are not callers"

    assert {n["client"] for n in rep["new_clients_in_window"]} == {"Muse"}, \
        "a client first seen inside the window; Claude was known before it"
    assert [n["door"] for n in rep["new_clients_in_window"]] == ["agent-broker"]

    tools = {t["tool"]: t for t in rep["result_counts_by_tool"]}
    fb = tools["find_business"]
    assert (fb["calls"], fb["with_count"], fb["zero_results"], fb["not_ok"]) == (4, 3, 1, 1), \
        "our own live-verification call is not an external caller's find_business"
    assert float(fb["avg_results"]) == pytest.approx((3 + 0 + 1) / 3, abs=0.01)

    assert rep["instrumentation_gaps"] == {"mcp_rows_without_a_door": 1, "mcp_http_rows_without_a_door": 1, "rows": 4}


def test_the_report_runs_only_on_a_read_only_session_and_that_session_cannot_write(spine):
    psycopg2 = pytest.importorskip("psycopg2")
    r = spine_pg.load_script("door_usage_report")
    writable = psycopg2.connect(spine.owner("spine_after"))
    try:
        with pytest.raises(ValueError):
            r.report(writable)
    finally:
        writable.close()
    conn = r.connect(spine.owner("spine_after"))
    try:
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            conn.cursor().execute("delete from public.usage_events")
    finally:
        conn.close()


def test_every_query_in_the_report_is_a_select(spine):
    import re
    r = spine_pg.load_script("door_usage_report")
    for name, sql in r.QUERIES.items():
        assert re.match(r"\s*(with|select)\b", sql, re.I), name
        assert not re.search(r"\b(insert|update|delete|drop|alter|truncate|create|grant)\b", sql, re.I), name


def test_a_user_agent_named_as_ours_is_dropped_from_the_organic_figure(spine):
    """--own-ua extends the list: that is how a newly discovered probe is taken out of the number."""
    pytest.importorskip("psycopg2")
    r = spine_pg.load_script("door_usage_report")
    _seed_report(spine)
    conn = r.connect(spine.owner("spine_after"))
    try:
        base = sum(o["callers"] for o in r.report(conn)["organic_callers_by_day"])
        extended = tuple(r.OWN_INFRA_UAS) + ("Grok/1",)
        assert sum(o["callers"] for o in r.report(conn, own_uas=extended)["organic_callers_by_day"]) == base - 1
        assert r.report(conn, own_uas=())["instrumentation_gaps"]["rows"] == 4, "an empty own-UA list is accepted"
    finally:
        conn.close()
