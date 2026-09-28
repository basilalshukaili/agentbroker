-- revoked_jtis: durable single-token revocation, keyed on the token's jti.
--
-- CONTEXT. agent_interface/identity.py's revoke_token() (single leaked-token
-- revocation) added a jti to an in-memory-only set and reported success. It
-- had no durable counterpart, unlike the customer-level refund path
-- (_revoked_customer_ids / polar_order_events, hydrated by
-- _hydrate_revocations()) -- so a revoked token that survived to the next
-- process restart or container redeploy quietly became valid again. This
-- table is the durable half of the fix: _hydrate_jti_revocations() loads it
-- the same way _hydrate_revocations() loads polar_order_events (paged,
-- ordered, latch only on complete success, backoff-retried on failure), and
-- revoke_token()/revoke_jti() write to it via storage/supabase_client.py's
-- insert_row_strict so a failed durable write is never reported as success.
--
-- This is a SEPARATE table from polar_order_events on purpose: that table's
-- schema and meaning (order_id, customer_id, status) belong to the Polar
-- refund flow; a jti is not a customer_id and a single-token revoke is not a
-- refund event, so overloading one table for both would blur two different
-- audit trails for no benefit.
--
-- APPLY: paste into the Supabase SQL editor for the project at SUPABASE_URL,
-- same as migrations/idempotency_keys_claim_status.sql. NOT applied by this
-- change -- write it and describe it, per the task's hard rule; do not run
-- it anywhere. Until it is applied, jti revocation keeps working exactly as
-- before (in-memory, this process only) -- durability is additive, never a
-- precondition for the existing behaviour.

CREATE TABLE IF NOT EXISTS revoked_jtis (
    jti         TEXT PRIMARY KEY,
    reason      TEXT,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hydration reads newest-first and pages through the whole table (see
-- _hydrate_jti_revocations()); this index makes that ORDER BY cheap instead
-- of a full sort as the table grows.
CREATE INDEX IF NOT EXISTS revoked_jtis_revoked_at_idx ON revoked_jtis (revoked_at DESC);

-- Consistent with migrations/enable_rls.sql's policy for every other table:
-- service_role bypasses RLS entirely, so this is deny-all for anon/authenticated
-- and a no-op for the server, which always uses the service key.
ALTER TABLE revoked_jtis ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON revoked_jtis
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
