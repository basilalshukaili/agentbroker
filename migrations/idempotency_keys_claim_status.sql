-- idempotency_keys: durable claim status, for safe concurrent/interrupted retries.
--
-- CONTEXT. agent_interface/idempotency_gate.py originally wrote this table
-- ONLY after a tool call finished successfully (`put()`, called after
-- dispatch). That has no way to represent "someone is mid-flight right now"
-- or "a process claimed this key and then crashed before finishing" -- both
-- states read back as "no row", which the gate then treated as "safe to
-- execute", the exact double-booking/double-charge hazard this migration and
-- the accompanying code fix close. The fixed gate now writes a `pending` row
-- BEFORE dispatching the tool (claim), updates it to `complete` with the
-- replayable response on success, or `released` on a transient failure so a
-- legitimate retry with the same key is not durably blocked forever.
--
-- APPLY: paste into the Supabase SQL editor for the project at SUPABASE_URL,
-- same as migrations/operations_table.sql. NOT applied by this change --
-- write it and describe it, per the task's hard rule; do not run it anywhere.
--
-- The table itself is not defined by any migration in this repo (it predates
-- the migrations/ convention -- see migrations/enable_rls.sql, which only
-- turns RLS on for it). CREATE TABLE IF NOT EXISTS below is therefore the
-- full definition this fix now depends on, not just an ALTER.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id           BIGSERIAL PRIMARY KEY,
    agent_scope  TEXT NOT NULL,
    operation    TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    -- 'pending' (claimed, tool dispatch in flight or crashed before
    -- resolving) | 'complete' (successful, response is replayable) |
    -- 'released' (a transient failure freed the key for a fresh retry).
    status       TEXT NOT NULL DEFAULT 'pending',
    args_hash    TEXT,
    response     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Backward-compatible: adds the column to a table created before this fix
-- (every existing row predates the claim/complete/release split and was
-- only ever written on success, so backfilling it to 'complete' is the
-- honest reading of what those rows already represent).
ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
UPDATE idempotency_keys SET status = 'complete' WHERE status = 'pending' AND response IS NOT NULL;

-- THE UNIQUE CONSTRAINT THAT MAKES A CLAIM ACTUALLY ATOMIC ACROSS PROCESSES.
--
-- The application-level fix (idempotency_gate.py's claim()) does a SELECT
-- then an INSERT, which is correct for same-process concurrency (protected
-- separately, in memory, by storage/idempotency_store.py's reserve()) and
-- for the sequential crash-then-restart case this task asked for, but it is
-- NOT by itself an atomic compare-and-set against a second process racing
-- the SAME select-then-insert window at the same instant. This constraint is
-- what closes that last gap: a genuine simultaneous double-INSERT from two
-- processes for the same key can only ever succeed once at the database
-- level, and the loser's insert_row() call returns None (its existing,
-- already-fail-open contract) rather than creating a second pending row.
-- Without this constraint two processes that both pass the SELECT check in
-- the same instant could both proceed to INSERT and both execute the tool --
-- the one true-simultaneous-cross-process race this migration exists to
-- close. It is written, not applied, per this task's hard rule; until it is
-- applied, that one specific race (two SEPARATE machines/processes claiming
-- the identical key in the same few milliseconds) is reduced, not eliminated.
CREATE UNIQUE INDEX IF NOT EXISTS idempotency_keys_scope_op_key_uq
    ON idempotency_keys (agent_scope, operation, idem_key);

CREATE INDEX IF NOT EXISTS idempotency_keys_status_idx ON idempotency_keys (status);

-- Consistent with migrations/enable_rls.sql's policy for every other table:
-- service_role bypasses RLS entirely, so this is deny-all for anon/authenticated
-- and a no-op for the server, which always uses the service key.
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON idempotency_keys
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
