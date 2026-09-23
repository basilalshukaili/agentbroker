-- credits_billing.sql
-- AgentBroker credits billing schema (slice 2, 2026-08-24)
-- Apply via Supabase Management API or the SQL editor in the Supabase dashboard.
-- Safe to re-run: uses IF NOT EXISTS and replaces the RPC functions.
-- Existing installations must reapply this file after review to replace the
-- billing functions, add commit replay metadata and revoke inherited PUBLIC
-- execution rights from all four billing RPCs.
-- 1 credit = 1 US cent. Balance never goes negative (CHECK constraint enforced).

BEGIN;

-- ============================================================================
-- TABLES
-- ============================================================================

CREATE TABLE IF NOT EXISTS credit_accounts (
    account_id              TEXT PRIMARY KEY,
    customer_id             TEXT,
    email                   TEXT,
    plan                    TEXT NOT NULL DEFAULT 'free',
    balance_credits         BIGINT NOT NULL DEFAULT 0 CHECK (balance_credits >= 0),
    lifetime_granted        BIGINT NOT NULL DEFAULT 0,
    lifetime_spent          BIGINT NOT NULL DEFAULT 0,
    key_token               TEXT,
    key_jti                 TEXT,
    low_balance_notified_at TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              BIGSERIAL PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES credit_accounts(account_id),
    entry_type      TEXT NOT NULL CHECK (entry_type IN (
                        'grant', 'topup', 'hold', 'commit', 'refund', 'adjustment'
                    )),
    amount_credits  BIGINT NOT NULL,   -- signed: negative for holds, positive for grants/refunds/topups
    actual_credits  BIGINT,            -- immutable actual cost on a commit marker; NULL on historical rows
    operation       TEXT,
    operation_id    TEXT,
    hold_id         TEXT,
    idempotency_key TEXT,
    reason_code     TEXT,
    source          TEXT,
    order_id        TEXT,
    balance_after   BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Existing installations may already have commit markers. Their unknown actual
-- cost stays NULL and must not be treated as a verified idempotent replay.
ALTER TABLE credit_ledger ADD COLUMN IF NOT EXISTS actual_credits BIGINT;

-- UNIQUE per (hold_id, entry_type): one hold, one commit, one refund per hold_id
CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_hold_type_ux
    ON credit_ledger(hold_id, entry_type)
    WHERE hold_id IS NOT NULL;

-- UNIQUE per idempotency_key (partial: NULL keys not constrained)
CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_idempotency_ux
    ON credit_ledger(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Fast per-account history queries
CREATE INDEX IF NOT EXISTS credit_ledger_account_created_idx
    ON credit_ledger(account_id, created_at DESC);

-- Enable RLS (service_role bypasses RLS automatically; anon/authenticated blocked)
ALTER TABLE credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_ledger   ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- ATOMIC RPC FUNCTIONS (SECURITY DEFINER -- bypass RLS, run as table owner)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- credit_reserve
-- Lock account row; if balance >= positive amount, decrement and insert hold.
-- A hold_id replay must match the original account, amount and operation.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_reserve(
    p_account   TEXT,
    p_amount    BIGINT,
    p_hold_id   TEXT,
    p_op        TEXT DEFAULT NULL,
    p_op_id     TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_balance   BIGINT;
    v_rows      INTEGER;
    v_prior     RECORD;
BEGIN
    IF p_account IS NULL OR btrim(p_account) = ''
       OR p_hold_id IS NULL OR btrim(p_hold_id) = ''
       OR p_amount IS NULL OR p_amount <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'invalid_reservation');
    END IF;

    -- Lock the account row for the duration of this transaction
    SELECT balance_credits INTO v_balance
    FROM public.credit_accounts
    WHERE account_id = p_account
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'ok',          false,
            'reason_code', 'account_not_found',
            'balance',     0,
            'balance_after', 0
        );
    END IF;

    -- An exact retry is valid even if other charges have since reduced the
    -- balance. It must not reserve again after this hold has been finalized.
    SELECT account_id, amount_credits, operation, operation_id INTO v_prior
    FROM public.credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'hold';
    IF FOUND THEN
        IF v_prior.account_id IS DISTINCT FROM p_account
           OR v_prior.amount_credits IS DISTINCT FROM -p_amount
           OR v_prior.operation IS DISTINCT FROM p_op
           OR v_prior.operation_id IS DISTINCT FROM p_op_id THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_conflict');
        END IF;
        IF EXISTS (SELECT 1 FROM public.credit_ledger
                   WHERE hold_id = p_hold_id AND entry_type IN ('commit', 'refund')) THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_finalized');
        END IF;
        RETURN jsonb_build_object('ok', true, 'idempotent', true,
                                  'balance_after', v_balance);
    END IF;

    IF v_balance < p_amount THEN
        RETURN jsonb_build_object(
            'ok',          false,
            'reason_code', 'insufficient_credits',
            'balance',     v_balance,
            'balance_after', v_balance
        );
    END IF;

    -- Insert hold (idempotent: do nothing on duplicate hold_id+entry_type)
    INSERT INTO public.credit_ledger (
        account_id, entry_type, amount_credits,
        operation, operation_id, hold_id, balance_after
    )
    VALUES (
        p_account, 'hold', -p_amount,
        p_op, p_op_id, p_hold_id, v_balance - p_amount
    )
    ON CONFLICT (hold_id, entry_type) WHERE hold_id IS NOT NULL DO NOTHING;

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows > 0 THEN
        -- Hold was newly inserted; decrement balance
        UPDATE public.credit_accounts
        SET balance_credits = balance_credits - p_amount,
            updated_at      = NOW()
        WHERE account_id = p_account;

        RETURN jsonb_build_object(
            'ok',          true,
            'balance_after', v_balance - p_amount
        );
    ELSE
        -- A concurrent insert won the unique hold key, possibly for another
        -- account. Verify its immutable binding after the conflicting insert
        -- commits; a duplicate key alone is never authority to proceed.
        SELECT account_id, amount_credits, operation, operation_id INTO v_prior
        FROM public.credit_ledger
        WHERE hold_id = p_hold_id AND entry_type = 'hold';
        IF NOT FOUND OR v_prior.account_id IS DISTINCT FROM p_account
           OR v_prior.amount_credits IS DISTINCT FROM -p_amount
           OR v_prior.operation IS DISTINCT FROM p_op
           OR v_prior.operation_id IS DISTINCT FROM p_op_id THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_conflict');
        END IF;
        IF EXISTS (SELECT 1 FROM public.credit_ledger
                   WHERE hold_id = p_hold_id AND entry_type IN ('commit', 'refund')) THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_finalized');
        END IF;
        RETURN jsonb_build_object(
            'ok',          true,
            'idempotent',  true,
            'balance_after', v_balance
        );
    END IF;
END;
$$;


-- ----------------------------------------------------------------------------
-- credit_commit
-- Finalize a hold. If actual < held, refund the difference and update balance.
-- Updates lifetime_spent. A duplicate hold_id is idempotent only when its
-- recorded actual cost and refund match; release and commit are exclusive.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_commit(
    p_hold_id   TEXT,
    p_actual    BIGINT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_held      BIGINT;
    v_account   TEXT;
    v_balance   BIGINT;
    v_diff      BIGINT;
    v_prior_actual BIGINT;
    v_refund       BIGINT;
    v_refund_reason TEXT;
BEGIN
    IF p_hold_id IS NULL OR btrim(p_hold_id) = ''
       OR p_actual IS NULL OR p_actual < 0 THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'invalid_actual');
    END IF;

    -- Find the hold entry
    SELECT -amount_credits, account_id
    INTO v_held, v_account
    FROM public.credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'hold';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_not_found');
    END IF;

    IF v_held <= 0 OR p_actual > v_held THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'invalid_actual');
    END IF;

    -- Lock the account row
    SELECT balance_credits INTO v_balance
    FROM public.credit_accounts
    WHERE account_id = v_account
    FOR UPDATE;

    -- The account lock serializes commit and release for this hold. A prior
    -- commit is replayable only with its recorded actual cost and refund.
    SELECT actual_credits INTO v_prior_actual
    FROM public.credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'commit';
    IF FOUND THEN
        IF v_prior_actual IS DISTINCT FROM p_actual THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'commit_conflict');
        END IF;
        SELECT amount_credits, reason_code INTO v_refund, v_refund_reason
        FROM public.credit_ledger
        WHERE hold_id = p_hold_id AND entry_type = 'refund';
        IF (v_held > p_actual AND (NOT FOUND
                                  OR v_refund IS DISTINCT FROM v_held - p_actual
                                  OR v_refund_reason IS DISTINCT FROM 'commit_partial_refund'))
           OR (v_held = p_actual AND FOUND) THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'commit_conflict');
        END IF;
        RETURN jsonb_build_object('ok', true, 'idempotent', true,
                                  'balance_after', v_balance);
    END IF;

    IF EXISTS (SELECT 1 FROM public.credit_ledger
               WHERE hold_id = p_hold_id AND entry_type = 'refund') THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_released');
    END IF;

    INSERT INTO public.credit_ledger (
        account_id, entry_type, amount_credits, actual_credits,
        hold_id, balance_after
    )
    VALUES (v_account, 'commit', 0, p_actual, p_hold_id, v_balance);

    -- First commit: handle partial refund and lifetime_spent
    v_diff := v_held - p_actual;

    IF v_diff > 0 THEN
        -- Actual cost < held: refund the difference
        INSERT INTO public.credit_ledger (
            account_id, entry_type, amount_credits, hold_id, reason_code, balance_after
        )
        VALUES (v_account, 'refund', v_diff, p_hold_id, 'commit_partial_refund', v_balance + v_diff);

        UPDATE public.credit_accounts
        SET balance_credits = balance_credits + v_diff,
            lifetime_spent  = lifetime_spent  + p_actual,
            updated_at      = NOW()
        WHERE account_id = v_account;

        v_balance := v_balance + v_diff;
    ELSE
        -- Actual == held: no refund needed
        UPDATE public.credit_accounts
        SET lifetime_spent = lifetime_spent + p_actual,
            updated_at     = NOW()
        WHERE account_id = v_account;
    END IF;

    RETURN jsonb_build_object('ok', true, 'balance_after', v_balance);
