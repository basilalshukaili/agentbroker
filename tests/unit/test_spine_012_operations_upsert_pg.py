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
  ORDER   (added after review) a write that arrives late never undoes one that is already final: an in-progress
          write (pending, executing, pending_async) cannot replace a final row, whatever order the two HTTP calls
          land in - proven with a FORCED interleaving (one writer held uncommitted while the other waits behind
          it), a 30-pair race, and the production store with a delayed first write; the skipped update still
          answers with the row that stands, never NULL; agent_id and appointment_id are kept when a later write
          carries NULL (set_executing sends no owner); final over final and in-progress over in-progress remain a
          full overwrite.
  OWNER   (added after the second review) a row that has an owner is written only by that owner: another agent's
          write is refused with 42501 whatever its status, the refusal names nobody, and a fresh operation id (or an
          unowned write) cannot claim an appointment id another agent recorded - proven through the production
          store the way the cancellation lookup reads it, and with a FORCED race between two claimants. The
          limit it does not remove (a holder of the anon key) is stated in the migration header.
  APPLY   the apply gives up in 3 s with 55P03 when another transaction holds the table, leaves nothing half-done,
          and the same file then applies; legacy-shaped rows (the receipt held as a jsonb STRING, as 60 of the 61
          live rows do) still read back through the unchanged readers.
  VALUES  a NUL character, a lone surrogate, NaN or Infinity in a receipt - and a result that cannot be serialised
          at all - no longer cost the whole durable record: the writer cleans them, or keeps the row without the
          result, so status, reason, appointment and owner still land.

Opt-in with OAUTH_PG_TESTS=1 (the one switch for every database-backed test here; the name is historical).
Skipped, with the reason, when docker, the image or asyncpg is missing - and CI does not set the switch, so the
ship step and Node B must run this file with it and require ZERO skipped (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
import json
import re
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
-- The live spine has this default ACL for spine_owner (read from pg_default_acl): a function spine_owner creates
-- is born executable by anon, authenticated and service_role (and PUBLIC, which is Postgres's own default). The
-- fixture used to set default privileges only for the superuser, so a FRESH function started from a different ACL
-- than the live one would - and the "revoke from public, anon, authenticated, service_role" in the migration
-- was being tested from a friendlier start than production has.
alter default privileges for role spine_owner in schema public
    grant execute on functions to anon, authenticated, service_role;
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

-- The 61 rows the live table holds were written by the older direct-table path, which stored the receipt as a
-- JSON STRING: measured read-only on 2026-10-04, 60 of 61 have jsonb_typeof(result_json) = 'string' (a jsonb
-- scalar that CONTAINS the serialised receipt) and 1 is NULL; none is an object. The fixture used objects, so no
-- test read a real legacy-shaped row back through the unchanged readers.
insert into public.operations (operation_id, ts, tool, status, reason_code, result_json, agent_id, appointment_id)
values
 ('legacy-1', '2026-08-23 13:20:29+00', 'schedule_appointment', 'complete', 'appointment_confirmed',
  to_jsonb('{"status": "success", "result": {"appointment_id": "appt-legacy"}}'::text), 'agent-old', 'appt-legacy'),
 ('legacy-2', '2026-09-01 21:19:41+00', 'send_message', 'complete', null,
  to_jsonb('{"status": "success"}'::text), null, null),
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
        for name in ("before_db", "after_db", "lock_db", "rehearsal_db"):
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
    return SimpleNamespace(before=_db(dsn, "before_db"), after=_db(dsn, "after_db"), lock=_db(dsn, "lock_db"),
                           rehearsal=_db(dsn, "rehearsal_db"))


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


def test_after_a_legacy_shaped_row_still_reads_back_through_the_unchanged_readers(dbs, monkeypatch):
    """The rows that exist today hold the receipt as a jsonb STRING (see LEGACY_TABLE_SQL). 012 changes neither
    reader, so the production read path - which parses a string result_json back into the receipt - must still
    answer for them, by operation id and by the provider's appointment id."""
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))
    assert q(dbs.after, "select jsonb_typeof(result_json) from public.operations "
                        "where operation_id = 'legacy-1'")[0][0] == "string"

    async def go():
        reader = store_mod.OutcomeStore()
        return (await reader.get_async("legacy-1"), await reader.get_async("legacy-3"),
                await reader.get_appointment_owner_async("appt-legacy"))
    one, three, owner = run(go())
    assert one["status"] == "complete" and one["agent_id"] == "agent-old"
    assert one["outcome"] == {"status": "success", "result": {"appointment_id": "appt-legacy"}}
    assert three["status"] == "pending" and three["outcome"] == {}
    assert owner == "agent-old"


