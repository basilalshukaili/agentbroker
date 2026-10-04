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
-- THE FIX. Two parts make the function work; three rules make it safe to have working, because a function that has
-- never worked has also never been exposed to what its callers do with it.
--
-- To make it work:
--   * add the column the repo always declared (idempotent; the table has 61 rows, the default fills them);
--   * cast the text to jsonb inside the function, treating a blank result - NULL, '' or only JSON whitespace
--     (space, tab, CR, LF) - as NULL ("no result", not a parse error), so the caller needs NO change: same name,
--     same parameter names and types, and the same anon / service_role access (the one access change,
--     `authenticated`, is explained below).
--
-- The rules (each is proven against a real PostgreSQL in tests/unit/test_spine_012_operations_upsert_pg.py):
--   1. ARRIVAL ORDER. The service writes one operation several times (pending, then the final receipt; see
--      storage/outcome_store.py) as independent fire-and-forget HTTP calls, and two calls for one operation can
--      pass each other. A plain "last write wins" upsert let a late `pending` - every other column NULL - wipe the
--      reason, appointment, result and owner of a booking that was already confirmed (in a run of 30 concurrent
--      pending/final pairs against the first committed version, 13 rows ended that way; the number varies run to
--      run). So a write whose status is IN PROGRESS (pending, executing, pending_async) never replaces a row whose
--      status is final (anything else: success, failure, partial, unknown, the legacy 'complete', ...). The
--      statement then answers with the row that stands, never NULL (a NULL answer is logged by the service as a
--      failed write). Final replaces in-progress, in-progress replaces in-progress, and final replaces final as a
--      FULL overwrite: a blank result is still "no result" and a later reason replaces an earlier one. The order
--      is by STATUS CLASS, not by time, so it relies on a write being labelled with the status it was made with;
--      storage/outcome_store.py's _fire_persist used to hand the writer the LIVE record, so a delayed first write
--      read 'failure' by the time it ran. It now snapshots the record when the write is made (same branch,
--      tests/unit/test_outcome_store_write_snapshot.py).
--   2. NOTHING IS CLEARED BY OMISSION. agent_id and appointment_id keep what is recorded when the new write
--      carries NULL: coalesce(new, existing). That is the in-memory store's own rule ("only ever SET the owner,
--      never clear it") applied durably. The set_executing write carries no owner at all and would otherwise clear
--      it, and a fresh process would then answer operation_owner_unknown even to the agent that owns it.
--   3. WHO MAY WRITE WHICH ROW. The anon role reaches this function, and the owner, the appointment id and the
--      status come from its seven arguments. Reproduced on the first committed version of this file: one call as
--      anon with the id of a row owned by agent-victim and the identity agent-attacker, and
--      get_appointment_owner_async('appt-victim') answered agent-attacker - which core/ownership.read_denial
--      accepts for a CANCELLATION. A fresh operation id carrying the victim's appointment id did the same (the
--      appointment reader is `limit 1` with no ordering). So:
--        - a row that has an owner is written only by that owner, or by a write that names none (the service's
--          own set_executing names none). A write that names a DIFFERENT owner is REFUSED with 42501, whatever its
--          status, so the caller is told; it does not silently do nothing. The check is part of the ON CONFLICT
--          condition, so it is judged against the row's latest committed version and holds under a race. A row
--          with no owner takes the first owner it is given, and keeps it.
--        - an appointment id belongs to whoever first recorded it: a write for ANOTHER operation that carries an
--          appointment id already recorded under a different owner is refused with 42501. "Different" includes
--          no owner at all, in both directions: a write that names no agent cannot take an owned booking's id
--          (an unowned row carrying the victim's id could be the one the reader returns, and an unowned owner
--          fails every cancellation closed), and a write that names an agent cannot take an unowned booking's id
--          (that would turn "nobody may cancel this" into "this agent may"). Two agents claiming one free
--          appointment id at the same moment are serialised by an advisory lock keyed on the id.
--        The refusal text names the operation or appointment id the caller sent, never the owner.
--        The rules apply to EVERY caller of the function, service_role included. An operator who must correct an
--        owner does it with a plain UPDATE as spine_owner, not through this function.
--
-- WHAT THIS DOES NOT DEFEND. Whoever holds the anon key (the service container and the founder's laptop hold it;
-- the review found no copy in any web bundle) can still:
--   * write any FRESH operation id under any agent id they choose;
--   * write under the victim's own agent id, if they know it;
--   * change the status and result of an owned row with a write that names no agent, if they know its operation
--     id (an unguessable uuid4 that only the owner is given);
--   * block a booking's own write by claiming its appointment id first - which needs the provider's booking id
--     before the booking exists, and no caller has that.
-- Closing the first three is a separate writer credential for this one function: a role that is not anon, with
-- anon's EXECUTE revoked and the container's configuration changed in the same release. That is a follow-up that
-- needs a decision (a new secret on the box); it is not something a function body can do. Until then this file
-- narrows what an anonymous-key holder can do to an EXISTING booking; it does not make the key safe to publish.
--
-- ORDER: apply this TOGETHER WITH or AFTER the release that contains storage/outcome_store.py's snapshot fix (the
-- same branch). It changes no signature, so applying it first breaks nothing and the image already live keeps
-- working - but on the old image a pending write and the final write made in the same tick are BOTH labelled with
-- the final status (the writer read the live record), so rule 1 cannot tell them apart and the durable row can
-- still lose its reason, appointment and result. Until it is applied at all, behaviour is exactly what it is today:
-- nothing is recorded.
-- Number 012 on purpose: 011 (OAuth Connect) is already in the base this branch was cut from.
--
-- THE APPLY ITSELF takes ACCESS EXCLUSIVE on operations for the `alter table`, and spine_owner has no
-- lock_timeout. One open transaction that has merely READ the table (a pg_dump, a forgotten psql) would make the
-- apply wait for ever, and every NEW reader queues behind the waiting apply, so get_status / get_outcome would
-- answer "store unavailable" until the holder ended. The file therefore sets its own lock_timeout as its first
-- statement (SET LOCAL: it lasts for the transaction hatchloop's scripts/apply_sql.py wraps the file in, and no
-- longer): the
-- apply fails in 3 seconds with 55P03, nothing is changed, and it can be re-run. Apply outside a backup window.
--
-- Idempotent. Owner stays spine_owner (BYPASSRLS), SECURITY DEFINER, search_path pinned, EXECUTE for anon and
-- service_role only - the same shape as migrations 009/010. The grants are repeated so a fresh database ends in
-- the same state, and `authenticated` is NOT granted (the live function has an ACL entry for it from the
-- Supabase default; revoking it is intended - nothing uses that role).
--
-- KNOWN LIMITS (none of them new; before this file nothing at all was recorded):
--   * jsonb refuses a result containing a NUL character (22P05) or a NaN / Infinity number (22P02). The service
--     now cleans both before it sends (storage/outcome_store.py) and, if the database still refuses a result with
--     a data exception, writes the row once more without it so status, reason, appointment and owner still land;
--     another caller of this function that sends such text gets the error.
--   * p_tool and p_status must not be NULL (23502: the columns are NOT NULL). The service always sends both.
--   * the 61 existing rows get updated_at = the apply time (the column default), not their original ts. Nothing
--     reads updated_at; the service reads ts.
--
-- SHIP STEP (a migration is applied by hand; merging or deploying applies nothing):
--   1. OAUTH_PG_TESTS=1 python -m pytest tests/unit/test_spine_012_operations_upsert.py \
--          tests/unit/test_spine_012_operations_upsert_pg.py tests/unit/test_outcome_store_write_snapshot.py -rs
--      and require ZERO skipped. A skip means docker, asyncpg or the postgres image was missing and the
--      real-database tests did not run; CI does not set OAUTH_PG_TESTS, so by default only the structural tests do.
--   2. apply as spine_owner with projects/hatchloop/scripts/apply_sql.py (one transaction), then run VERIFY part 2;
--   3. log it in sql/agentbroker/APPLIED.md, and re-run ops/vps/spine/verify_spine_public.py --parts e
--      --operations-upsert-fixed (the verifier otherwise still tolerates the old 42703).

