"""migrations/spine/012_operations_upsert_fix.sql, run against a real PostgreSQL 18.6 - the function is CALLED,
before the migration and after it.

The structural test next to this one reads the migration. A plpgsql body is compiled lazily, so reading it proves
little: the broken definition this migration replaces was accepted by `create function` and failed only on the
first call, silently, for a month. This module closes that gap the only way that counts: it builds the schema the
live spine had (the table with no `updated_at` and a jsonb `result_json`; the old `operations_upsert` copied
verbatim from `pg_get_functiondef`; the owner `spine_owner` with BYPASSRLS; the live ACL, which still carried
`authenticated`), calls the function as the `anon` role the service uses, and watches it fail. Then it applies
the migration as `spine_owner` - the way `scripts/apply_sql.py` does - and drives the same calls again.

Two databases are cut from one template in a throwaway container, so the BEFORE and AFTER tests are independent
of each other and of the order they run in. Nothing here touches the spine, the VPS or any real database.

What is pinned:
  BEFORE  the old function fails 42703 on the missing column; with the column added it fails 42804 on text into
          jsonb; and the production writer (`storage.outcome_store._supabase_upsert`) therefore returns False and
          leaves no row, so a fresh process answers "unknown operation" for an operation that ran.
  AFTER   the 3 legacy rows are untouched and gain `updated_at`; insert then update of one id leaves one row;
          a blank result (NULL, '' or spaces) is "no result", not a parse error; invalid JSON is refused loudly
          (22P02) and changes nothing; an empty id is refused (22023); Arabic text and nested JSON round-trip as
          jsonb; the boundary is unchanged - SECURITY DEFINER, owner spine_owner (BYPASSRLS), search_path pinned,
          EXECUTE for anon and service_role only (`authenticated` was revoked on purpose), no direct table access,
          the signature the caller sends by name; applying the file again changes nothing; ten simultaneous
          writers of one id leave one row; and the production code's write is read back by a FRESH store, by
          operation id and by the provider's appointment id (the cancellation-ownership lookup).

Opt-in with OAUTH_PG_TESTS=1 (the one switch for every database-backed test here; the name is historical).
Skipped, with the reason, when docker, the image or asyncpg is missing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg is not installed")

from tests import oauth_pg  # noqa: E402
from tests.unit.test_spine_012_operations_upsert import OLD_DEFINITION  # noqa: E402

_WHY_NOT = oauth_pg.docker_unavailable_reason()
pytestmark = pytest.mark.skipif(bool(_WHY_NOT), reason=_WHY_NOT or "ok")

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "spine" / "012_operations_upsert_fix.sql"

SIGNATURE = ("p_operation_id text, p_tool text, p_status text, p_reason_code text, p_appointment_id text, "
             "p_result_json text, p_agent_id text")
LIVE_ACL_BEFORE = {"spine_owner=X/spine_owner", "anon=X/spine_owner", "authenticated=X/spine_owner",
                   "service_role=X/spine_owner"}
ACL_AFTER = {"spine_owner=X/spine_owner", "anon=X/spine_owner", "service_role=X/spine_owner"}

# ---- the schema the live spine had on 2026-10-04 (measured read-only: columns, constraint, indexes, RLS, policy,
# ---- function owner and ACL) --------------------------------------------------------------------------------
ROLE_SQL = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'spine_owner') then
    create role spine_owner nologin bypassrls;
  end if;
end $$;
grant create, usage on schema public to spine_owner;
"""

