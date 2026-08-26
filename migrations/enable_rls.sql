-- Row Level Security lockdown.
--
-- FOUND 2026-08-26, live exposure. Fourteen public tables had RLS disabled, so
-- anything holding the project's ANON key could read them through PostgREST.
-- Proven, not theorised: a direct anon-key GET returned rows from
-- `pending_keys`, which stores the EMAIL and VERIFICATION TOKEN of every
-- in-flight free-key request. That token is what /keys/verify consumes, so
-- reading the table was enough to claim someone else's API key.
--
-- The rest were empty only because traffic has not started. `conversations`
-- and `conversation_messages` hold real message content between an end user's
-- agent and a business; `whatsapp_inbound`, `waitlist` and `escalations` hold
-- personal data. They would have leaked the moment they filled up.
--
-- WHY RLS WITH NO POLICIES IS THE RIGHT FIX HERE. `service_role` BYPASSES RLS
-- entirely, and every server path uses the service key:
--   * the MCP origin  -> storage/supabase_client.py prefers SUPABASE_SERVICE_KEY
--   * the site's API routes (activity, waitlist, orchestrator) -> SUPABASE_SERVICE_KEY
-- Production has SUPABASE_SERVICE_KEY set and SUPABASE_ANON_KEY absent, so
-- there is no anon code path to break. Enabling RLS with zero policies is
-- therefore deny-all for anon/authenticated and a no-op for us.
--
-- Do NOT "fix" a future permission error by adding a permissive policy. If a
-- component cannot read, it is using the wrong key.
--
-- Verified after applying: anon-key GET returns 0 rows on every table below;
-- service-key GET returns rows; all 10 system_health checks green; the gated
-- dashboard route still returns live events.

alter table public.usage_events          enable row level security;
alter table public.operations            enable row level security;
alter table public.anon_data_quota       enable row level security;
alter table public.whatsapp_inbound      enable row level security;
alter table public.pending_keys          enable row level security;  -- takeover vector
alter table public.waitlist              enable row level security;
alter table public.escalations           enable row level security;
alter table public.orchestrator_state    enable row level security;
alter table public.consent_optouts       enable row level security;
alter table public.conversation_messages enable row level security;
alter table public.conversations         enable row level security;
alter table public.demand_digests        enable row level security;
alter table public.idempotency_keys      enable row level security;
alter table public.pending_requests      enable row level security;

-- Any table added later must be created WITH RLS enabled. To audit:
--
--   select c.relname, c.relrowsecurity
--   from pg_class c join pg_namespace n on n.oid = c.relnamespace
--   where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity = false;
--
-- That query should return zero rows.