set local lock_timeout = '3s';

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
    v_row  operations;
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

    -- Rule 3, the appointment half: an appointment id belongs to the agent that first recorded it. The lock makes
    -- the check and the write one step for two claimants of the same id (it is released with the transaction); the
    -- lock order is always appointment first, row second, so it cannot deadlock with the row lock below.
    if p_appointment_id is not null then
        perform pg_advisory_xact_lock(hashtextextended('operations_upsert:appointment:' || p_appointment_id, 0));
        if exists (
            select 1
            from operations o
            where o.appointment_id = p_appointment_id and o.operation_id <> p_operation_id and o.agent_id is distinct from p_agent_id
        ) then
            raise exception 'operations_upsert: appointment % is recorded for another agent', p_appointment_id
                using errcode = '42501';
        end if;
    end if;

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
        appointment_id = coalesce(excluded.appointment_id, operations.appointment_id),
        result_json    = excluded.result_json,
        agent_id       = coalesce(excluded.agent_id, operations.agent_id),
        updated_at     = excluded.updated_at
    -- Both conditions are evaluated against the row's LATEST committed version (PostgreSQL waits for a concurrent
    -- writer of the row first), so they hold under a race.
    -- Rule 3: only the owner (or a write that names none, or a row that has none) may update.
    -- Rule 1: an in-progress write may only replace another in-progress row.
    where (operations.agent_id is null or excluded.agent_id is null or operations.agent_id = excluded.agent_id)
      and (operations.status in ('pending', 'executing', 'pending_async')
           or excluded.status not in ('pending', 'executing', 'pending_async'))
    returning * into v_row;

    if not found then
        -- The update was skipped. Either a late in-progress write for an operation that already has its final
        -- outcome (rule 1: answer with the row that stands, so the caller sees a successful write and not a NULL),
        -- or a write that names a different owner (rule 3: refuse it, loudly, so the caller is told).
        select * into v_row from operations where operation_id = p_operation_id;
        if v_row.agent_id is not null and p_agent_id is not null and v_row.agent_id <> p_agent_id then
            raise exception 'operations_upsert: operation % is recorded for another agent', p_operation_id
                using errcode = '42501';
        end if;
    end if;

    return to_jsonb(v_row);