LEGACY_TABLE_SQL = """
create table public.operations (
    operation_id   text primary key,
    ts             timestamptz not null default now(),
    tool           text not null,
    status         text not null,
    reason_code    text,
    result_json    jsonb,
    agent_id       text,
    appointment_id text
);
create index operations_agent_id_idx on public.operations (agent_id) where agent_id is not null;
create index operations_appointment_id_idx on public.operations (appointment_id) where appointment_id is not null;
alter table public.operations enable row level security;
create policy service_role_full_access on public.operations for all to service_role using (true) with check (true);
revoke all on public.operations from anon, authenticated, public;

create function public.operations_get_by_id(p_operation_id text) returns jsonb
language sql security definer set search_path = public as $$
    select to_jsonb(o) from operations o where o.operation_id = p_operation_id limit 1;
$$;
create function public.operations_get_by_appointment_id(p_appointment_id text) returns jsonb
language sql security definer set search_path = public as $$
    select to_jsonb(o) from operations o
    where o.appointment_id = p_appointment_id and o.reason_code = 'appointment_confirmed' limit 1;
$$;

insert into public.operations (operation_id, ts, tool, status, reason_code, result_json, agent_id, appointment_id)
values
 ('legacy-1', '2026-08-23 13:20:29+00', 'schedule_appointment', 'complete', 'appointment_confirmed',
  '{"status": "success", "result": {"appointment_id": "appt-legacy"}}', 'agent-old', 'appt-legacy'),
 ('legacy-2', '2026-09-01 21:19:41+00', 'send_message', 'complete', null, '{"status": "success"}', null, null),
 ('legacy-3', '2026-08-30 08:00:00+00', 'send_message', 'pending', null, null, 'agent-old', null);
"""

LEGACY_GRANTS_SQL = """
revoke all on function public.operations_get_by_id(text) from public;
grant execute on function public.operations_get_by_id(text) to anon, authenticated, service_role;
revoke all on function public.operations_get_by_appointment_id(text) from public;
grant execute on function public.operations_get_by_appointment_id(text) to anon, authenticated, service_role;
revoke all on function public.operations_upsert(text, text, text, text, text, text, text) from public;
grant execute on function public.operations_upsert(text, text, text, text, text, text, text)
    to anon, authenticated, service_role;
"""

OUTCOME = {
    "status": "success",
    "reason_code": "appointment_confirmed",
    "result": {"appointment_id": "appt-prod-1", "when": "2026-10-05T09:00:00+04:00"},
    "quota": {"remaining": 7},   # transient per-call block: must never be persisted
}


