"""Migration spine/012 - operations_upsert must only write columns the table has, and must cast its text to jsonb.

THE BUG CLASS. A plpgsql function is compiled lazily: `create function` succeeds even when its body names a column
the table does not have, and the error (42703) only appears on the first CALL. operations_upsert shipped that way
(it writes operations.updated_at, which the live table never had) and every call has failed since, silently: the
caller logs a warning and goes on, so the durable operation record was never written.

This is a STRUCTURAL guard on the migration file - a proof about the file, not about a live database. The live
proof is the rolled-back transaction recorded in the migration header (BEGIN; apply; call; ROLLBACK).

The detector (`problems`) is exercised against BOTH the old definition, copied verbatim from the live spine on
2026-10-04, which it must reject, and migration 012, which it must accept - so the test can tell the two apart.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "migrations" / "spine" / "012_operations_upsert_fix.sql"

# Columns of public.operations on the spine, measured 2026-10-04 (information_schema.columns).
LIVE_COLUMNS = {"operation_id", "ts", "tool", "status", "reason_code", "result_json", "agent_id", "appointment_id"}

# The live definition before migration 012, verbatim from pg_get_functiondef.
OLD_DEFINITION = """
CREATE OR REPLACE FUNCTION public.operations_upsert(p_operation_id text, p_tool text, p_status text, p_reason_code text, p_appointment_id text, p_result_json text, p_agent_id text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
    v_row operations;
begin
    insert into operations (
        operation_id, ts, tool, status, reason_code, appointment_id,
        result_json, agent_id, updated_at
    )
    values (
        p_operation_id, now(), p_tool, p_status, p_reason_code,
        p_appointment_id, p_result_json, p_agent_id, now()
    )
    on conflict (operation_id) do update set
        ts             = excluded.ts,
        tool           = excluded.tool,
        status         = excluded.status,
        reason_code    = excluded.reason_code,
        appointment_id = excluded.appointment_id,
        result_json    = excluded.result_json,
        agent_id       = excluded.agent_id,
        updated_at     = excluded.updated_at
    returning * into v_row;

    return to_jsonb(v_row);
end;
$function$
"""


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def problems(sql: str, columns: set) -> list:
    """Return what is wrong with the operations_upsert definition in `sql`, given the columns the table will have."""
    code = _strip_comments(sql)
    added = {m.group(1).lower() for m in re.finditer(
        r"add\s+column\s+if\s+not\s+exists\s+(\w+)", code, re.IGNORECASE)}
    have = {c.lower() for c in columns} | added
    out = []
    ins = re.search(r"insert\s+into\s+operations\s*\((.*?)\)\s*values\s*\((.*?)\)\s*on\s+conflict", code,
                    re.IGNORECASE | re.DOTALL)
    if not ins:
        return ["no `insert into operations (...) values (...) on conflict` found"]
    cols = [c.strip().lower() for c in ins.group(1).split(",") if c.strip()]
    for c in cols:
        if c not in have:
            out.append(f"writes column {c!r} that the table does not have (42703 at the first call)")
    sets = re.search(r"do\s+update\s+set(.*?)returning", code, re.IGNORECASE | re.DOTALL)
    for m in re.finditer(r"(\w+)\s*=\s*excluded\.(\w+)", sets.group(1) if sets else ""):
        if m.group(1).lower() not in have:
            out.append(f"updates column {m.group(1)!r} that the table does not have")
    # result_json is jsonb: the text parameter must be cast before it reaches VALUES.
    values = [v.strip() for v in re.split(r",(?![^()]*\))", ins.group(2))]
    if len(values) == len(cols) and "result_json" in cols:
        v = values[cols.index("result_json")]
        if "p_result_json" in v and "jsonb" not in v.lower():
            out.append("passes the text p_result_json into the jsonb column result_json without a cast")
        elif re.fullmatch(r"\w+", v) and v.lower() != "null":
            # The value goes in through a plpgsql variable, so follow it. Judging only the VALUES entry let
            # `v_json text` + `::text` through: a mutant the real-database tests catch and this test did not.
            decl = re.search(rf"\b{re.escape(v)}\s+(\w+)\s*;", code, re.IGNORECASE)
            if not decl or decl.group(1).lower() != "jsonb":
                out.append(f"result_json is fed from the variable {v!r}, which is not declared jsonb")
            assign = re.search(rf"\b{re.escape(v)}\s*:=\s*(.*?);", code, re.IGNORECASE | re.DOTALL)
            if not assign or "::jsonb" not in assign.group(1).lower().replace(" ", ""):
                out.append(f"the variable {v!r} that feeds result_json is not assigned through a ::jsonb cast")
    return out


def test_old_definition_is_rejected():
    found = problems(OLD_DEFINITION, LIVE_COLUMNS)
    assert any("updated_at" in p for p in found), found
    assert any("without a cast" in p for p in found), found


def test_migration_012_is_accepted():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert problems(sql, LIVE_COLUMNS) == []


def test_migration_012_adds_the_column_idempotently():
    code = _strip_comments(MIGRATION.read_text(encoding="utf-8"))
    assert re.search(r"alter\s+table\s+public\.operations\s+add\s+column\s+if\s+not\s+exists\s+updated_at\s+timestamptz"
                     r"\s+not\s+null\s+default\s+now\(\)", code, re.IGNORECASE)


def test_signature_is_unchanged_so_no_caller_changes():
    """storage/outcome_store.py passes exactly these seven named parameters; PostgREST resolves by name."""
    code = _strip_comments(MIGRATION.read_text(encoding="utf-8"))
    sig = re.search(r"function\s+public\.operations_upsert\s*\((.*?)\)\s*returns\s+jsonb", code,
                    re.IGNORECASE | re.DOTALL).group(1)
    names = [p.split()[0] for p in sig.split(",")]
    assert names == ["p_operation_id", "p_tool", "p_status", "p_reason_code", "p_appointment_id", "p_result_json",
                     "p_agent_id"]
    caller = (REPO / "storage" / "outcome_store.py").read_text(encoding="utf-8")
    for n in names:
        assert f'"{n}"' in caller, f"outcome_store.py no longer sends {n}"


def test_grants_keep_the_boundary():
    """Same door as migrations 009/010: SECURITY DEFINER, search_path pinned, EXECUTE for anon + service_role only."""
    code = _strip_comments(MIGRATION.read_text(encoding="utf-8")).lower()
    assert "security definer" in code
    assert "set search_path = public" in code
    assert re.search(r"revoke all on function public\.operations_upsert\([^)]*\)\s+from public, anon, authenticated, "
                     r"service_role", code)
    assert re.search(r"grant execute on function public\.operations_upsert\([^)]*\)\s+to anon, service_role", code)
    assert "authenticated" not in code.split("grant execute", 1)[1]


def rule_problems(sql: str) -> list:
    """What is missing from the rules the function enforces. The behaviour itself is proven against a real PostgreSQL
    in test_spine_012_operations_upsert_pg.py (opt-in: OAUTH_PG_TESTS=1); this is the guard that runs everywhere."""
    code = re.sub(r"\s+", " ", _strip_comments(sql).lower())
    in_progress = "('pending', 'executing', 'pending_async')"
    out = []
    for fragment, why in [
        # arrival order
        (f"operations.status in {in_progress} or excluded.status not in {in_progress}",
         "no arrival-order rule: a late in-progress write may replace a final row"),
        ("agent_id = coalesce(excluded.agent_id, operations.agent_id)",
         "a write that carries no owner clears the owner"),
        ("appointment_id = coalesce(excluded.appointment_id, operations.appointment_id)",
         "a write that carries no appointment id clears it"),
        ("if not found then select * into v_row from operations where operation_id = p_operation_id",
         "a skipped update answers NULL, which the service logs as a failed write"),
        # who may write which row
        ("operations.agent_id is null or excluded.agent_id is null or operations.agent_id = excluded.agent_id",
         "no owner rule: any agent may overwrite a row that belongs to another"),
        ("v_row.agent_id is not null and p_agent_id is not null and v_row.agent_id <> p_agent_id",
         "a write refused for its owner is not told so (it would be skipped without an error)"),
        ("using errcode = '42501'",
         "a refused write does not raise insufficient_privilege"),
        ("o.appointment_id = p_appointment_id and o.operation_id <> p_operation_id "
         "and o.agent_id is distinct from p_agent_id",
         "an agent can claim an appointment id another agent already recorded"),
        ("pg_advisory_xact_lock(hashtextextended(",
         "two agents claiming one appointment id at the same moment are not serialised"),
        # the apply itself
        ("set local lock_timeout = '3s'",
         "the apply can queue every reader behind it: no lock_timeout"),
    ]:
        if fragment not in code:
            out.append(why)
    return out


def test_old_definition_has_none_of_the_rules():
    assert len(rule_problems(OLD_DEFINITION)) == 10


def test_migration_012_has_every_rule():
    assert rule_problems(MIGRATION.read_text(encoding="utf-8")) == []


def test_the_refusal_names_no_agent():
    """The error text goes back to whoever called the function; it must not say who the owner is."""
    code = _strip_comments(MIGRATION.read_text(encoding="utf-8"))
    messages = re.findall(r"raise exception\s+'([^']*)'(.*?);", code, re.IGNORECASE | re.DOTALL)
    refusals = [(m, args) for m, args in messages if "42501" in args]
    assert refusals, "no refusal found"
    for message, args in refusals:
        params = args.split("using")[0]          # what is substituted into the message
        assert "v_row" not in params and "p_agent_id" not in params and "operations." not in params, (message, args)


def test_lock_timeout_is_the_first_statement():
    """It must come before the `alter table`: the lock request is the first thing that can queue."""
    code = _strip_comments(MIGRATION.read_text(encoding="utf-8")).lower()
    assert code.index("set local lock_timeout") < code.index("alter table")


def test_the_cast_detector_follows_the_variable_the_value_goes_through():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert problems(sql, LIVE_COLUMNS) == []
    as_text = sql.replace("v_json jsonb;", "v_json text;").replace("::jsonb", "::text")
    assert as_text != sql
    assert any("not declared jsonb" in p for p in problems(as_text, LIVE_COLUMNS))
    cast_to_text = sql.replace("::jsonb", "::text")          # declared jsonb, but assigned through a text cast
    assert any("::jsonb cast" in p for p in problems(cast_to_text, LIVE_COLUMNS))
