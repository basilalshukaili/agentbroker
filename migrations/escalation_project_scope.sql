-- Add project_id to escalations table for cross-project isolation.
--
-- WHY: AgentBroker (HatchLoop) and TechMate OS both write to this shared
-- Supabase table. Without project_id, TechMate OS cannot filter to its own
-- escalations — it sees AgentBroker records as notification noise.
-- lane_guard does not protect this path (no HTTP layer to gate on).
--
-- APPLY: paste into the Supabase SQL editor for the AgentBroker project.
-- Existing rows default to 'hatchloop' (the only prior producer).

ALTER TABLE public.escalations
    ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'hatchloop';

CREATE INDEX IF NOT EXISTS escalations_project_id_idx ON public.escalations (project_id);