def _db(dsn: str, name: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/" + name


async def _connect_with_retry(dsn: str):
    for _ in range(40):
        try:
            return await asyncpg.connect(dsn, timeout=5)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
    raise AssertionError("the throwaway database never accepted a connection")


async def _build(dsn: str) -> SimpleNamespace:
    """Legacy schema in the template database, two databases cut from it, the migration on one (twice)."""
    c = await _connect_with_retry(dsn)
    try:
        await c.execute(ROLE_SQL)
        await c.execute("set role spine_owner")
        await c.execute(LEGACY_TABLE_SQL)
        await c.execute(OLD_DEFINITION)          # verbatim from the live spine, created (and so owned) by spine_owner
        await c.execute(LEGACY_GRANTS_SQL)
        await c.execute("reset role")
    finally:
        await c.close()
    # a template database must have no other session on it; the server may need a moment to reap ours
    admin = await _connect_with_retry(_db(dsn, "template1"))
    try:
        for name in ("before_db", "after_db"):
            for _ in range(40):
                try:
                    await admin.execute(f"create database {name} template postgres")
                    break
                except asyncpg.PostgresError as exc:
                    if exc.sqlstate != "55006":      # object_in_use
                        raise
                    await asyncio.sleep(0.5)
            else:
                raise AssertionError("the template database stayed busy")
    finally:
        await admin.close()
    sql = MIGRATION.read_text(encoding="utf-8")
    c = await _connect_with_retry(_db(dsn, "after_db"))
    try:
        await c.execute("set role spine_owner")   # the ship step connects as the function owner
        await c.execute(sql)
        await c.execute(sql)                      # idempotent: applied twice
    finally:
        await c.close()
    return SimpleNamespace(before=_db(dsn, "before_db"), after=_db(dsn, "after_db"))


@pytest.fixture(scope="module")
def dbs():
    with oauth_pg.start_postgres() as dsn:
        yield asyncio.run(_build(dsn))


# ---- small helpers ---------------------------------------------------------------------------------------------
run = asyncio.run


async def upsert(dsn, op, *, tool="send_message", status="complete", reason=None, appt=None, result=None,
                 agent="agent-a", role="anon"):
    """operations_upsert as the service calls it: a role-limited session, the seven named arguments."""
    c = await asyncpg.connect(dsn)
    try:
        if role:
            await c.execute(f"set role {role}")
        row = await c.fetchval("select public.operations_upsert($1, $2, $3, $4, $5, $6, $7)",
                               op, tool, status, reason, appt, result, agent)
    finally:
        await c.close()
    return json.loads(row) if isinstance(row, str) else row


async def state_of(coro):
    """The SQLSTATE the awaited call raised, or None when it did not raise."""
    try:
        await coro
    except asyncpg.PostgresError as exc:
        return exc.sqlstate
    return None


def acl(rows) -> set:
    text = rows[0][0]
    return set(text.strip("{}").split(","))


async def _drain():
    """set_complete also fires a background write; let it land before the loop closes."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def q(dsn, sql, *args):
    return [tuple(r) for r in run(oauth_pg.query(dsn, sql, *args))]


LEGACY_ROWS = ("select operation_id, ts, tool, status, reason_code, result_json::text, agent_id, appointment_id "
               "from public.operations where operation_id like 'legacy-%' order by 1")


# =================================================== BEFORE the migration ========================================
def test_before_the_old_function_dies_on_the_missing_column(dbs):
    """Production's failure, reproduced: operations.updated_at does not exist (42703) - and nothing is recorded."""
    assert q(dbs.before, "select count(*) from information_schema.columns where table_name = 'operations' "
                         "and column_name = 'updated_at'")[0][0] == 0
    assert run(state_of(upsert(dbs.before, "zz-before-1", result='{"ok": true}'))) == "42703"
    assert q(dbs.before, "select count(*) from public.operations where operation_id = 'zz-before-1'")[0][0] == 0


def test_before_the_column_alone_is_not_enough_text_into_jsonb_fails_next(dbs):
    """The second defect hides behind the first: with the column present the call fails 42804. Rolled back."""
    async def go():
        c = await asyncpg.connect(dbs.before)
        try:
            tx = c.transaction()
            await tx.start()
            try:
                await c.execute("alter table public.operations add column updated_at timestamptz not null "
                                "default now()")
                return await state_of(c.fetchval(
                    "select public.operations_upsert('zz-before-2', 't', 's', null, null, '{\"ok\": true}', 'a')"))
            finally:
                await tx.rollback()
        finally:
            await c.close()
    assert run(go()) == "42804"
    assert q(dbs.before, "select count(*) from information_schema.columns where table_name = 'operations' "
                         "and column_name = 'updated_at'")[0][0] == 0


def test_before_the_production_writer_records_nothing(dbs, monkeypatch):
    """storage.outcome_store: the write reports False, no row exists, and a fresh process says 'unknown'."""
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.before))

    async def go():
        wrote = await store_mod.OutcomeStore().set_complete_durable(
            "op-before-1", dict(OUTCOME), tool="schedule_appointment", agent_id="agent-owner")
        await _drain()
        return wrote, await store_mod.OutcomeStore().get_async("op-before-1")
    wrote, found = run(go())
    assert wrote is False
    assert found is None
    assert q(dbs.before, "select count(*) from public.operations where operation_id = 'op-before-1'")[0][0] == 0


def test_before_the_fixture_matches_the_live_boundary(dbs):
    """The BEFORE world is the live one: owner spine_owner (BYPASSRLS), definer, and `authenticated` still granted."""
    rows = q(dbs.before, "select p.prosecdef, r.rolname, r.rolbypassrls, p.proacl::text, p.proconfig::text, "
                         "pg_get_function_identity_arguments(p.oid) from pg_proc p "
                         "join pg_roles r on r.oid = p.proowner where p.proname = 'operations_upsert'")
    assert len(rows) == 1
    definer, owner, bypass, proacl, config, args = rows[0]
    assert (definer, owner, bypass) == (True, "spine_owner", True)
    assert set(proacl.strip("{}").split(",")) == LIVE_ACL_BEFORE
    assert config == "{search_path=public}"
    assert args == SIGNATURE


