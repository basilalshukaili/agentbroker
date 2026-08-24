-- credits_billing.sql
-- AgentBroker credits billing schema (slice 2, 2026-08-24)
-- Apply via Supabase Management API or the SQL editor in the Supabase dashboard.
-- Safe to re-run: uses IF NOT EXISTS + ON CONFLICT DO NOTHING throughout.
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
-- Lock account row; if balance >= amount, decrement and insert hold (idempotent
-- on hold_id). Returns {ok, reason_code, balance_after}.
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
AS $$
DECLARE
    v_balance   BIGINT;
    v_rows      INTEGER;
BEGIN
    -- Lock the account row for the duration of this transaction
    SELECT balance_credits INTO v_balance
    FROM credit_accounts
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

    IF v_balance < p_amount THEN
        RETURN jsonb_build_object(
            'ok',          false,
            'reason_code', 'insufficient_credits',
            'balance',     v_balance,
            'balance_after', v_balance
        );
    END IF;

    -- Insert hold (idempotent: do nothing on duplicate hold_id+entry_type)
    INSERT INTO credit_ledger (
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
        UPDATE credit_accounts
        SET balance_credits = balance_credits - p_amount,
            updated_at      = NOW()
        WHERE account_id = p_account;

        RETURN jsonb_build_object(
            'ok',          true,
            'balance_after', v_balance - p_amount
        );
    ELSE
        -- Duplicate hold_id: already reserved (idempotent retry).
        -- Return ok=true so the caller can proceed without double-charging.
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
-- Updates lifetime_spent. Idempotent per hold_id (ON CONFLICT DO NOTHING on
-- the commit ledger entry).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_commit(
    p_hold_id   TEXT,
    p_actual    BIGINT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_held      BIGINT;
    v_account   TEXT;
    v_balance   BIGINT;
    v_diff      BIGINT;
    v_rows      INTEGER;
BEGIN
    -- Find the hold entry
    SELECT ABS(amount_credits), account_id
    INTO v_held, v_account
    FROM credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'hold';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_not_found');
    END IF;

    -- Lock the account row
    SELECT balance_credits INTO v_balance
    FROM credit_accounts
    WHERE account_id = v_account
    FOR UPDATE;

    -- Insert commit marker (idempotent: do nothing on duplicate)
    INSERT INTO credit_ledger (
        account_id, entry_type, amount_credits, hold_id, balance_after
    )
    VALUES (v_account, 'commit', 0, p_hold_id, v_balance)
    ON CONFLICT (hold_id, entry_type) WHERE hold_id IS NOT NULL DO NOTHING;

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows = 0 THEN
        -- Duplicate commit (idempotent retry) -- do not re-charge.
        RETURN jsonb_build_object(
            'ok',          true,
            'idempotent',  true,
            'balance_after', v_balance
        );
    END IF;

    -- First commit: handle partial refund and lifetime_spent
    v_diff := v_held - p_actual;

    IF v_diff > 0 THEN
        -- Actual cost < held: refund the difference
        INSERT INTO credit_ledger (
            account_id, entry_type, amount_credits, hold_id, reason_code, balance_after
        )
        VALUES (v_account, 'refund', v_diff, p_hold_id, 'commit_partial_refund', v_balance + v_diff);

        UPDATE credit_accounts
        SET balance_credits = balance_credits + v_diff,
            lifetime_spent  = lifetime_spent  + p_actual,
            updated_at      = NOW()
        WHERE account_id = v_account;

        v_balance := v_balance + v_diff;
    ELSE
        -- Actual == held: no refund needed
        UPDATE credit_accounts
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
-- Idempotent: a second release is a no-op (ON CONFLICT DO NOTHING on refund).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION credit_release(
    p_hold_id   TEXT,
    p_reason    TEXT DEFAULT 'release'
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_held      BIGINT;
    v_account   TEXT;
    v_balance   BIGINT;
    v_rows      INTEGER;
BEGIN
    SELECT ABS(amount_credits), account_id
    INTO v_held, v_account
    FROM credit_ledger
    WHERE hold_id = p_hold_id AND entry_type = 'hold';

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'reason_code', 'hold_not_found');
    END IF;

    SELECT balance_credits INTO v_balance
    FROM credit_accounts
    WHERE account_id = v_account
    FOR UPDATE;

    -- Insert refund entry (idempotent: do nothing on duplicate)
    INSERT INTO credit_ledger (
        account_id, entry_type, amount_credits, hold_id, reason_code, balance_after
    )
    VALUES (v_account, 'refund', v_held, p_hold_id, p_reason, v_balance + v_held)
    ON CONFLICT (hold_id, entry_type) WHERE hold_id IS NOT NULL DO NOTHING;

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows > 0 THEN
        -- First release: restore the balance
        UPDATE credit_accounts
        SET balance_credits = balance_credits + v_held,
            updated_at      = NOW()
        WHERE account_id = v_account;

        v_balance := v_balance + v_held;
    END IF;
    -- If v_rows = 0: already released (idempotent), return current balance

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
AS $$
DECLARE
    v_balance       BIGINT;
    v_entry_type    TEXT;
    v_rows          INTEGER;
BEGIN
    v_entry_type := CASE WHEN p_source IN ('topup', 'polar') THEN 'topup' ELSE 'grant' END;

    -- Upsert account: create if missing, add credits if existing
    INSERT INTO credit_accounts (
        account_id, plan, balance_credits, lifetime_granted, updated_at
    )
    VALUES (p_account, 'free', p_amount, p_amount, NOW())
    ON CONFLICT (account_id) DO UPDATE
    SET balance_credits  = credit_accounts.balance_credits  + p_amount,
        lifetime_granted = credit_accounts.lifetime_granted + p_amount,
        updated_at       = NOW();

    SELECT balance_credits INTO v_balance
    FROM credit_accounts WHERE account_id = p_account;

    -- Insert ledger entry (idempotent by idempotency_key when provided)
    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO credit_ledger (
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
            -- Idempotent duplicate: roll back the balance increment above.
            -- We already incremented in the upsert, so reverse it.
            UPDATE credit_accounts
            SET balance_credits  = balance_credits  - p_amount,
                lifetime_granted = lifetime_granted - p_amount,
                updated_at       = NOW()
            WHERE account_id = p_account;

            SELECT balance_credits INTO v_balance
            FROM credit_accounts WHERE account_id = p_account;

            RETURN jsonb_build_object(
                'ok',          true,
                'idempotent',  true,
                'balance_after', v_balance
            );
        END IF;
    ELSE
        -- No idempotency key: always insert (e.g. manual adjustments)
        INSERT INTO credit_ledger (
            account_id, entry_type, amount_credits, source, order_id, balance_after
        )
        VALUES (p_account, v_entry_type, p_amount, p_source, p_order_id, v_balance);
    END IF;

    RETURN jsonb_build_object('ok', true, 'balance_after', v_balance);
END;
$$;

COMMIT;
