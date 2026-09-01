-- Leads table: durable store behind the capture_lead tool.
--
-- capture_lead used to persist nothing — it returned a uuid5 of the dedup key
-- as though it were a record locator and reported status=partial. This table is
-- what makes the tool real: core/capture_lead.py takes the lead_id from the
-- inserted row and charges only when a row is actually written.
--
-- dedup_key is UNIQUE and is the whole idempotency mechanism. The handler
-- builds it as  smb_id || '|' || (phone or email or name)  and, on a unique
-- violation, reads the existing row back and returns the SAME lead_id with
-- cost 0.00. Change that formula and every row already stored stops matching.
--
-- STATE: this table was created by hand in the Supabase project at SUPABASE_URL
-- on 2026-09-01, BEFORE this file existed. The DDL below is the schema in
-- version control, written to match what is already live — it is idempotent, so
-- re-running it against that project is a no-op.
--
-- APPLY (new environments): paste into the Supabase SQL editor for the project
-- at SUPABASE_URL, then confirm SUPABASE_URL + SUPABASE_SERVICE_KEY are set on
-- the Render service (Environment tab -> smb-broker service).

CREATE TABLE IF NOT EXISTS public.leads (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dedup_key      TEXT NOT NULL UNIQUE,
    smb_id         TEXT NOT NULL,
    prospect_name  TEXT,
    prospect_phone TEXT,
    prospect_email TEXT,
    source         TEXT,
    notes          TEXT,
    agent_id       TEXT,
    channel_used   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The SMB reading its own funnel, and the newest-first listing.
CREATE INDEX IF NOT EXISTS leads_smb_id_idx ON public.leads (smb_id);
CREATE INDEX IF NOT EXISTS leads_created_at_idx ON public.leads (created_at DESC);

-- Enable RLS (consistent with enable_rls.sql policy — service_role bypasses it).
-- Lead rows are prospects' names, phone numbers and email addresses: anon must
-- never be able to read this table.
ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

-- Service role can read and write; anon cannot.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'leads'
          AND policyname = 'service_role_full_access'
    ) THEN
        CREATE POLICY "service_role_full_access" ON public.leads
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;
END
$$;
