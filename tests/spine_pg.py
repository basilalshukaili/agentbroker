"""A disposable PostgreSQL that looks like the spine, for migrations that touch `usage_events`.

`start_spine()` runs the postgres image that is already on the machine in a throwaway container (random loopback
port, random passwords that are never printed, `--rm`, removed again on exit). It never pulls an image and never
touches any real database. It builds the roles, the default privileges and the `usage_events` table the way the
live spine has them - measured from the live catalog on 2026-10-04 (names, types, grants; no row content) - and
then applies the REAL migration files 009 and 010 as `spine_owner`, so the state the new migration starts from is
the state produced by the migrations that produced the live one, not a hand-written imitation of it.

Two databases are made in the one container: `spine_before` (everything up to 010) and `spine_after` (the same,
then 013 applied twice). A test that needs "the database has not been migrated yet" and one that needs "it has"
therefore run side by side.

Opt-in (SPINE_PG_TESTS=1): each module starts a postgres container, and on a busy machine that takes minutes. The
fast suite stays fast; release and Node B runs set the flag. A skip always says why.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPINE_DIR = ROOT / "migrations" / "spine"
# The spine runs PostgreSQL 17; the proof image is 18.6 (the newest present on the development machine). Another
# image that is already local can be named to check the same migration on another major version.
IMAGE = os.environ.get("SPINE_PG_IMAGE", "postgres:18.6-bookworm")

CLUSTER_SQL = """
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator login noinherit password '{authenticator_password}';
create role spine_owner login bypassrls password '{owner_password}';
grant anon, authenticated, service_role to authenticator;
"""

# What the live spine's pg_default_acl says for objects created by spine_owner in schema public (measured):
# functions: anon, authenticated, service_role, spine_owner may EXECUTE; tables: service_role and spine_owner
# only (anon and authenticated get NOTHING on a new table); sequences: all four.
DATABASE_SQL = """
alter schema public owner to spine_owner;
grant usage on schema public to anon, authenticated, service_role;
alter default privileges for role spine_owner in schema public
    grant execute on functions to anon, authenticated, service_role, spine_owner;
alter default privileges for role spine_owner in schema public
    grant all on tables to service_role, spine_owner;
alter default privileges for role spine_owner in schema public
    grant all on sequences to anon, authenticated, service_role, spine_owner;
"""

# The live table, column for column (types, nullability, defaults as measured), plus its three indexes, RLS on and no
# policy. The three compliance tables are stand-ins: migration 009 revokes/alters them and its functions name them.
# plpgsql does not look inside a body until it runs, but a LANGUAGE sql function (consent_optouts_hydrate) is checked
# when it is created, so consent_optouts carries the columns that one reads; nothing here runs those functions.
BASELINE_SQL = """
create table public.usage_events (
    id bigserial primary key,
    ts timestamptz not null default now(),
    tool text not null,
    args_hash text,
    ip_hash text,
    user_agent text,
    key_id text,
    session_kind text not null,
    method text
);
create index idx_usage_events_ts on public.usage_events (ts);
create index idx_usage_events_session_kind on public.usage_events (session_kind);
alter table public.usage_events enable row level security;
create table public.compliance_audit (placeholder int);
create table public.pending_keys (placeholder int);
create table public.consent_optouts (id bigserial primary key, recipient_id text, channel text, use_case text,
    revocation_method text, source text, created_at timestamptz default now());