def test_after_an_update_overwrites_tool_ts_and_a_missing_owner(dbs):
    """What a second write replaces, column by column. The first version of the insert/update test used the same
    agent and tool on both calls and never read `ts`, so deleting `agent_id = ...`, `tool = ...` or `ts = ...`
    from the DO UPDATE list left every test green (found by a mutation sweep)."""
    first = run(upsert(dbs.after, "zz-cols-1", tool="tool_one", status="pending", agent=None))
    assert first["agent_id"] is None and first["tool"] == "tool_one"
    second = run(upsert(dbs.after, "zz-cols-1", tool="tool_two", status="success", reason="r2", agent="agent-a"))
    row = q(dbs.after, ROW_SQL, "zz-cols-1")
    assert len(row) == 1
    _, ts, tool, status, reason, _appt, _result, agent, updated_at = row[0]
    assert (tool, status, reason, agent) == ("tool_two", "success", "r2", "agent-a")   # an owner arrives with a later write
    assert second["tool"] == "tool_two" and second["agent_id"] == "agent-a"
    assert ts > datetime.fromisoformat(first["ts"]) and updated_at > datetime.fromisoformat(first["updated_at"])


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


def test_after_recreating_the_function_on_the_live_shaped_table_ends_in_the_same_state(dbs):
    """The function is dropped and 012 has to CREATE it, with the right grants. Postgres gives EXECUTE to PUBLIC on
    a new function, and spine_owner's default ACL (see ROLE_SQL) adds anon, authenticated and service_role, so only
    the migration's own revoke keeps it from the world.

    What this is NOT: a rebuild of the whole database. The repo's own migrations/operations_table.sql would make
    `result_json` TEXT and `updated_at` nullable - a table the spine never had - and 012's `add column if not
    exists` would leave that as it is. The spine is rebuilt from a copy of the live schema, which is what the
    fixture is. Done inside a transaction that is rolled back, so the shared database is left as it was."""
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


# =================================================== AFTER: arrival order ========================================
# The writers are independent tasks (`OutcomeStore._fire_persist`), each one an HTTP call, and two calls for one
# operation can pass each other. The first version of this function let whichever write ARRIVED last win outright:
# a late "pending" - every other column NULL - wiped the reason, the appointment, the result and the owner of a
# booking that was already confirmed (and 4 of 30 concurrent pending/final pairs ended the day stuck at 'pending').
# Applying 012 as it stood would have been an improvement on recording nothing, but it would have made the
# cancellation-ownership lookup fail closed at random on real bookings. Fixed in the function, before it is applied.
IN_PROGRESS = ("pending", "executing", "pending_async")
FINAL_RESULT = json.dumps({"status": "success", "reason_code": "appointment_confirmed",
                           "result": {"appointment_id": "appt-final"}})
ROW_SQL = ("select operation_id, ts, tool, status, reason_code, appointment_id, result_json::text, agent_id, "
           "updated_at from public.operations where operation_id = $1")
UPSERT_SQL = "select public.operations_upsert($1, $2, $3, $4, $5, $6, $7)"


@pytest.mark.parametrize("late", IN_PROGRESS)
def test_after_a_late_in_progress_write_never_replaces_a_final_outcome(dbs, late):
    key = f"zz-late-{late}"
    run(upsert(dbs.after, key, tool="schedule_appointment", status="success", reason="appointment_confirmed",
               appt="appt-final", result=FINAL_RESULT, agent="agent-owner"))
    settled = q(dbs.after, ROW_SQL, key)
    answer = run(upsert(dbs.after, key, tool="schedule_appointment", status=late, reason=None, appt=None,
                        result=None, agent=None))
    assert q(dbs.after, ROW_SQL, key) == settled              # not one column moved, updated_at included
    # the writer is handed the row that stands. A NULL here is what the service logs as "persist_failed" and
    # reports to nobody, so a skipped update must not look like a failed write.
    assert answer is not None
    assert answer["status"] == "success" and answer["reason_code"] == "appointment_confirmed"
    assert answer["agent_id"] == "agent-owner" and answer["appointment_id"] == "appt-final"
    assert answer["result_json"]["result"]["appointment_id"] == "appt-final"


def test_after_in_progress_states_still_replace_each_other_and_a_final_outcome_still_replaces_them(dbs):
    key = "zz-forward-1"
    tool = "schedule_appointment"
    run(upsert(dbs.after, key, tool=tool, status="pending", agent="agent-owner"))
    r = run(upsert(dbs.after, key, tool=tool, status="pending_async", result='{"status": "pending_async"}',
                   agent="agent-owner"))
    assert r["status"] == "pending_async" and r["result_json"] == {"status": "pending_async"}
    r = run(upsert(dbs.after, key, tool=tool, status="executing", agent=None))
    assert r["status"] == "executing" and r["agent_id"] == "agent-owner"
    # an executing row is still in progress: a booking that then waits for the provider's confirmation says so
    r = run(upsert(dbs.after, key, tool=tool, status="pending_async", result='{"status": "pending_async", "again": 1}',
                   agent=None))
    assert r["status"] == "pending_async" and r["agent_id"] == "agent-owner" and r["result_json"]["again"] == 1
    r = run(upsert(dbs.after, key, tool=tool, status="success", reason="appointment_confirmed", appt="appt-final",
                   result=FINAL_RESULT, agent="agent-owner"))
    assert r["status"] == "success" and r["appointment_id"] == "appt-final"
    # a final outcome is still replaced by a later final one: full overwrite, as the blank-result test pins
    r = run(upsert(dbs.after, key, tool=tool, status="failure", reason="x", appt=None, result=None, agent=None))
    assert r["status"] == "failure" and r["reason_code"] == "x" and r["result_json"] is None


