-- AgentBroker on the spine, 2026-10-04: operations_upsert has never worked, and it is the ONLY writer of the
-- durable operation record (get_status / get_outcome read it back).
--
-- THE DEFECT (measured 2026-10-03 on the spine, and the same on the old Supabase project - it was copied
-- faithfully, see docs/supabase-to-vps-cutover.md section 9 item 1, and ops/vps/spine/check_operations_upsert.sql):
--   1. the function writes operations.updated_at, a column the live table does not have (the repo's
--      migrations/operations_table.sql declares it; the table predates it)           -> SQLSTATE 42703
--   2. it passes p_result_json (text) into operations.result_json (jsonb) unchanged  -> 42804 once (1) is fixed
-- Every call therefore fails. storage/outcome_store.py catches it and logs "outcome_store_persist_failed", so
-- nothing crashes - the operation just is not recorded, and a later get_status / get_outcome after a restart
-- answers "unknown operation" for something that ran. Measured 2026-10-04: the table holds 61 rows, written
-- 2026-08-23 .. 2026-09-01 (by the older direct-table path, before sql/agentbroker/001 took the anon key's
-- table grant away and moved every write onto this function); not one row since.
--
-- THE FIX, deliberately small:
--   * add the column the repo always declared (idempotent; the table has 61 rows, the default fills them);
--   * cast the text to jsonb inside the function, treating a blank result - NULL, '' or only JSON whitespace
--     (space, tab, CR, LF) - as NULL ("no result", not a parse error), so the caller needs NO change: same name,
--     same parameter names and types, same grants.
--
-- ORDER: apply this BEFORE or AFTER any code release - it changes no signature, so the image already live and
-- every in-flight branch keep working. Until it is applied, behaviour is exactly what it is today.
-- Number 012 on purpose: feat/oauth-connect-20261003 already carries 011.
--
-- Idempotent. Owner stays spine_owner (BYPASSRLS), SECURITY DEFINER, search_path pinned, EXECUTE for anon and
-- service_role only - the same shape as migrations 009/010. The grants are repeated so a fresh database ends in
-- the same state, and `authenticated` is NOT granted (the live function has an ACL entry for it from the
-- Supabase default; revoking it is intended - nothing uses that role).

alter table public.operations
    add column if not exists updated_at timestamptz not null default now();

create or replace function public.operations_upsert(
    p_operation_id   text,
    p_tool           text,
    p_status         text,
    p_reason_code    text,
    p_appointment_id text,
    p_result_json    text,
    p_agent_id       text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row operations;
    v_json jsonb;
begin
    if p_operation_id is null or btrim(p_operation_id) = '' then
        raise exception 'operations_upsert: operation_id is required' using errcode = '22023';
    end if;
    -- NULL, '' and anything made only of JSON whitespace mean "no result"; anything else must be valid JSON
    -- (22P02 otherwise, loudly). btrim() with no argument strips spaces only, so a lone newline or tab would
    -- have reached the parser and been refused as "input string ended unexpectedly" (found by the
    -- real-database test, tests/unit/test_spine_012_operations_upsert_pg.py).
    v_json := nullif(btrim(p_result_json, E' \t\r\n'), '')::jsonb;

    insert into operations (
        operation_id, ts, tool, status, reason_code, appointment_id,
        result_json, agent_id, updated_at
    )
    values (
        p_operation_id, now(), p_tool, p_status, p_reason_code,
        p_appointment_id, v_json, p_agent_id, now()
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
$$;

revoke all on function public.operations_upsert(text, text, text, text, text, text, text)
    from public, anon, authenticated, service_role;
grant execute on function public.operations_upsert(text, text, text, text, text, text, text)
    to anon, service_role;

-- ----------------------------------------------------------------------------
-- VERIFY (read-only, or inside BEGIN ... ROLLBACK - DDL is transactional in Postgres):
--
--   begin;
--     \i migrations/spine/012_operations_upsert_fix.sql
--     select operations_upsert('zz-verify-012', 'verify', 'complete', null, null, '{"ok":true}', 'verify')
--            ->> 'operation_id';                                           -- expect zz-verify-012
--     select result_json->>'ok', updated_at is not null from operations where operation_id = 'zz-verify-012';
--                                                                           -- expect true | t
--     select operations_upsert('zz-verify-012', 'verify', 'failed', 'x', null, '', 'verify')->>'status';
--                                                                           -- expect failed (upsert path, blank result)
--   rollback;
--
--   select proacl::text from pg_proc where proname = 'operations_upsert';
--   -- expect {spine_owner=X/spine_owner,anon=X/spine_owner,service_role=X/spine_owner}
--
-- Through the public door (what the container does), with the anon JWT and Prefer: tx=rollback so no row stays:
--   POST /rest/v1/rpc/operations_upsert  {"p_operation_id":"zz-verify-012", ... }  -> 200 and a jsonb row.
-- ----------------------------------------------------------------------------