end;
$$;

revoke all on function public.operations_upsert(text, text, text, text, text, text, text)
    from public, anon, authenticated, service_role;
grant execute on function public.operations_upsert(text, text, text, text, text, text, text)
    to anon, service_role;

-- ----------------------------------------------------------------------------
-- VERIFY (read-only, or inside BEGIN ... ROLLBACK - DDL is transactional in Postgres, so nothing stays).
--
-- 1. Before committing: apply the file inside a transaction and drive the function. The ACL is checked INSIDE the
--    transaction - after the ROLLBACK the live function shows its old ACL again, with `authenticated`.
--
--   begin;
--     \i migrations/spine/012_operations_upsert_fix.sql
--     select (select array_agg(a order by a) from unnest(proacl::text[]) a)
--            = array['anon=X/spine_owner','service_role=X/spine_owner','spine_owner=X/spine_owner']
--       from pg_proc where proname = 'operations_upsert';                         -- expect t
--     select operations_upsert('zz-verify-012', 'verify', 'success', 'appointment_confirmed', 'appt-zz',
--                              '{"ok":true}', 'verify') ->> 'operation_id';       -- expect zz-verify-012
--     select result_json->>'ok', updated_at is not null from operations where operation_id = 'zz-verify-012';
--                                                                                 -- expect true | t
--     select operations_upsert('zz-verify-012', 'verify', 'pending', null, null, null, null) ->> 'status';
--                                                                                 -- expect success (a late in-progress
--                                                                                 -- write does not replace a final one)
--     select agent_id, appointment_id, reason_code from operations where operation_id = 'zz-verify-012';
--                                                                                 -- expect verify | appt-zz | appointment_confirmed
--     select operations_upsert('zz-verify-012', 'verify', 'failure', 'x', null, '', null) ->> 'status';
--                                                                                 -- expect failure (final over final, blank result)
--     select result_json is null, agent_id, appointment_id from operations where operation_id = 'zz-verify-012';
--                                                                                 -- expect t | verify | appt-zz
--     savepoint a;
--     select operations_upsert('zz-verify-012', 'verify', 'success', null, null, null, 'someone-else');
--                                                                                 -- expect ERROR 42501 (another agent's row)
--     rollback to a;                                                              -- (an ERROR aborts the transaction)
--     savepoint b;
--     select operations_upsert('zz-verify-012-b', 'verify', 'success', 'appointment_confirmed', 'appt-zz',
--                              null, 'someone-else');                             -- expect ERROR 42501 (its appointment id)
--     rollback to b;
--   rollback;
--
-- 2. After a COMMITTED apply only (before that the live function still shows the old ACL, with `authenticated`):
--
--   select (select array_agg(a order by a) from unnest(proacl::text[]) a)
--          = array['anon=X/spine_owner','service_role=X/spine_owner','spine_owner=X/spine_owner']
--     from pg_proc where proname = 'operations_upsert';                          -- expect t
--
-- 3. Through the public door (what the container does), with the anon JWT and `Prefer: tx=rollback` so no row stays
--    (PostgREST runs with db-tx-end = commit-allow-override, see docs/supabase-to-vps-cutover.md):
--   POST /rest/v1/rpc/operations_upsert
--        {"p_operation_id":"zz-verify-012","p_tool":"verify","p_status":"success","p_reason_code":null,
--         "p_appointment_id":null,"p_result_json":"{\"ok\":true}","p_agent_id":"verify"}   -> 200 and a jsonb row.
-- ----------------------------------------------------------------------------