def test_after_a_null_owner_or_appointment_never_clears_what_is_recorded(dbs):
    """Only the two identity columns are protected. Everything else is still written as sent."""
    key = "zz-coalesce-1"
    run(upsert(dbs.after, key, tool="schedule_appointment", status="success", reason="appointment_confirmed",
               appt="appt-keep", result=FINAL_RESULT, agent="agent-owner"))
    again = run(upsert(dbs.after, key, tool="schedule_appointment", status="success",
                       reason="appointment_confirmed", appt=None, result=FINAL_RESULT, agent=None))
    assert again["agent_id"] == "agent-owner" and again["appointment_id"] == "appt-keep"
    # a write that names no agent but carries the row's OWN appointment id is not "another operation claiming it"
    own_id = run(upsert(dbs.after, key, tool="schedule_appointment", status="success",
                        reason="appointment_confirmed", appt="appt-keep", result=FINAL_RESULT, agent=None))
    assert own_id["agent_id"] == "agent-owner" and own_id["appointment_id"] == "appt-keep"
    later =run(upsert(dbs.after, key, tool="schedule_appointment", status="failure", reason=None, appt=None,
                       result=None, agent=None))
    assert later["agent_id"] == "agent-owner" and later["appointment_id"] == "appt-keep"
    assert later["status"] == "failure" and later["reason_code"] is None and later["result_json"] is None
    # a value that IS sent replaces what it replaces: the owner's own write may move its appointment id ...
    moved = run(upsert(dbs.after, key, tool="schedule_appointment", status="failure", agent="agent-owner",
                       appt="appt-two"))
    assert moved["agent_id"] == "agent-owner" and moved["appointment_id"] == "appt-two"
    # ... but a DIFFERENT agent's write is refused outright (this used to be accepted, and agent-two became the
    # owner of somebody else's row - see "who may write which row" below)
    settled = q(dbs.after, ROW_SQL, key)
    assert run(state_of(upsert(dbs.after, key, tool="schedule_appointment", status="failure", agent="agent-two",
                               appt="appt-two"))) == "42501"
    assert q(dbs.after, ROW_SQL, key) == settled
    # and a first write with no owner at all is simply unowned (NULL), not an error
    assert run(upsert(dbs.after, "zz-coalesce-2", status="success", agent=None))["agent_id"] is None


async def _race(dsn, key, first, second):
    """Two sessions race for one id and the interleaving is FORCED, not hoped for: `first` is written but not yet
    committed, `second` is issued and has to wait behind it, then `first` commits. `second` therefore runs against
    the row `first` committed - the case a plain read-then-write version of this function gets wrong."""
    c1 = await asyncpg.connect(dsn)
    c2 = await asyncpg.connect(dsn)
    probe = await asyncpg.connect(dsn)
    try:
        await c1.execute("set role anon")
        await c2.execute("set role anon")
        pid = await c2.fetchval("select pg_backend_pid()")
        tx = c1.transaction()
        await tx.start()
        await c1.fetchval(UPSERT_SQL, key, *first)
        blocked = asyncio.ensure_future(c2.fetchval(UPSERT_SQL, key, *second))
        for _ in range(200):
            if await probe.fetchval("select wait_event_type from pg_stat_activity where pid = $1", pid) == "Lock":
                break
            await asyncio.sleep(0.05)
        else:
            blocked.cancel()
            raise AssertionError("the second writer never waited behind the first")
        await tx.commit()
        row = await asyncio.wait_for(blocked, 30)
        return json.loads(row) if isinstance(row, str) else row
    finally:
        for c in (c1, c2, probe):
            await c.close()


def _args(status, reason=None, appt=None, result=None, agent="agent-owner"):
    return ("schedule_appointment", status, reason, appt, result, agent)


@pytest.mark.parametrize("n, first_status, second_status", [
    (0, "success", "pending"),
    (1, "success", "pending_async"),
    (2, "pending", "success"),
])
def test_after_a_write_that_waited_behind_another_is_judged_against_the_committed_row(dbs, n, first_status,
                                                                                      second_status):
    key = f"zz-blocked-{n}"
    final = _args("success", "appointment_confirmed", f"appt-blocked-{n}", FINAL_RESULT)
    first = final if first_status == "success" else _args(first_status)
    second = final if second_status == "success" else _args(second_status, agent=None)
    answer = run(_race(dbs.after, key, first, second))
    row = q(dbs.after, "select status, reason_code, appointment_id, agent_id from public.operations "
                       "where operation_id = $1", key)
    assert [tuple(r) for r in row] == [("success", "appointment_confirmed", f"appt-blocked-{n}", "agent-owner")]
    assert answer is not None and answer["status"] == "success"      # the waiting writer is told what stands


def test_after_thirty_concurrent_pending_and_final_pairs_never_end_pending(dbs):
    """The measurement that found the defect: 4 of 30 ended 'pending' with the reason and appointment wiped."""
    async def one(i):
        await asyncio.gather(
            upsert(dbs.after, f"zz-soak-{i}", tool="schedule_appointment", status="pending", agent="agent-owner"),
            upsert(dbs.after, f"zz-soak-{i}", tool="schedule_appointment", status="success",
                   reason="appointment_confirmed", appt=f"appt-soak-{i}", result=FINAL_RESULT, agent="agent-owner"))

    async def go():
        await asyncio.gather(*[one(i) for i in range(30)])
    run(go())
    assert q(dbs.after, "select count(*) from public.operations where operation_id like 'zz-soak-%'")[0][0] == 30
    assert q(dbs.after, "select count(*) from public.operations where operation_id like 'zz-soak-%' "
                        "and status = 'success' and reason_code = 'appointment_confirmed' "
                        "and appointment_id = 'appt-soak-' || substr(operation_id, 9) "
                        "and agent_id = 'agent-owner' and result_json is not null")[0][0] == 30