END;
$$;


-- ----------------------------------------------------------------------------
-- credit_release
-- Release a hold on tool failure: refund the full held amount back to balance.
-- Idempotent only for the same reason and held amount, before any commit.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_release(
    p_hold_id   TEXT,
    p_reason    TEXT DEFAULT 'release'
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_held      BIGINT;
    v_account   TEXT;
    v_balance   BIGINT;
    v_prior_refund BIGINT;
    v_prior_reason TEXT;
BEGIN
    IF p_hold_id IS NULL OR btrim(p_hold_id) = '' THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_not_found');
    END IF;

    SELECT -amount_credits, account_id
    INTO v_held, v_account
    FROM public.credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'hold';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_not_found');
    END IF;

    IF v_held <= 0 THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'invalid_hold');
    END IF;

    SELECT balance_credits INTO v_balance
    FROM public.credit_accounts
    WHERE account_id = v_account
    FOR UPDATE;

    -- Commit and release are mutually exclusive under the same account lock.
    -- This also refuses historical rows where a release followed a commit.
    IF EXISTS (SELECT 1 FROM public.credit_ledger
               WHERE hold_id = p_hold_id AND entry_type = 'commit') THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_committed');
    END IF;

    SELECT amount_credits, reason_code INTO v_prior_refund, v_prior_reason
    FROM public.credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'refund';
    IF FOUND THEN
        IF v_prior_refund IS DISTINCT FROM v_held
           OR v_prior_reason IS DISTINCT FROM p_reason THEN
            RETURN jsonb_build_object('ok', false, 'reason_code', 'release_conflict');
        END IF;
        RETURN jsonb_build_object('ok', true, 'idempotent', true,
                                  'balance_after', v_balance);
    END IF;

    INSERT INTO public.credit_ledger (
        account_id, entry_type, amount_credits, hold_id, reason_code, balance_after
    )
    VALUES (v_account, 'refund', v_held, p_hold_id, p_reason, v_balance + v_held);

    UPDATE public.credit_accounts
    SET balance_credits = balance_credits + v_held,
        updated_at      = NOW()
    WHERE account_id = v_account;

    v_balance := v_balance + v_held;

    RETURN jsonb_build_object('ok', true, 'balance_after', v_balance);
