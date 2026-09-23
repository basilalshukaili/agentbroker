"""Guarded disposable-PostgreSQL acceptance for historical credit metadata.

Requires TM_CREDIT_SQL_TEST_DISPOSABLE=yes and a loopback non-5432
tm_credit_test_* database. Never infers a target from application settings.
"""

from __future__ import annotations

from pathlib import Path

from check_credit_grant_sql import MIGRATION, _dsn, _run


RECONCILE = Path(__file__).resolve().parents[2] / "migrations" / "credits_history_reconcile.sql"


def main() -> None:
    dsn = _dsn()
    _run(dsn, """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            CREATE ROLE anon NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            CREATE ROLE authenticated NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
            CREATE ROLE service_role NOLOGIN;
          END IF;
        END $$;
    """)
    _run(dsn, file=MIGRATION)
    _run(dsn, """
        DELETE FROM public.credit_ledger WHERE account_id LIKE 'tm_history_%';
        DELETE FROM public.credit_accounts WHERE account_id LIKE 'tm_history_%';
        INSERT INTO public.credit_accounts
          (account_id, balance_credits, lifetime_granted, lifetime_spent)
        VALUES ('tm_history_full',90,100,10),
               ('tm_history_partial',94,100,6),
               ('tm_history_release',100,100,0),
               ('tm_history_bad',90,100,10);
        INSERT INTO public.credit_ledger
          (account_id,entry_type,amount_credits,hold_id,reason_code)
        VALUES
          ('tm_history_full','hold',-10,'tm_history_full_hold',NULL),
          ('tm_history_full','commit',0,'tm_history_full_hold',NULL),
          ('tm_history_partial','hold',-10,'tm_history_partial_hold',NULL),
          ('tm_history_partial','commit',0,'tm_history_partial_hold',NULL),
          ('tm_history_partial','refund',4,'tm_history_partial_hold','commit_partial_refund'),
          ('tm_history_release','hold',-10,'tm_history_release_hold',NULL),
          ('tm_history_release','refund',10,'tm_history_release_hold','release'),
          ('tm_history_bad','hold',-10,'tm_history_bad_hold',NULL),
          ('tm_history_bad','commit',0,'tm_history_bad_hold',NULL),
          ('tm_history_bad','refund',11,'tm_history_bad_hold','commit_partial_refund');
    """)

    _run(dsn, file=RECONCILE, error="Ambiguous historical credit commits")
    assert _run(dsn, """
        SELECT count(*) FROM public.credit_ledger
        WHERE account_id LIKE 'tm_history_%' AND entry_type='commit'
          AND actual_credits IS NULL
    """) == "3", "failed reconciliation must roll back every update"

    _run(dsn, """
        UPDATE public.credit_ledger SET amount_credits=4
        WHERE account_id='tm_history_bad' AND entry_type='refund';
        UPDATE public.credit_accounts SET balance_credits=94,lifetime_spent=6
        WHERE account_id='tm_history_bad';
    """)
    _run(dsn, file=RECONCILE)
    assert _run(dsn, """
        SELECT string_agg(account_id || ':' || actual_credits, ',' ORDER BY account_id)
        FROM public.credit_ledger
        WHERE account_id LIKE 'tm_history_%' AND entry_type='commit'
    """) == "tm_history_bad:6,tm_history_full:10,tm_history_partial:6"
    _run(dsn, file=RECONCILE)  # exact replay is idempotent
    assert _run(dsn, """
        SET ROLE service_role;
        SELECT public.credit_commit('tm_history_partial_hold',6)->>'idempotent';
    """).splitlines()[-1] == "true"
    assert _run(dsn, """
        SET ROLE service_role;
        SELECT public.credit_commit('tm_history_partial_hold',5)->>'reason_code';
    """).splitlines()[-1] == "commit_conflict"
    assert _run(dsn, """
        SET ROLE service_role;
        SELECT public.credit_release('tm_history_full_hold')->>'reason_code';
    """).splitlines()[-1] == "hold_committed"

    _run(dsn, """
        UPDATE public.credit_ledger SET actual_credits=9
        WHERE account_id='tm_history_full' AND entry_type='commit';
    """)
    _run(dsn, file=RECONCILE, error="Ambiguous historical credit commits")
    assert _run(dsn, """
        SELECT actual_credits FROM public.credit_ledger
        WHERE account_id='tm_history_full' AND entry_type='commit'
    """) == "9", "a mismatch must fail without rewriting an existing value"
    _run(dsn, """
        UPDATE public.credit_ledger SET actual_credits=10
        WHERE account_id='tm_history_full' AND entry_type='commit';
        UPDATE public.credit_accounts SET lifetime_spent=11
        WHERE account_id='tm_history_full';
    """)
    _run(dsn, file=RECONCILE, error="lifetime_spent disagrees with ledger")
    assert _run(dsn, """
        SELECT actual_credits FROM public.credit_ledger
        WHERE account_id='tm_history_full' AND entry_type='commit'
    """) == "10"
    _run(dsn, """
        UPDATE public.credit_accounts SET lifetime_spent=10
        WHERE account_id='tm_history_full';
    """)
    _run(dsn, file=RECONCILE)
    assert _run(dsn, """
        SELECT bool_and(NOT has_function_privilege('anon',p.oid,'EXECUTE')
                        AND has_function_privilege('service_role',p.oid,'EXECUTE'))
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname IN
          ('credit_grant','credit_reserve','credit_commit','credit_release')
    """) == "t", "backfill must preserve the four-RPC privilege boundary"
    print("historical credit SQL acceptance PASS: guarded rollback, exact derivation, replay and rerun")


if __name__ == "__main__":
    main()