def test_after_a_delayed_first_write_from_the_production_store_cannot_undo_the_final_one(dbs, monkeypatch):
    """core/schedule_appointment.py, the failed-enqueue branch: set_pending, then at once the terminal receipt.
    Two independent writes; here the FIRST one's HTTP call is held until the SECOND has been acknowledged by the
    database, so it lands last - by construction, not by a guessed delay. It has to arrive labelled as what it was
    when it was made ('pending'), not as whatever the shared record says by then ('failure' with every other column
    empty), or no ordering rule in the database can tell it from a real final write."""
    import storage.outcome_store as store_mod
    real = oauth_pg.PgRpc(dbs.after)
    sent = []
    acknowledged = []          # the final write's answer, once the database has it

    async def first_write_lands_last(fn, payload):
        if fn != "operations_upsert":
            return await real(fn, payload)
        sent.append(payload["p_status"])
        if len(sent) == 1:
            for _ in range(400):
                if acknowledged:
                    break
                await asyncio.sleep(0.05)
            assert acknowledged, "the final write was never acknowledged"
            return await real(fn, payload)
        answer = await real(fn, payload)
        acknowledged.append(answer)
        return answer
    monkeypatch.setattr("storage.supabase_client.rpc", first_write_lands_last)
    receipt = {"status": "failure", "reason_code": "async_channel_not_provisioned", "retriable": True,
               "human_message": "Nothing was booked and nothing was charged."}

    async def go():
        store = store_mod.OutcomeStore()
        store.set_pending("op-late-1", "schedule_appointment", agent_id="agent-owner")
        store.set_complete("op-late-1", dict(receipt), agent_id="agent-owner")
        await _drain()
        return await store_mod.OutcomeStore().get_async("op-late-1")       # a fresh process: nothing in memory
    found = run(go())
    assert sent == ["pending", "failure"]
    assert acknowledged and acknowledged[0]["status"] == "failure"
    assert found is not None and found["status"] == "failure" and found["agent_id"] == "agent-owner"
    assert found["outcome"]["reason_code"] == "async_channel_not_provisioned"
    assert found["outcome"]["human_message"] == receipt["human_message"]
    row = q(dbs.after, "select status, reason_code, agent_id, result_json is not null from public.operations "
                       "where operation_id = 'op-late-1'")
    assert [tuple(r) for r in row] == [("failure", "async_channel_not_provisioned", "agent-owner", True)]


def test_after_the_executing_write_keeps_the_owner_the_pending_write_recorded(dbs, monkeypatch):
    """OutcomeStore.set_executing sends no agent_id, so it used to clear the owner durably; a fresh store then
    answered operation_owner_unknown even to the agent that owns the operation."""
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))

    async def go():
        store = store_mod.OutcomeStore()
        store.set_pending("op-exec-1", "schedule_appointment", agent_id="agent-owner")
        await _drain()
        store.set_executing("op-exec-1")
        await _drain()
        return await store_mod.OutcomeStore().get_async("op-exec-1")
    found = run(go())
    assert found is not None and found["status"] == "executing" and found["agent_id"] == "agent-owner"
    assert q(dbs.after, "select tool, status, agent_id from public.operations where operation_id = 'op-exec-1'") \
        == [("schedule_appointment", "executing", "agent-owner")]


# =================================================== AFTER: who may write which row ==============================
# The anon role reaches this function, and the function takes the owner, the appointment id and the status from its
# seven arguments. Reproduced on the first committed version (10f889d): ONE call as anon with the id of a row owned
# by agent-victim and the identity agent-attacker, and get_appointment_owner_async('appt-victim') answered
# 'agent-attacker' - which core/ownership.read_denial accepts for a CANCELLATION. A fresh operation id carrying the
# victim's appointment id and reason 'appointment_confirmed' did the same (the reader is `limit 1`, unordered).
#
# THE RULE: a row that has an owner is written only by that owner (or by a write that names none - the service's own
# set_executing sends none), and an appointment id belongs to whoever first recorded it - an agent, or nobody.
#
# WHAT IT DOES NOT DEFEND, stated here and in the migration header because it is the honest limit: whoever holds the
# anon key can still write any FRESH operation id under any agent id they like, can write under the victim's own
# agent id if they know it, and can change an owned row's status and result with a write that names no agent if they
# know its operation id (an unguessable uuid4). Closing that is a separate server-side writer credential for this one
# function with anon's EXECUTE revoked and the container's configuration changed in the same release - a follow-up,
# not this file.
ATTACKER_RESULT = json.dumps({"status": "failure", "reason_code": "x", "human_message": "overwritten"})


