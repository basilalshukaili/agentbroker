\set ON_ERROR_STOP on

DELETE FROM public.credit_ledger WHERE account_id LIKE 'tm_terminal_%';
DELETE FROM public.credit_accounts WHERE account_id LIKE 'tm_terminal_%';
INSERT INTO public.credit_accounts(account_id, balance_credits, lifetime_granted)
SELECT 'tm_terminal_' || suffix, 100, 100
FROM (VALUES ('numeric'), ('release'), ('retry'), ('other'),
             ('race_commit'), ('race_release'), ('legacy'),
             ('hold_a'), ('hold_b'), ('hold_same')) AS accounts(suffix);
INSERT INTO public.credit_ledger(account_id, entry_type, amount_credits,
                                 hold_id, balance_after)
VALUES ('tm_terminal_legacy', 'hold', -10, 'tm_terminal_legacy_hold', 90),
       ('tm_terminal_legacy', 'commit', 0, 'tm_terminal_legacy_hold', 90);
UPDATE public.credit_accounts SET balance_credits = 90
WHERE account_id = 'tm_terminal_legacy';

SET ROLE service_role;
DO $$
DECLARE
    r jsonb;
BEGIN
    r := public.credit_reserve('tm_terminal_numeric', -10, 'tm_terminal_negative');
    IF r->>'reason_code' IS DISTINCT FROM 'invalid_reservation' THEN
        RAISE EXCEPTION 'negative reserve accepted: %', r;
    END IF;
    r := public.credit_reserve('tm_terminal_numeric', 10, 'tm_terminal_hold', 'call', 'one');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'reserve failed: %', r; END IF;
    r := public.credit_commit('tm_terminal_hold', -5);
    IF r->>'reason_code' IS DISTINCT FROM 'invalid_actual' THEN
        RAISE EXCEPTION 'negative actual accepted: %', r;
    END IF;
    r := public.credit_commit('tm_terminal_hold', 11);
    IF r->>'reason_code' IS DISTINCT FROM 'invalid_actual' THEN
        RAISE EXCEPTION 'over-held actual accepted: %', r;
    END IF;
    r := public.credit_commit('tm_terminal_hold', 5);
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'partial commit failed: %', r; END IF;
    r := public.credit_commit('tm_terminal_hold', 5);
    IF r->>'idempotent' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'exact commit replay failed: %', r; END IF;
    r := public.credit_commit('tm_terminal_hold', 4);
    IF r->>'reason_code' IS DISTINCT FROM 'commit_conflict' THEN
        RAISE EXCEPTION 'mismatched commit replay accepted: %', r;
    END IF;
    r := public.credit_release('tm_terminal_hold');
    IF r->>'reason_code' IS DISTINCT FROM 'hold_committed' THEN
        RAISE EXCEPTION 'release after commit accepted: %', r;
    END IF;
    r := public.credit_reserve('tm_terminal_numeric', 10, 'tm_terminal_hold', 'call', 'one');
    IF r->>'reason_code' IS DISTINCT FROM 'hold_finalized' THEN
        RAISE EXCEPTION 'finalized hold replay accepted: %', r;
    END IF;
    r := public.credit_reserve('tm_terminal_release', 10, 'tm_terminal_release_hold');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'release fixture reserve failed: %', r; END IF;
    r := public.credit_release('tm_terminal_release_hold', 'tool_failed');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'release failed: %', r; END IF;
    r := public.credit_release('tm_terminal_release_hold', 'tool_failed');
    IF r->>'idempotent' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'exact release replay failed: %', r; END IF;
    r := public.credit_release('tm_terminal_release_hold', 'other_reason');
    IF r->>'reason_code' IS DISTINCT FROM 'release_conflict' THEN
        RAISE EXCEPTION 'release reason mismatch accepted: %', r;
    END IF;
    r := public.credit_commit('tm_terminal_release_hold', 10);
    IF r->>'reason_code' IS DISTINCT FROM 'hold_released' THEN
        RAISE EXCEPTION 'commit after release accepted: %', r;
    END IF;

    r := public.credit_reserve('tm_terminal_retry', 80, 'tm_terminal_retry_hold', 'call', 'two');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'retry fixture reserve failed: %', r; END IF;
    r := public.credit_reserve('tm_terminal_retry', 80, 'tm_terminal_retry_hold', 'call', 'two');
    IF r->>'idempotent' IS DISTINCT FROM 'true' THEN
        RAISE EXCEPTION 'exact reserve replay below balance failed: %', r;
    END IF;
    r := public.credit_reserve('tm_terminal_retry', 80, 'tm_terminal_retry_hold', 'call', 'different');
    IF r->>'reason_code' IS DISTINCT FROM 'hold_conflict' THEN
        RAISE EXCEPTION 'operation mismatch accepted: %', r;
    END IF;
    r := public.credit_reserve('tm_terminal_other', 80, 'tm_terminal_retry_hold', 'call', 'two');
    IF r->>'reason_code' IS DISTINCT FROM 'hold_conflict' THEN
        RAISE EXCEPTION 'account mismatch accepted: %', r;
    END IF;

    r := public.credit_commit('tm_terminal_legacy_hold', 10);
    IF r->>'reason_code' IS DISTINCT FROM 'commit_conflict' THEN
        RAISE EXCEPTION 'historical NULL actual replay accepted: %', r;
    END IF;
    r := public.credit_release('tm_terminal_legacy_hold');
    IF r->>'reason_code' IS DISTINCT FROM 'hold_committed' THEN
        RAISE EXCEPTION 'historical commit released: %', r;
    END IF;

    r := public.credit_reserve('tm_terminal_race_commit', 10, 'tm_terminal_race_commit_hold');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'commit race fixture failed: %', r; END IF;
    r := public.credit_reserve('tm_terminal_race_release', 10, 'tm_terminal_race_release_hold');
    IF r->>'ok' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'release race fixture failed: %', r; END IF;
END $$;
RESET ROLE;

DO $$
DECLARE balance bigint; spent bigint;
BEGIN
    SELECT balance_credits, lifetime_spent INTO balance, spent
    FROM public.credit_accounts WHERE account_id = 'tm_terminal_numeric';
    IF balance <> 95 OR spent <> 5 THEN
        RAISE EXCEPTION 'numeric account mismatch: balance %, spent %', balance, spent;
    END IF;
END $$;

SELECT 'serial billing contract PASS';
