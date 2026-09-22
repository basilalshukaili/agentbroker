"""Disposable PostgreSQL acceptance for credit_grant's money-moving contract.

Run explicitly with TM_CREDIT_SQL_TEST_DISPOSABLE=yes and a password-free
TM_CREDIT_SQL_TEST_DSN pointing at a loopback, non-5432 tm_credit_test_* DB.
This file never selects a production target or starts a database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse


MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "credits_billing.sql"
SIGNATURE = "public.credit_grant(text,bigint,text,text,text)"


def _dsn() -> str:
    dsn = os.getenv("TM_CREDIT_SQL_TEST_DSN", "")
    parsed = urlparse(dsn)
    if (
        os.getenv("TM_CREDIT_SQL_TEST_DISPOSABLE") != "yes"
        or parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or not parsed.port
        or parsed.port == 5432
        or not parsed.path.lstrip("/").startswith("tm_credit_test_")
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Refusing SQL test: require disposable loopback tm_credit_test_* DB on a non-default port")
    return dsn


def _command(dsn: str, sql: str | None = None, file: Path | None = None) -> list[str]:
    args = ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-d", dsn]
    if sql is not None:
        args += ["-c", sql]
    elif file is not None:
        args += ["-f", str(file)]
    return args


def _run(dsn: str, sql: str | None = None, file: Path | None = None, error: str | None = None) -> str:
    result = subprocess.run(_command(dsn, sql, file), capture_output=True, text=True, timeout=25)
    if error is None:
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    assert result.returncode != 0 and error in result.stderr, (result.stdout, result.stderr)
    return result.stderr


def _grant(dsn: str, account: str, amount: str, source: str, key: str, order: str,
           error: str | None = None) -> dict | None:
    statement = (
        "SELECT public.credit_grant("
        f"'{account}', {amount}, '{source}', '{key}', '{order}')"
    )
    output = _run(dsn, statement, error=error)
    return None if error else json.loads(output)


def _balance(dsn: str, account: str) -> tuple[int, int] | None:
    output = _run(
        dsn,
        "SELECT balance_credits || ',' || lifetime_granted "
        f"FROM public.credit_accounts WHERE account_id = '{account}'",
    )
    return tuple(map(int, output.split(","))) if output else None


def _ledger_count(dsn: str, key: str) -> int:
    return int(_run(dsn, f"SELECT count(*) FROM public.credit_ledger WHERE idempotency_key = '{key}'"))


def _wait_for_uncommitted_grant(dsn: str) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        active = _run(
            dsn,
            "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
            "AND state = 'active' AND query LIKE '%pg_sleep(2)%'",
        )
        if int(active) > 0:
            return
        time.sleep(0.05)
    raise AssertionError("concurrent grant never reached its held transaction")


def _concurrent_pair(dsn: str, suffix: str, same_entitlement: bool) -> None:
    key = f"tm_credit_{suffix}"
    first = f"tm_credit_account_{suffix}_a"
    second = first if same_entitlement else f"tm_credit_account_{suffix}_b"
    first_sql = (
        "BEGIN; "
        f"SELECT public.credit_grant('{first}', 100, 'polar', '{key}', '{key}'); "
        "SELECT pg_sleep(2); COMMIT;"
    )
    proc = subprocess.Popen(_command(dsn, first_sql), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for_uncommitted_grant(dsn)
        _grant(dsn, second, "100", "polar", key, key,
               error=None if same_entitlement else "23505")
        out, err = proc.communicate(timeout=8)
        assert proc.returncode == 0, (out, err)
        assert _balance(dsn, first) == (100, 100)
        assert _balance(dsn, second) == ((100, 100) if same_entitlement else None)
        assert _ledger_count(dsn, key) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)


def main() -> None:
    dsn = _dsn()
    # These synthetic roles are local to the disposable PostgreSQL cluster.
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
    # Seed an inherited PUBLIC privilege, then prove rerun removes it.
    _run(dsn, f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO PUBLIC")
    _run(dsn, file=MIGRATION)
    for role, expected in (("anon", "f"), ("authenticated", "f"), ("service_role", "t")):
        actual = _run(dsn, f"SELECT has_function_privilege('{role}', '{SIGNATURE}', 'EXECUTE')")
        assert actual == expected, (role, actual)
    _run(dsn, "SET ROLE anon; SELECT public.credit_grant('tm_denied', 100)", error="42501")
    _run(dsn, "SET ROLE authenticated; SELECT public.credit_grant('tm_denied', 100)", error="42501")
    service_key = f"tm_credit_service_{uuid.uuid4().hex[:12]}"
    service_output = _run(
        dsn,
        "SET ROLE service_role; "
        f"SELECT public.credit_grant('{service_key}', 1, 'grant', '{service_key}', NULL)",
    )
    assert json.loads(service_output.splitlines()[-1])["ok"] is True

    suffix = uuid.uuid4().hex[:12]
    key = f"tm_credit_{suffix}"
    account = f"tm_credit_account_{suffix}"
    assert _grant(dsn, account, "100", "polar", key, key)["ok"] is True
    assert _grant(dsn, account, "100", "polar", key, key)["idempotent"] is True
    assert _balance(dsn, account) == (100, 100)
    assert _ledger_count(dsn, key) == 1
    for changed in (
        (account + "_other", "100", "polar", key),
        (account, "101", "polar", key),
        (account, "100", "grant", key),
        (account, "100", "polar", key + "_other"),
    ):
        _grant(dsn, changed[0], changed[1], changed[2], key, changed[3], error="23505")
    for amount in ("0", "-1", "NULL"):
        _grant(dsn, account, amount, "polar", key + "_invalid", key, error="22023")
    assert _balance(dsn, account) == (100, 100)
    assert _balance(dsn, account + "_other") is None
    assert _ledger_count(dsn, key) == 1

    _concurrent_pair(dsn, uuid.uuid4().hex[:12], same_entitlement=True)
    _concurrent_pair(dsn, uuid.uuid4().hex[:12], same_entitlement=False)
    print("credit_grant SQL acceptance PASS: exact replay, mismatches, positive amount, roles, concurrent retries")


if __name__ == "__main__":
    main()