def _book(dsn, key, appt, agent="agent-victim"):
    run(upsert(dsn, key, tool="schedule_appointment", status="success", reason="appointment_confirmed", appt=appt,
               result=FINAL_RESULT, agent=agent))
    return q(dsn, ROW_SQL, key)


@pytest.mark.parametrize("status", ["success", "failure", "pending", "executing", "pending_async"])
def test_after_another_agent_cannot_overwrite_an_operation_that_has_an_owner(dbs, status):
    """Whatever status the foreign write carries - including an in-progress one, which the arrival rule alone would
    have skipped silently - it is REFUSED (42501), so the caller is told, and not one column moves."""
    key, appt = f"zz-owned-{status}", f"appt-owned-{status}"
    settled = _book(dbs.after, key, appt)
    assert run(state_of(upsert(dbs.after, key, tool="schedule_appointment", status=status,
                               reason="appointment_confirmed", appt=appt, result=ATTACKER_RESULT,
                               agent="agent-attacker"))) == "42501"
    assert q(dbs.after, ROW_SQL, key) == settled


def test_after_the_refusal_does_not_name_the_owner(dbs):
    """The error text reaches whoever made the call; it must not tell them who the owner is."""
    key = "zz-owned-quiet"
    _book(dbs.after, key, "appt-owned-quiet", agent="agent-victim-secret")

    async def go():
        c = await asyncpg.connect(dbs.after)
        try:
            await c.execute("set role anon")
            await c.fetchval(UPSERT_SQL, key, "t", "success", None, None, None, "agent-attacker")
        except asyncpg.PostgresError as exc:
            return exc.sqlstate, str(exc)
        finally:
            await c.close()
    sqlstate, text = run(go())
    assert sqlstate == "42501"
    assert "agent-victim-secret" not in text and "agent-attacker" not in text


def test_after_the_takeover_that_was_reproduced_does_not_work_through_the_production_store(dbs, monkeypatch):
    """The exact attack, driven through the production writer and read back by a fresh process the way a
    cancellation reads it: same operation id with another identity, then a fresh operation id for the victim's
    appointment. Both writes fail, and the owner lookup still names the victim."""
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))
    receipt = {"status": "success", "reason_code": "appointment_confirmed",
               "result": {"appointment_id": "appt-victim-prod"}}

    async def go():
        won = await store_mod.OutcomeStore().set_complete_durable(
            "op-victim-prod", dict(receipt), tool="schedule_appointment", agent_id="agent-victim")
        attacker = store_mod.OutcomeStore()
        same_id = await attacker.set_complete_durable(
            "op-victim-prod", dict(receipt), tool="schedule_appointment", agent_id="agent-attacker")
        fresh_id = await attacker.set_complete_durable(
            "op-attacker-fresh", dict(receipt), tool="schedule_appointment", agent_id="agent-attacker")
        await _drain()
        return won, same_id, fresh_id, await store_mod.OutcomeStore().get_appointment_owner_async("appt-victim-prod")
    won, same_id, fresh_id, owner = run(go())
    assert won is True
    assert same_id is False and fresh_id is False
    assert owner == "agent-victim"
    assert q(dbs.after, "select count(*) from public.operations where operation_id = 'op-attacker-fresh'")[0][0] == 0


@pytest.mark.parametrize("who", ["agent-attacker", None])
def test_after_a_fresh_operation_cannot_claim_an_appointment_another_agent_booked(dbs, who):
    """A row with NO owner is refused too: the reader is `limit 1` with no ordering, so an unowned row carrying the
    victim's appointment id could be the one it returns - and an unowned owner fails every cancellation closed."""
    appt = f"appt-claim-{who}"
    _book(dbs.after, f"zz-claim-booked-{who}", appt)
    fresh = f"zz-claim-fresh-{who}"
    assert run(state_of(upsert(dbs.after, fresh, tool="schedule_appointment", status="success",
                               reason="appointment_confirmed", appt=appt, result=ATTACKER_RESULT, agent=who))) == "42501"
    assert q(dbs.after, "select count(*) from public.operations where operation_id = $1", fresh)[0][0] == 0
    # the lookup a cancellation is authorised by still has exactly one candidate: the booking's own row
    assert q(dbs.after, "select count(*), min(agent_id) from public.operations where appointment_id = $1 "
                        "and reason_code = 'appointment_confirmed'", appt) == [(1, "agent-victim")]


def test_after_an_unowned_booking_is_not_up_for_grabs(dbs):
    """'A different owner' includes NO owner, in both directions. An appointment id recorded with no owner cannot be
    taken by an agent either: nobody may cancel an unowned booking today (core/ownership.read_denial fails closed),
    and a claim would turn that into 'this agent may'."""
    appt = "appt-unowned-1"
    _book(dbs.after, "zz-unowned-booked", appt, agent=None)
    assert run(state_of(upsert(dbs.after, "zz-unowned-claim", tool="schedule_appointment", status="success",
                               reason="appointment_confirmed", appt=appt, result=ATTACKER_RESULT,
                               agent="agent-attacker"))) == "42501"
    assert q(dbs.after, "select count(*) from public.operations where appointment_id = $1", appt) == [(1,)]
    # a second row that names nobody takes nothing from anybody
    again = run(upsert(dbs.after, "zz-unowned-again", tool="schedule_appointment", status="success",
                       reason="cancelled", appt=appt, result='{"status": "success"}', agent=None))
    assert again["agent_id"] is None and again["appointment_id"] == appt


