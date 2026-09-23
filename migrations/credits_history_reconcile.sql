-- Backfill historical commit actuals AFTER reviewed credits_billing.sql.
-- Never run against a live target without an exact snapshot, independent review,
-- a bounded operator window and the preceding migration's privilege readback.
-- Idempotent for already-reconciled rows; aborts the entire transaction on
-- ambiguous history. No account balance or lifetime total is changed.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- Match the RPCs' account-then-ledger lock order and freeze billing history
-- while validating a complete snapshot and updating legacy commit metadata.
LOCK TABLE public.credit_accounts IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE public.credit_ledger IN SHARE ROW EXCLUSIVE MODE;

DO $reconcile$
DECLARE
    v_bad BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'credit_ledger'
          AND column_name = 'actual_credits'
    ) THEN
        RAISE EXCEPTION 'Apply reviewed credits_billing.sql before historical reconciliation';
    END IF;

    -- A duplicate or missing hold key would multiply a join or leave a commit
    -- without a unique original charge. Do not infer an amount in either case.
    SELECT count(*) INTO v_bad FROM (
        SELECT hold_id, entry_type
        FROM public.credit_ledger
        WHERE entry_type IN ('hold', 'commit', 'refund')
        GROUP BY hold_id, entry_type
        HAVING hold_id IS NULL OR count(*) <> 1
    ) bad_keys;
    IF v_bad <> 0 THEN
        RAISE EXCEPTION 'Ambiguous credit hold/commit/refund keys: %', v_bad;
    END IF;

    -- The only inferable outcomes are a full committed hold, or a commit with
    -- one bounded partial refund on the same account. Existing non-NULL actuals
    -- must already equal that derivation. Released holds are left untouched.
    SELECT count(*) INTO v_bad
    FROM public.credit_ledger c
    LEFT JOIN public.credit_ledger h
      ON h.hold_id = c.hold_id AND h.entry_type = 'hold'
    LEFT JOIN public.credit_ledger r
      ON r.hold_id = c.hold_id AND r.entry_type = 'refund'
    WHERE c.entry_type = 'commit'
      AND (
        h.id IS NULL OR h.account_id IS DISTINCT FROM c.account_id
        OR h.amount_credits >= 0 OR h.amount_credits = -9223372036854775808
        OR c.amount_credits <> 0
        OR (r.id IS NOT NULL AND (
            r.account_id IS DISTINCT FROM c.account_id
            OR r.reason_code IS DISTINCT FROM 'commit_partial_refund'
            OR r.amount_credits < 1
            OR r.amount_credits::numeric > -h.amount_credits::numeric
        ))
        OR (c.actual_credits IS NOT NULL AND c.actual_credits::numeric
            IS DISTINCT FROM (
                -h.amount_credits::numeric
                - COALESCE(r.amount_credits::numeric, 0)
            ))
      );
    IF v_bad <> 0 THEN
        RAISE EXCEPTION 'Ambiguous historical credit commits: %', v_bad;
    END IF;

    -- Verify the derivation against the independently maintained account
    -- lifetime totals, including accounts with no commits.
    WITH derived AS (
        SELECT c.account_id,
               sum(-h.amount_credits::numeric
                   - COALESCE(r.amount_credits::numeric, 0)) AS spent
        FROM public.credit_ledger c
        JOIN public.credit_ledger h
          ON h.hold_id = c.hold_id AND h.entry_type = 'hold'
        LEFT JOIN public.credit_ledger r
          ON r.hold_id = c.hold_id AND r.entry_type = 'refund'
        WHERE c.entry_type = 'commit'
        GROUP BY c.account_id
    )
    SELECT count(*) INTO v_bad
    FROM public.credit_accounts a
    LEFT JOIN derived d ON d.account_id = a.account_id
    WHERE a.lifetime_spent::numeric IS DISTINCT FROM COALESCE(d.spent, 0);
    IF v_bad <> 0 THEN
        RAISE EXCEPTION 'Credit lifetime_spent disagrees with ledger: % accounts', v_bad;
    END IF;

    UPDATE public.credit_ledger c
    SET actual_credits = (
        -h.amount_credits::numeric - COALESCE(r.amount_credits::numeric, 0)
    )::BIGINT
    FROM public.credit_ledger h
    LEFT JOIN public.credit_ledger r
      ON r.hold_id = h.hold_id AND r.entry_type = 'refund'
    WHERE c.entry_type = 'commit' AND c.actual_credits IS NULL
      AND h.entry_type = 'hold' AND h.hold_id = c.hold_id;

    SELECT count(*) INTO v_bad FROM public.credit_ledger
    WHERE entry_type = 'commit' AND actual_credits IS NULL;
    IF v_bad <> 0 THEN
        RAISE EXCEPTION 'Credit commit metadata remains unreconciled: %', v_bad;
    END IF;
END
$reconcile$;

COMMIT;
