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