# =================================================== AFTER the migration =========================================
def test_after_the_legacy_rows_are_untouched_and_gain_updated_at(dbs):
    assert q(dbs.after, LEGACY_ROWS) == q(dbs.before, LEGACY_ROWS)
    cols = q(dbs.after, "select data_type, is_nullable, column_default from information_schema.columns "
                        "where table_name = 'operations' and column_name = 'updated_at'")
    assert len(cols) == 1
    assert cols[0][0] == "timestamp with time zone" and cols[0][1] == "NO" and "now()" in cols[0][2]
    assert q(dbs.after, "select count(*) from public.operations where updated_at is null")[0][0] == 0
    assert q(dbs.after, "select count(*) from public.operations where operation_id like 'legacy-%'")[0][0] == 3


def test_after_insert_then_update_of_one_id_leaves_one_row(dbs):
    first = run(upsert(dbs.after, "zz-ins-1", tool="schedule_appointment", status="pending",
                       result='{"status": "pending"}', agent="agent-a"))
    assert first["operation_id"] == "zz-ins-1" and first["status"] == "pending" and first["tool"] == "schedule_appointment"
    assert first["result_json"] == {"status": "pending"}     # jsonb: an object, not a string holding JSON
    assert first["updated_at"] and first["ts"] and first["agent_id"] == "agent-a"

    second = run(upsert(dbs.after, "zz-ins-1", tool="schedule_appointment", status="complete",
                        reason="appointment_confirmed", appt="appt-zz",
                        result='{"status": "success", "result": {"appointment_id": "appt-zz"}}', agent="agent-a"))
    assert second["status"] == "complete" and second["reason_code"] == "appointment_confirmed"
    assert second["appointment_id"] == "appt-zz"
    assert second["result_json"]["result"]["appointment_id"] == "appt-zz"
    assert datetime.fromisoformat(second["updated_at"]) > datetime.fromisoformat(first["updated_at"])
    assert q(dbs.after, "select count(*) from public.operations where operation_id = 'zz-ins-1'")[0][0] == 1


def test_after_the_readers_return_what_the_writer_wrote(dbs):
    run(upsert(dbs.after, "zz-read-1", tool="schedule_appointment", status="complete",
               reason="appointment_confirmed", appt="appt-read-1", result='{"status": "success"}',
               agent="agent-reader"))

    async def read(fn, arg):
        c = await asyncpg.connect(dbs.after)
        try:
            await c.execute("set role anon")
            row = await c.fetchval(f"select public.{fn}($1)", arg)
        finally:
            await c.close()
        return json.loads(row) if isinstance(row, str) else row
    by_id = run(read("operations_get_by_id", "zz-read-1"))
    by_appt = run(read("operations_get_by_appointment_id", "appt-read-1"))
    assert by_id["operation_id"] == "zz-read-1" and by_id["result_json"] == {"status": "success"}
    assert by_appt["operation_id"] == "zz-read-1" and by_appt["agent_id"] == "agent-reader"
    assert run(read("operations_get_by_id", "zz-no-such-operation")) is None


@pytest.mark.parametrize("n, blank", [(0, None), (1, ""), (2, "   "), (3, "\n\t ")])
def test_after_a_blank_result_means_no_result_on_insert_and_on_update(dbs, n, blank):
    key = f"zz-blank-{n}"
    inserted = run(upsert(dbs.after, key, result=blank))
    assert inserted["result_json"] is None
    run(upsert(dbs.after, key, result='{"was": "here"}'))
    assert q(dbs.after, "select result_json::text from public.operations where operation_id = $1", key)[0][0] \
        == '{"was": "here"}'
    updated = run(upsert(dbs.after, key, status="failed", result=blank))      # full overwrite, not a merge
    assert updated["status"] == "failed" and updated["result_json"] is None
    assert q(dbs.after, "select result_json is null from public.operations where operation_id = $1", key)[0][0]


def test_after_invalid_json_is_refused_loudly_and_changes_nothing(dbs):
    assert run(state_of(upsert(dbs.after, "zz-bad-new", result="{not json"))) == "22P02"
    assert q(dbs.after, "select count(*) from public.operations where operation_id = 'zz-bad-new'")[0][0] == 0

    run(upsert(dbs.after, "zz-bad-old", status="pending", result='{"keep": true}'))
    assert run(state_of(upsert(dbs.after, "zz-bad-old", status="complete", result='{"truncated": '))) == "22P02"
    row = q(dbs.after, "select status, result_json::text from public.operations where operation_id = 'zz-bad-old'")[0]
    assert tuple(row) == ("pending", '{"keep": true}')


