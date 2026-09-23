"""Disposable PostgreSQL acceptance for reserve/commit/release state transitions.

Run explicitly with TM_CREDIT_SQL_TEST_DISPOSABLE=yes and a password-free
TM_CREDIT_SQL_TEST_DSN for a loopback tm_credit_test_* DB on a non-default port.
The target database, if any, is never inferred from application settings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from check_credit_grant_sql import MIGRATION, _command, _dsn, _run, _wait_for_uncommitted_grant


FIXTURE = Path(__file__).with_name("credit_state_contract.sql")


def _account(dsn: str, suffix: str) -> tuple[int, int]:
    balance, spent = _run(
        dsn,
        "SELECT balance_credits || ',' || lifetime_spent "
        f"FROM public.credit_accounts WHERE account_id='tm_terminal_{suffix}'",
    ).split(",")
    return int(balance), int(spent)


def _compete(dsn: str, suffix: str, first_call: str, second_call: str,
             losing_reason: str, expected: tuple[int, int]) -> None:
    account = f"tm_terminal_{suffix}"
    first_sql = (
        "BEGIN; "
        f"SELECT account_id FROM public.credit_accounts WHERE account_id='{account}' FOR UPDATE; "
        "SELECT pg_sleep(2); SET ROLE service_role; "
        f"SELECT {first_call}->>'ok'; COMMIT;"
    )
    first = subprocess.Popen(
        _command(dsn, first_sql), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        # The first transaction has acquired the account lock before reaching
        # pg_sleep. The second RPC must wait and see its committed terminal row.
        _wait_for_uncommitted_grant(dsn)
        second = _run(dsn, f"SET ROLE service_role; SELECT {second_call}->>'reason_code'")
        out, err = first.communicate(timeout=8)
        assert first.returncode == 0, (out, err)
        assert "true" in out.splitlines(), out
        assert second.splitlines()[-1] == losing_reason, second
        assert _account(dsn, suffix) == expected
        commit_count = int(_run(
            dsn,
            "SELECT count(*) FROM public.credit_ledger "
            f"WHERE hold_id='tm_terminal_{suffix}_hold' AND entry_type='commit'",
        ))
        refund_count = int(_run(
            dsn,
            "SELECT count(*) FROM public.credit_ledger "
            f"WHERE hold_id='tm_terminal_{suffix}_hold' AND entry_type='refund'",
        ))
        assert (commit_count, refund_count) == (
            (1, 0) if suffix == "race_commit" else (0, 1)
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=3)


def main() -> None:
    dsn = _dsn()
    _run(dsn, """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            CREATE ROLE anon NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            CREATE ROLE authenticated NOLOGIN;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            CREATE ROLE service_role NOLOGIN;
          END IF;
        END $$;
    """)
    _run(dsn, file=MIGRATION)
    assert "serial billing contract PASS" in _run(dsn, file=FIXTURE)
    _compete(
        dsn, "race_commit",
        "public.credit_commit('tm_terminal_race_commit_hold', 10)",
        "public.credit_release('tm_terminal_race_commit_hold')",
        "hold_committed", (90, 10),
    )
    _compete(
        dsn, "race_release",
        "public.credit_release('tm_terminal_race_release_hold')",
        "public.credit_commit('tm_terminal_race_release_hold', 10)",
        "hold_released", (100, 0),
    )
    print("billing state SQL acceptance PASS: numeric/replay binding, both terminal races")


if __name__ == "__main__":
    main()