END;
$$;


-- ----------------------------------------------------------------------------
-- credit_grant
-- Upsert account and add credits. Idempotent per idempotency_key (prevents
-- double-grant on webhook retries). Also used for the Polar topup flow.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_grant(
    p_account           TEXT,
    p_amount            BIGINT,
    p_source            TEXT DEFAULT 'grant',
    p_idempotency_key   TEXT DEFAULT NULL,
    p_order_id          TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_balance       BIGINT;
    v_entry_type    TEXT;
    v_rows          INTEGER;
    v_prior         RECORD;
BEGIN
    IF p_account IS NULL OR btrim(p_account) = '' OR p_amount IS NULL OR p_amount <= 0 THEN
        RAISE EXCEPTION 'credit_grant requires an account and positive amount'
            USING ERRCODE = '22023';
    END IF;

    v_entry_type := CASE WHEN p_source IN ('topup', 'polar') THEN 'topup' ELSE 'grant' END;

    -- Upsert account: create if missing, add credits if existing
    INSERT INTO public.credit_accounts (
        account_id, plan, balance_credits, lifetime_granted, updated_at
    )
    VALUES (p_account, 'free', p_amount, p_amount, NOW())
    ON CONFLICT (account_id) DO UPDATE
    SET balance_credits  = public.credit_accounts.balance_credits  + p_amount,
        lifetime_granted = public.credit_accounts.lifetime_granted + p_amount,
        updated_at       = NOW();

    SELECT balance_credits INTO v_balance
    FROM public.credit_accounts WHERE account_id = p_account;

    -- Insert ledger entry (idempotent by idempotency_key when provided)
    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO public.credit_ledger (
            account_id, entry_type, amount_credits, source, order_id,
            idempotency_key, balance_after
        )
        VALUES (
            p_account, v_entry_type, p_amount, p_source, p_order_id,
            p_idempotency_key, v_balance
        )
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING;

        GET DIAGNOSTICS v_rows = ROW_COUNT;

        IF v_rows = 0 THEN
            -- A key proves only that some grant won. Bind the replay to that
            -- original entitlement before reporting success. A conflicting
            -- replay raises, rolling back the upsert in this transaction.
            SELECT account_id, entry_type, amount_credits, source, order_id
            INTO v_prior
            FROM public.credit_ledger
            WHERE idempotency_key = p_idempotency_key
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'credit_grant idempotency ledger row missing'
                    USING ERRCODE = '23505';
            END IF;
            IF v_prior.account_id IS DISTINCT FROM p_account
               OR v_prior.entry_type IS DISTINCT FROM v_entry_type
               OR v_prior.amount_credits IS DISTINCT FROM p_amount
               OR v_prior.source IS DISTINCT FROM p_source
               OR v_prior.order_id IS DISTINCT FROM p_order_id THEN
                RAISE EXCEPTION 'credit_grant idempotency key conflicts with original grant'
                    USING ERRCODE = '23505';
            END IF;

            -- Idempotent duplicate: roll back the balance increment above.
            -- We already incremented in the upsert, so reverse it.
            UPDATE public.credit_accounts
            SET balance_credits  = balance_credits  - p_amount,
                lifetime_granted = lifetime_granted - p_amount,
                updated_at       = NOW()
            WHERE account_id = p_account;

            SELECT balance_credits INTO v_balance
            FROM public.credit_accounts WHERE account_id = p_account;

            RETURN jsonb_build_object(
                'ok',          true,
                'idempotent',  true,
                'balance_after', v_balance
            );
        END IF;
    ELSE
        -- No idempotency key: always insert a positive grant.
        INSERT INTO public.credit_ledger (
            account_id, entry_type, amount_credits, source, order_id, balance_after
        )
        VALUES (p_account, v_entry_type, p_amount, p_source, p_order_id, v_balance);
    END IF;

    RETURN jsonb_build_object('ok', true, 'balance_after', v_balance);
END;
$$;

-- SECURITY DEFINER bypasses RLS. Only the backend service principal may move
-- credits; a public/anon RPC caller must not choose an arbitrary account/hold.
REVOKE EXECUTE ON FUNCTION public.credit_reserve(TEXT, BIGINT, TEXT, TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.credit_reserve(TEXT, BIGINT, TEXT, TEXT, TEXT)
    TO service_role;
REVOKE EXECUTE ON FUNCTION public.credit_commit(TEXT, BIGINT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.credit_commit(TEXT, BIGINT)
    TO service_role;
REVOKE EXECUTE ON FUNCTION public.credit_release(TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.credit_release(TEXT, TEXT)
    TO service_role;
REVOKE EXECUTE ON FUNCTION public.credit_grant(TEXT, BIGINT, TEXT, TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.credit_grant(TEXT, BIGINT, TEXT, TEXT, TEXT)
    TO service_role;

COMMIT;
