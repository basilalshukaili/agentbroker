"""A disposable PostgreSQL for the OAuth tests - and an `rpc` that talks to it the way PostgREST would.

`start_postgres()` runs the postgres image that is already on the machine in a throwaway container: random
loopback port, random password that is never printed, `--rm`, removed again on exit. It never pulls an image
and never touches any real database. `docker_unavailable_reason()` says why it cannot run, so a test can skip
with the reason instead of passing silently.

`PgRpc` replaces `storage.supabase_client.rpc`: PostgREST turns `POST /rpc/<fn>` with a JSON body into a call
of `<fn>` with named arguments, as the `anon` role. This does the same over asyncpg, so the production
`SpineStore` code runs unmodified against the real SQL - only the HTTP hop is replaced.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "spine" / "011_oauth_connect.sql"
IMAGE = "postgres:18.6-bookworm"

PREPARE_SQL = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin; end if;
end $$;
alter default privileges in schema public grant execute on functions to anon, authenticated, public;
grant usage on schema public to anon, authenticated, service_role;
create table if not exists public.credit_accounts (
    account_id text primary key, customer_id text, email text,
    plan text not null default 'free', balance_credits bigint not null default 0,
    updated_at timestamptz not null default now());
"""


def docker_unavailable_reason() -> str:
    """Why the database-backed tests will not run here ('' when they will).

    They are OPT-IN (OAUTH_PG_TESTS=1): each module starts a postgres container, and on a busy machine that
    takes minutes. The fast suite stays fast; release and Node B runs set the flag. A skip always says so."""
    import os
    if os.environ.get("OAUTH_PG_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return "database-backed OAuth tests are opt-in: set OAUTH_PG_TESTS=1 (starts a throwaway postgres container)"
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return "asyncpg is not installed"
    exe = shutil.which("docker")
    if not exe:
        return "docker is not installed"
    try:
        p = subprocess.run([exe, "image", "inspect", IMAGE], capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return f"docker is not usable ({type(exc).__name__})"
    if p.returncode != 0:
        return f"the {IMAGE} image is not present locally (these tests never pull images)"
    return ""


@contextlib.contextmanager
def start_postgres():
    """Yield a superuser DSN for a fresh database with the migration applied (twice - it is idempotent)."""
    import asyncpg
    password = secrets.token_hex(16)
    name = f"ab-oauth-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}",
         "-p", "127.0.0.1::5432", "--memory", "384m", IMAGE],
        capture_output=True, encoding="utf-8", timeout=120)
    if run.returncode != 0:
        raise RuntimeError("could not start the throwaway database container")
    try:
        port = None
        for _ in range(20):
            out = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True,
                                 encoding="utf-8", timeout=30).stdout.strip().splitlines()
            if out:
                port = out[0].rsplit(":", 1)[1]
                break
            time.sleep(0.5)
        assert port, "the container published no port"
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"

        async def _setup():
            for _ in range(300):   # a busy machine (other sessions' containers) can take minutes
                try:
                    c = await asyncpg.connect(dsn, timeout=3)
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.5)
            else:
                raise AssertionError("the throwaway database never accepted a connection")
            try:
                await c.execute(PREPARE_SQL)
                sql = MIGRATION.read_text(encoding="utf-8")
                await c.execute(sql)
                await c.execute(sql)
            finally:
                await c.close()
        asyncio.run(_setup())
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


class PgRpc:
    """Drop-in for storage.supabase_client.rpc: named-argument function calls as the `anon` role."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    async def __call__(self, fn: str, payload: dict):
        import asyncpg
        names = list(payload)
        args = ", ".join(f"{k} := ${i + 1}" for i, k in enumerate(names))
        values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in payload.values()]
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute("set role anon")
            try:
                row = await conn.fetchval(f"select public.{fn}({args})", *values)
            except asyncpg.PostgresError as exc:
                raise RuntimeError(f"rpc({fn!r}) failed: HTTP 400 body={exc}") from exc
        finally:
            await conn.close()
        return json.loads(row) if isinstance(row, str) else row


async def query(dsn: str, sql: str, *args):
    """Superuser read/write for assertions and for aging rows (never used by the code under test)."""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()