@pytest.mark.parametrize("op", [None, "", "   "])
def test_after_an_empty_operation_id_is_refused(dbs, op):
    assert run(state_of(upsert(dbs.after, op, result='{"ok": true}'))) == "22023"


def test_after_arabic_and_nested_json_round_trip_as_jsonb(dbs):
    doc = {"name": "شركة الرفيق التقني", "n": 12345678901234567890, "f": 1.5,
           "nested": {"a": [1, 2, {"b": None}], "t": True}, "city": "مسقط"}
    out = run(upsert(dbs.after, "zz-json-1", result=json.dumps(doc, ensure_ascii=False)))
    assert out["result_json"] == doc
    assert out["result_json"] == json.loads(json.dumps(doc))
    stored = q(dbs.after, "select jsonb_typeof(result_json) from public.operations where operation_id = 'zz-json-1'")
    assert stored[0][0] == "object"


def test_after_the_boundary_is_unchanged_and_authenticated_is_gone(dbs):
    rows = q(dbs.after, "select p.prosecdef, r.rolname, r.rolbypassrls, p.proacl::text, p.proconfig::text, "
                        "pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid) from pg_proc p "
                        "join pg_roles r on r.oid = p.proowner where p.proname = 'operations_upsert'")
    assert len(rows) == 1                                   # one function, not an overload beside the old one
    definer, owner, bypass, proacl, config, args, result = rows[0]
    assert (definer, owner, bypass) == (True, "spine_owner", True)
    assert set(proacl.strip("{}").split(",")) == ACL_AFTER
    assert config == "{search_path=public}"
    assert args == SIGNATURE and result == "jsonb"

    # authenticated lost EXECUTE on purpose; anon and service_role keep it
    assert run(state_of(upsert(dbs.after, "zz-auth-1", result='{}', role="authenticated"))) == "42501"
    assert run(upsert(dbs.after, "zz-svc-1", result='{}', role="service_role"))["operation_id"] == "zz-svc-1"

    # no direct door to the table for anon
    async def direct():
        c = await asyncpg.connect(dbs.after)
        try:
            await c.execute("set role anon")
            return (await state_of(c.fetch("select * from public.operations")),
                    await state_of(c.execute("insert into public.operations (operation_id, tool, status) "
                                             "values ('zz-direct', 't', 's')")))
        finally:
            await c.close()
    assert run(direct()) == ("42501", "42501")

    # the two readers were not touched by 012
    for fn in ("operations_get_by_id", "operations_get_by_appointment_id"):
        sql = f"select p.proacl::text, pg_get_functiondef(p.oid) from pg_proc p where p.proname = '{fn}'"
        assert q(dbs.after, sql) == q(dbs.before, sql)


def test_after_applying_the_migration_again_changes_nothing(dbs):
    sql_acl = "select p.proacl::text from pg_proc p where p.proname = 'operations_upsert'"
    sql_def = "select pg_get_functiondef(p.oid) from pg_proc p where p.proname = 'operations_upsert'"
    before_acl, before_def = acl(q(dbs.after, sql_acl)), q(dbs.after, sql_def)[0][0]

    async def reapply():
        c = await asyncpg.connect(dbs.after)
        try:
            await c.execute("set role spine_owner")
            await c.execute(MIGRATION.read_text(encoding="utf-8"))
        finally:
            await c.close()
    run(reapply())
    assert acl(q(dbs.after, sql_acl)) == before_acl == ACL_AFTER
    assert q(dbs.after, sql_def)[0][0] == before_def
    assert q(dbs.after, "select count(*) from information_schema.columns where table_name = 'operations' "
                        "and column_name = 'updated_at'")[0][0] == 1


