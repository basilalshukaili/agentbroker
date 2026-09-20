-- Operations table: durable async-receipt store for get_status / get_outcome.
--
-- This table is the persistence backend for outcome_store.py. Without it,
-- get_status and get_outcome return not_found for every operation_id because
-- each Render request runs in a fresh process with an empty in-memory cache.
--
-- APPLY: paste this into the Supabase SQL editor for the project at SUPABASE_URL,
-- then confirm SUPABASE_URL + SUPABASE_SERVICE_KEY are set on the Render service
-- (Environment tab → smb-broker service).

CREATE TABLE IF NOT EXISTS operations (
    operation_id   TEXT PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool           TEXT,
    status         TEXT,
    reason_code    TEXT,
    -- The PROVIDER's own booking id (Cal.com uid) for a confirmed
    -- appointment, so a cancellation -- which only ever holds this id, never
    -- our operation_id -- can find the operation row that owns it, on a
    -- process other than the one that wrote it. Populated only for the
    -- appointment_confirmed outcome; NULL for everything else. See
    -- storage/outcome_store.py's get_appointment_owner_async and
    -- core/schedule_appointment.py's cancellation-authorization check
    -- (tests/unit/test_cancellation_authorization.py).
    appointment_id TEXT,
    result_json    TEXT,
    agent_id       TEXT,
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Backward-compatible: adds the column to a table created before this fix,
-- without disturbing existing rows (they resolve to NULL == "no recorded
-- appointment owner", which the read path already treats as fail-closed).
ALTER TABLE operations ADD COLUMN IF NOT EXISTS appointment_id TEXT;

-- Index for the common poll-by-agent-id query pattern (future use).
CREATE INDEX IF NOT EXISTS operations_agent_id_idx ON operations (agent_id)
    WHERE agent_id IS NOT NULL;

-- Index for the cancellation-ownership lookup (by provider booking id).
CREATE INDEX IF NOT EXISTS operations_appointment_id_idx ON operations (appointment_id)
    WHERE appointment_id IS NOT NULL;

-- Enable RLS (consistent with enable_rls.sql policy — service_role bypasses it).
ALTER TABLE operations ENABLE ROW LEVEL SECURITY;

-- Service role can read and write; anon cannot.
CREATE POLICY "service_role_full_access" ON operations
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