def test_after_an_agent_cannot_move_its_own_row_onto_an_appointment_another_agent_booked(dbs):
    _book(dbs.after, "zz-move-victim", "appt-move-victim")
    run(upsert(dbs.after, "zz-move-own", tool="schedule_appointment", status="pending", agent="agent-attacker"))
    settled = q(dbs.after, ROW_SQL, "zz-move-own")
    assert run(state_of(upsert(dbs.after, "zz-move-own", tool="schedule_appointment", status="success",
                               reason="appointment_confirmed", appt="appt-move-victim", result=ATTACKER_RESULT,
                               agent="agent-attacker"))) == "42501"
    assert q(dbs.after, ROW_SQL, "zz-move-own") == settled


def test_after_the_owner_and_new_claimants_still_work(dbs):
    """The rule must not get in the way of the flows that are legitimate."""
    appt = "appt-legit-1"
    _book(dbs.after, "zz-legit-booked", appt, agent="agent-a")
    # the same agent records a second operation on its own appointment (a cancellation has its own operation id)
    again = run(upsert(dbs.after, "zz-legit-cancel", tool="schedule_appointment", status="success",
                       reason="cancelled", appt=appt, result='{"status": "success"}', agent="agent-a"))
    assert again["agent_id"] == "agent-a" and again["appointment_id"] == appt
    # an appointment id nobody has recorded is anybody's to record, and two agents can use one tool
    assert run(upsert(dbs.after, "zz-legit-free", appt="appt-legit-free", agent="agent-b"))["agent_id"] == "agent-b"
    # an operation nobody owns yet takes the first owner it is given, and keeps it
    run(upsert(dbs.after, "zz-adopt-1", status="pending", agent=None))
    assert run(upsert(dbs.after, "zz-adopt-1", status="success", agent="agent-first"))["agent_id"] == "agent-first"
    assert run(state_of(upsert(dbs.after, "zz-adopt-1", status="success", agent="agent-second"))) == "42501"
    assert q(dbs.after, "select agent_id from public.operations where operation_id = 'zz-adopt-1'") == [("agent-first",)]
    # an unowned row stays writable by anyone, exactly as before - the rule protects owners, it does not invent them
    run(upsert(dbs.after, "zz-nobody-1", status="pending", agent=None))
    assert run(upsert(dbs.after, "zz-nobody-1", status="success", agent=None))["status"] == "success"


def test_after_a_legacy_row_with_an_owner_is_protected_too(dbs):
    settled = q(dbs.after, LEGACY_ROWS)
    assert run(state_of(upsert(dbs.after, "legacy-1", tool="schedule_appointment", status="success",
                               agent="agent-attacker"))) == "42501"
    assert run(state_of(upsert(dbs.after, "zz-legacy-claim", tool="schedule_appointment", status="success",
                               reason="appointment_confirmed", appt="appt-legacy", agent="agent-attacker"))) == "42501"
    assert q(dbs.after, LEGACY_ROWS) == settled


async def _race_claims(dsn, appt):
    """Two DIFFERENT agents claim one free appointment id, and the interleaving is forced: the first has written but
    not committed, the second is issued and has to wait behind it, then the first commits. Without a lock keyed on
    the appointment id the second never waits - it cannot see the first's uncommitted row - passes the check, and
    both claims stand."""
    c1 = await asyncpg.connect(dsn)
    c2 = await asyncpg.connect(dsn)
    probe = await asyncpg.connect(dsn)
    try:
        await c1.execute("set role anon")
        await c2.execute("set role anon")
        pid = await c2.fetchval("select pg_backend_pid()")
        tx = c1.transaction()
        await tx.start()
        await c1.fetchval(UPSERT_SQL, "zz-claimrace-1", "schedule_appointment", "success", "appointment_confirmed",
                          appt, FINAL_RESULT, "agent-first")
        second = asyncio.ensure_future(c2.fetchval(UPSERT_SQL, "zz-claimrace-2", "schedule_appointment", "success",
                                                   "appointment_confirmed", appt, ATTACKER_RESULT, "agent-second"))
        for _ in range(200):
            if second.done():
                raise AssertionError("the second claim did not wait behind the first: two agents can claim one "
                                     "appointment id at the same moment")
            if await probe.fetchval("select wait_event_type from pg_stat_activity where pid = $1", pid) == "Lock":
                break
            await asyncio.sleep(0.05)
        else:
            second.cancel()
            raise AssertionError("the second claim never waited behind the first")
        await tx.commit()
        try:
            await asyncio.wait_for(second, 30)
        except asyncpg.PostgresError as exc:
            return exc.sqlstate
        return None
    finally:
        for c in (c1, c2, probe):
            await c.close()


def test_after_two_agents_claiming_one_appointment_at_once_leave_exactly_one_owner(dbs):
    state = run(_race_claims(dbs.after, "appt-claimrace"))
    assert state == "42501"
    assert q(dbs.after, "select operation_id, agent_id from public.operations where appointment_id = 'appt-claimrace'") \
        == [("zz-claimrace-1", "agent-first")]