def test_after_a_rebuilt_database_ends_in_the_same_state(dbs):
    """If the spine is ever rebuilt (`resync`), the function does not exist yet and 012 must CREATE it with the
    right grants. Postgres gives EXECUTE to PUBLIC on a new function, so only the migration's own revoke keeps it
    from the world. Done inside a transaction that is rolled back, so the shared database is left as it was."""
    async def go():
        c = await asyncpg.connect(dbs.after)
        try:
            tx = c.transaction()
            await tx.start()
            try:
                await c.execute("drop function public.operations_upsert(text, text, text, text, text, text, text)")
                gone = await c.fetchval("select count(*) from pg_proc where proname = 'operations_upsert'")
                await c.execute("set local role spine_owner")
                await c.execute(MIGRATION.read_text(encoding="utf-8"))
                rows = await c.fetch("select p.proacl::text, r.rolname, p.prosecdef from pg_proc p "
                                     "join pg_roles r on r.oid = p.proowner where p.proname = 'operations_upsert'")
                return gone, [tuple(r) for r in rows]
            finally:
                await tx.rollback()
        finally:
            await c.close()
    gone, rows = run(go())
    assert gone == 0
    assert len(rows) == 1
    proacl, owner, definer = rows[0]
    assert set(proacl.strip("{}").split(",")) == ACL_AFTER      # nothing for PUBLIC (an '=X/...' entry) or the others
    assert (owner, definer) == ("spine_owner", True)
    assert q(dbs.after, "select count(*) from pg_proc where proname = 'operations_upsert'")[0][0] == 1


def test_after_ten_simultaneous_writers_of_one_id_leave_one_row(dbs):
    async def go():
        return await asyncio.gather(*[
            upsert(dbs.after, "zz-race-1", status=f"s{i}", result=json.dumps({"writer": i}), agent="agent-race")
            for i in range(10)])
    out = run(go())
    assert all(r["operation_id"] == "zz-race-1" for r in out)
    rows = q(dbs.after, "select status, result_json->>'writer' from public.operations where operation_id = 'zz-race-1'")
    assert len(rows) == 1
    assert rows[0][0] == f"s{rows[0][1]}"                     # one writer's status AND its own result, never mixed


def test_after_the_production_writer_is_read_back_by_a_fresh_process(dbs, monkeypatch):
    """The point of the whole migration: the service's own write survives its process.

    A second OutcomeStore has no memory of the first, which is what another worker or a restart looks like.
    The appointment lookup is the cancellation-ownership check: with the write failing, it silently failed closed.
    """
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))

    async def go():
        writer = store_mod.OutcomeStore()
        wrote = await writer.set_complete_durable(
            "op-after-1", dict(OUTCOME), tool="schedule_appointment", agent_id="agent-owner")
        await _drain()
        reader = store_mod.OutcomeStore()                     # a different "process": nothing in memory
        return (wrote, await reader.get_async("op-after-1"),
                await reader.get_appointment_owner_async("appt-prod-1"),
                await reader.get_appointment_owner_async("appt-not-booked"))
    wrote, envelope, owner, stranger = run(go())
    assert wrote is True
    assert envelope is not None
    assert envelope["operation_id"] == "op-after-1" and envelope["status"] == "success"
    assert envelope["agent_id"] == "agent-owner"
    assert envelope["outcome"] == {k: v for k, v in OUTCOME.items() if k != "quota"}   # quota never persisted
    assert owner == "agent-owner"
    assert stranger is None
    row = q(dbs.after, "select tool, status, reason_code, appointment_id, agent_id, updated_at is not null "
                       "from public.operations where operation_id = 'op-after-1'")
    assert [tuple(r) for r in row] == [("schedule_appointment", "success", "appointment_confirmed",
                                        "appt-prod-1", "agent-owner", True)]


def test_after_the_whole_run_left_no_function_overload_and_no_extra_table(dbs):
    """012 is one function and one column: nothing else appeared in the schema."""
    assert q(dbs.after, "select count(*) from pg_proc where proname = 'operations_upsert'")[0][0] == 1
    tables = {r[0] for r in q(dbs.after, "select table_name from information_schema.tables "
                                         "where table_schema = 'public'")}
    assert tables == {r[0] for r in q(dbs.before, "select table_name from information_schema.tables "
                                                  "where table_schema = 'public'")}