-- the original seven-field writer (migration 003), as the live spine has it
create or replace function public.usage_events_insert(
    p_tool text, p_args_hash text, p_ip_hash text, p_user_agent text, p_key_id text, p_session_kind text,
    p_method text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_id bigint;
begin
    if p_session_kind not in ('crawler', 'anon_agent', 'verified_agent_key', 'verified_human_key') then
        raise exception 'usage_events_insert: invalid session_kind %', p_session_kind using errcode = '22023';
    end if;
    insert into usage_events (ts, tool, args_hash, ip_hash, user_agent, key_id, session_kind, method)
    values (now(), left(p_tool, 128), p_args_hash, p_ip_hash, left(p_user_agent, 512), left(p_key_id, 64),
            p_session_kind, left(p_method, 64))
    returning id into v_id;
    return jsonb_build_object('id', v_id);
end $$;
revoke all on function public.usage_events_insert(text, text, text, text, text, text, text) from public;
grant execute on function public.usage_events_insert(text, text, text, text, text, text, text) to anon, service_role;
"""


def docker_unavailable_reason() -> str:
    """Why the database-backed tests will not run here ('' when they will)."""
    if os.environ.get("SPINE_PG_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return "database-backed spine tests are opt-in: set SPINE_PG_TESTS=1 (starts a throwaway postgres container)"
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return "asyncpg is not installed"
    exe = shutil.which("docker")
    if not exe:
        return "docker is not installed"
    try:
        p = subprocess.run([exe, "image", "inspect", IMAGE], capture_output=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return f"docker is not usable ({type(exc).__name__})"
    if p.returncode != 0:
        return f"the {IMAGE} image is not present locally (these tests never pull images)"
    return ""


class Spine:
    """DSNs for the throwaway cluster. `owner(db)` connects as spine_owner (what scripts/apply_sql.py does),
    `admin(db)` as the superuser (assertions and aging rows only; never used by the code under test)."""

    def __init__(self, port: str, admin_password: str, owner_password: str) -> None:
        self.port, self._admin_pw, self._owner_pw = port, admin_password, owner_password

    def admin(self, db: str = "postgres") -> str:
        return f"postgresql://postgres:{self._admin_pw}@127.0.0.1:{self.port}/{db}"

    def owner(self, db: str) -> str:
        return f"postgresql://spine_owner:{self._owner_pw}@127.0.0.1:{self.port}/{db}"

    def clone_before(self, name: str) -> str:
        """A new database that is a copy of `spine_before` (everything through 010, 013 NOT applied), for a test that
        migrates it. The template must have no open connection; every helper here closes its own."""
        run_sql_sync(self.admin(), f"create database {name} template spine_before owner spine_owner")
        return name


async def run_sql(dsn: str, sql: str):
    """Run a (multi-statement) script in one implicit transaction, like apply_sql.py does."""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.execute(sql)
    finally:
        await conn.close()


def run_sql_sync(dsn: str, sql: str):
    return asyncio.run(run_sql(dsn, sql))


async def query(dsn: str, sql: str, *args):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def load_script(name: str):
    """Import scripts/<name>.py (the scripts directory is not a package)."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def migration(name: str) -> str:
    return (SPINE_DIR / name).read_text(encoding="utf-8")


MIGRATION_009 = "009_keyholder_outcome_logging_and_compliance_rpcs.sql"
MIGRATION_010 = "010_usage_events_notification_outcome.sql"
MIGRATION_013 = "013_usage_events_door_columns.sql"


@contextlib.contextmanager
def start_spine():
    """Yield a `Spine` with two databases: `spine_before` (through 010) and `spine_after` (013 applied twice)."""
    import asyncpg  # noqa: F401
    admin_pw, owner_pw, auth_pw = (secrets.token_hex(16) for _ in range(3))
    name = f"ab-spine-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={admin_pw}",
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
        spine = Spine(port, admin_pw, owner_pw)

        async def _setup():
            import asyncpg
            for _ in range(300):      # a busy machine (other sessions' containers) can take minutes
                try:
                    c = await asyncpg.connect(spine.admin(), timeout=3)
                    break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.5)
            else:
                raise AssertionError("the throwaway database never accepted a connection")
            try:
                await c.execute(CLUSTER_SQL.format(authenticator_password=auth_pw, owner_password=owner_pw))
                for db in ("spine_before", "spine_after"):
                    await c.execute(f"create database {db} owner spine_owner")
            finally:
                await c.close()
            for db in ("spine_before", "spine_after"):
                await run_sql(spine.admin(db), DATABASE_SQL)
                # from here on everything is created by spine_owner, as apply_sql.py does on the real spine
                await run_sql(spine.owner(db), BASELINE_SQL)
                await run_sql(spine.owner(db), migration(MIGRATION_009))
                await run_sql(spine.owner(db), migration(MIGRATION_010))
            await run_sql(spine.owner("spine_after"), migration(MIGRATION_013))
            await run_sql(spine.owner("spine_after"), migration(MIGRATION_013))      # idempotent
        asyncio.run(_setup())
        yield spine
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


class PgRpc:
    """Drop-in for storage.supabase_client.rpc: named-argument function calls as the `anon` role, the way
    PostgREST makes them. A database error becomes the same RuntimeError text the real client raises, including
    PGRST202 for a function that does not exist, so the production fallback logic runs unmodified."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.calls: list = []

    async def __call__(self, fn: str, payload: dict):
        import asyncpg
        import json
        self.calls.append(fn)
        names = list(payload)
        args = ", ".join(f"{k} := ${i + 1}" for i, k in enumerate(names))
        values = [v for v in payload.values()]
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute("set role anon")
            try:
                row = await conn.fetchval(f"select public.{fn}({args})", *values)
            except asyncpg.UndefinedFunctionError as exc:
                raise RuntimeError(f"rpc({fn!r}) failed: HTTP 404 body={{\"code\":\"PGRST202\",\"message\":\"{exc}\"}}") from exc
            except asyncpg.PostgresError as exc:
                raise RuntimeError(f"rpc({fn!r}) failed: HTTP 400 body={exc}") from exc
        finally:
            await conn.close()
        return json.loads(row) if isinstance(row, str) else row