def test_after_a_foreign_write_that_waited_behind_the_owners_is_refused(dbs):
    """The owner rule has to hold when the foreign write arrives while the owner's write is still uncommitted: the
    check is part of the ON CONFLICT condition, so it is judged against the row the owner COMMITTED - not against
    what the foreign write could see when it started (an empty table), which is what a read-then-write version of
    this rule gets wrong."""
    key = "zz-blocked-owner"
    owner = _args("success", "appointment_confirmed", "appt-blocked-owner", FINAL_RESULT, agent="agent-owner")
    foreign = _args("success", "appointment_confirmed", None, ATTACKER_RESULT, agent="agent-attacker")
    assert run(state_of(_race(dbs.after, key, owner, foreign))) == "42501"
    assert q(dbs.after, "select status, reason_code, appointment_id, agent_id, result_json->>'status' "
                        "from public.operations where operation_id = $1", key) \
        == [("success", "appointment_confirmed", "appt-blocked-owner", "agent-owner", "success")]


# =================================================== AFTER: what the apply does to a busy table ==================
def test_after_the_apply_gives_up_fast_instead_of_queueing_everything_behind_it(dbs):
    """`alter table` needs ACCESS EXCLUSIVE, and spine_owner has no lock_timeout. One open transaction that has
    merely READ the table (a pg_dump, a forgotten psql) makes the apply wait forever - and every NEW reader queues
    behind the waiting apply, so get_status / get_outcome answer 'store unavailable' until the holder ends. The file
    sets its own lock_timeout: the apply then fails fast and cleanly, nothing changes, and it can simply be re-run."""
    import time

    column_sql = ("select count(*) from information_schema.columns "
                  "where table_name = 'operations' and column_name = 'updated_at'")

    async def go():
        holder = await asyncpg.connect(dbs.lock)
        applier = await asyncpg.connect(dbs.lock)
        probe = await asyncpg.connect(dbs.lock)
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.fetchval("select count(*) from public.operations")       # ACCESS SHARE, held open
            await applier.execute("set role spine_owner")
            t0 = time.monotonic()
            try:
                # the holder only lets go AFTER the apply returns, so a file without its own lock_timeout would hang
                # here for ever: the outer bound turns that into a failure with a name instead of a stuck suite
                failed = await asyncio.wait_for(state_of(applier.execute(MIGRATION.read_text(encoding="utf-8"))), 30)
            except asyncio.TimeoutError:
                failed = "hung"
            waited = time.monotonic() - t0
            has_column = await probe.fetchval(column_sql)
            still_old = await probe.fetchval("select position('v_json' in pg_get_functiondef(p.oid)) = 0 "
                                             "from pg_proc p where p.proname = 'operations_upsert'")
            await tx.rollback()                                                   # the holder lets go
            applier.terminate()                                                   # (it may be mid-cancel after a hang)
            applier = await asyncpg.connect(dbs.lock)
            await applier.execute("set role spine_owner")
            await applier.execute(MIGRATION.read_text(encoding="utf-8"))         # and the SAME file now applies
            applied = await probe.fetchval(column_sql)
            return failed, waited, has_column, still_old, applied
        finally:
            for c in (holder, applier, probe):
                await c.close()
    failed, waited, has_column, still_old, applied = run(go())
    assert failed == "55P03"                         # lock_not_available
    assert waited < 10                               # it gave up, it did not hang until the holder ended
    assert has_column == 0 and still_old is True     # and left nothing half-applied
    assert applied == 1


# =================================================== AFTER: values jsonb refuses =================================
# A NUL character, a lone surrogate, NaN and Infinity are all things a Python dict can hold and jsonb will not
# accept. The cancellation receipt echoes a caller-supplied field, so a caller controls one of them for its OWN
# operation. Before this was handled the whole durable record - status and owner as well - was lost, not just the
# offending field. (Self-inflicted only: no cross-agent effect was found.)
class _Odd:
    """Not JSON-serialisable, so the serialiser asks `default` for it - and the text it answers with is hostile."""

    def __str__(self):
        return "o\x00d\ud800d"


HOSTILE = {          # name -> extra fields at the top of the receipt (each case gets its own appointment id)
    "object_with_hostile_text": {"thing": _Odd()},
    "nul": {"human_message": "a\x00b"},
    "nan": {"human_message": "ok", "score": float("nan")},
    "infinity": {"human_message": "ok", "score": float("-inf")},
    "lone_surrogate": {"human_message": "x\ud800y"},
    "nul_in_a_key": {"human\x00key": "ok"},
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_after_values_jsonb_refuses_do_not_cost_the_whole_record(dbs, monkeypatch, name):
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))
    op = f"op-hostile-{name}"
    appt = f"appt-hostile-{name}"      # one per case: an appointment id belongs to the first agent that records it
    outcome = {"status": "success", "reason_code": "appointment_confirmed", "result": {"appointment_id": appt},
               **HOSTILE[name]}

    async def go():
        wrote = await store_mod.OutcomeStore().set_complete_durable(
            op, outcome, tool="schedule_appointment", agent_id=f"agent-{name}")
        await _drain()
        return wrote, await store_mod.OutcomeStore().get_async(op)
    wrote, found = run(go())
    assert wrote is True
    assert found is not None and found["status"] == "success" and found["agent_id"] == f"agent-{name}"
    assert found["outcome"]["reason_code"] == "appointment_confirmed"
    assert found["outcome"]["result"]["appointment_id"] == appt                  # the rest of the receipt survives
    assert q(dbs.after, "select appointment_id from public.operations where operation_id = $1", op) == [(appt,)]


