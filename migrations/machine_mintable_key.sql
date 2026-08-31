-- machine_mintable_key.sql
-- Adds source column to pending_keys to distinguish machine-minted rows from
-- human email-verified ones (P1 backlog item #6).
--
-- Apply via the Supabase SQL editor or Management API.
-- Safe to re-run: uses IF NOT EXISTS / DO $$ EXCEPTION pattern.
--
-- After applying, machine-minted key records land as:
--   email  = "agent:<agent_id>"    (surrogate — no real inbox)
--   source = "machine_minted"
--   token  = first 512 chars of the issued JWT (read-only audit trail)
--
-- Human email-verified rows keep source = NULL (backward compatible).

BEGIN;

-- Add source column if it does not exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = 'pending_keys'
           AND column_name  = 'source'
    ) THEN
        ALTER TABLE public.pending_keys
            ADD COLUMN source TEXT;
    END IF;
END $$;

-- Optional: index for audits / monitoring queries
CREATE INDEX IF NOT EXISTS pending_keys_source_idx
    ON public.pending_keys (source)
    WHERE source IS NOT NULL;

COMMIT;