def test_after_a_result_that_cannot_be_serialised_still_records_status_and_owner(dbs, monkeypatch):
    import storage.outcome_store as store_mod
    monkeypatch.setattr("storage.supabase_client.rpc", oauth_pg.PgRpc(dbs.after))
    loop = {"status": "success", "reason_code": "appointment_confirmed"}
    loop["result"] = {"appointment_id": "appt-loop"}
    loop["result"]["self"] = loop["result"]                                  # json.dumps raises ValueError

    async def go():
        wrote = await store_mod.OutcomeStore().set_complete_durable(
            "op-loop-1", loop, tool="schedule_appointment", agent_id="agent-loop")
        await _drain()
        return wrote
    assert run(go()) is True
    assert q(dbs.after, "select status, reason_code, agent_id, result_json is null from public.operations "
                        "where operation_id = 'op-loop-1'") == [("success", "appointment_confirmed", "agent-loop", True)]


# =================================================== the rehearsal written in the header =========================
ACL_SET_QUERY = ("select (select array_agg(a order by a) from unnest(proacl::text[]) a) "
                 "= array['anon=X/spine_owner','service_role=X/spine_owner','spine_owner=X/spine_owner'] "
                 "from pg_proc where proname = 'operations_upsert'")
# (statement, expected first row) - each statement must also appear, word for word, in the migration header
REHEARSAL = [
    ("select operations_upsert('zz-verify-012', 'verify', 'success', 'appointment_confirmed', 'appt-zz', "
     "'{\"ok\":true}', 'verify') ->> 'operation_id'", ("zz-verify-012",)),
    ("select result_json->>'ok', updated_at is not null from operations where operation_id = 'zz-verify-012'",
     ("true", True)),
    ("select operations_upsert('zz-verify-012', 'verify', 'pending', null, null, null, null) ->> 'status'",
     ("success",)),
    ("select agent_id, appointment_id, reason_code from operations where operation_id = 'zz-verify-012'",
     ("verify", "appt-zz", "appointment_confirmed")),
    ("select operations_upsert('zz-verify-012', 'verify', 'failure', 'x', null, '', null) ->> 'status'",
     ("failure",)),
    ("select result_json is null, agent_id, appointment_id from operations where operation_id = 'zz-verify-012'",
     (True, "verify", "appt-zz")),
]
REFUSED = [
    "select operations_upsert('zz-verify-012', 'verify', 'success', null, null, null, 'someone-else')",
    "select operations_upsert('zz-verify-012-b', 'verify', 'success', 'appointment_confirmed', 'appt-zz', null, "
    "'someone-else')",
]


def _plain(text: str) -> str:
    return " ".join(text.split())


def test_the_rehearsal_in_the_header_works_as_documented(dbs):
    """VERIFY part 1 runs the apply inside BEGIN..ROLLBACK and checks the ACL INSIDE the transaction (after the
    rollback the live function shows its old ACL again), part 2 checks it after a committed apply. Both are run
    here against the legacy-shaped database, and every statement is required to be in the header word for word."""
    header = _plain(re.sub(r"(?m)^--", " ", MIGRATION.read_text(encoding="utf-8")))
    for statement in [ACL_SET_QUERY] + [s for s, _ in REHEARSAL] + REFUSED:
        assert _plain(statement) in header, f"the header does not contain: {statement}"
    sql = MIGRATION.read_text(encoding="utf-8")

    async def go():
        c = await asyncpg.connect(dbs.rehearsal)
        out = {}
        try:
            await c.execute("set role spine_owner")
            tx = c.transaction()
            await tx.start()
            await c.execute(sql)                                              # \i migrations/spine/012_...sql
            out["acl_inside"] = await c.fetchval(ACL_SET_QUERY)
            out["rows"] = [tuple(await c.fetchrow(s)) for s, _ in REHEARSAL]
            out["refused"] = []
            for n, statement in enumerate(REFUSED):
                await c.execute(f"savepoint s{n}")
                out["refused"].append(await state_of(c.fetchval(statement)))
                await c.execute(f"rollback to s{n}")
            await tx.rollback()
            out["acl_after_rollback"] = await c.fetchval(ACL_SET_QUERY)
            out["column_after_rollback"] = await c.fetchval(
                "select count(*) from information_schema.columns where table_name = 'operations' "
                "and column_name = 'updated_at'")
            await c.execute(sql)                                              # the real apply
            out["acl_committed"] = await c.fetchval(ACL_SET_QUERY)
            out["rows_left"] = await c.fetchval("select count(*) from public.operations where operation_id like 'zz-verify-%'")
        finally:
            await c.close()
        return out
    out = run(go())
    assert out["acl_inside"] is True
    assert out["rows"] == [expected for _, expected in REHEARSAL]
    assert out["refused"] == ["42501", "42501"]
    assert out["acl_after_rollback"] is False and out["column_after_rollback"] == 0     # nothing stayed
    assert out["acl_committed"] is True and out["rows_left"] == 0
