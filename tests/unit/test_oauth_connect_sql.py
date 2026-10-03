"""migrations/spine/011_oauth_connect.sql, run against a real PostgreSQL - not read, not mocked.

A migration that has only ever been read by its author is a hypothesis. This one is applied to a throwaway
database in a throwaway container (docker, the postgres image already on the machine, a random loopback port,
a random password that is never printed) and then driven the way the service drives it: as the `anon` role,
through its functions, with the tables closed. Nothing here touches the spine, the VPS or any real database.

The module is skipped, with the reason, when docker, the image or asyncpg is missing - a skipped test says so
in the summary; it does not pass silently.

What is pinned, in the order the service relies on it:
  * the file applies, and applies a second time (idempotent);
  * anon can call the functions and cannot touch a table; `authenticated` and `public` can do neither;
  * the sign-in lifecycle: create -> mail -> look-up (not spent) -> press -> poll -> complete -> code;
  * single use, enforced by the database where two requests can race: one authorization code, one magic-link
    press, one refresh rotation - ten simultaneous attempts, exactly one winner;
  * replaying a spent code revokes the refresh chain it started; replaying a rotated refresh token revokes the
    whole chain; a different client presenting a token revokes nothing;
  * no function ever returns a digest or a secret; no table ever holds a raw value the tests put in;
  * an email's paid account: the explicit link wins and cannot be overwritten, accounts created before the
    link existed are found by email digest, and a non-subscription account is never returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets

import pytest

asyncpg = pytest.importorskip("asyncpg", reason="asyncpg is not installed")

from tests import oauth_pg  # noqa: E402

_WHY_NOT = oauth_pg.docker_unavailable_reason()
pytestmark = pytest.mark.skipif(bool(_WHY_NOT), reason=_WHY_NOT or "ok")


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


CHALLENGE = "A" * 43


@pytest.fixture(scope="module")
def db():
    """A disposable PostgreSQL with the migration applied twice (idempotent). Yields its superuser DSN."""
    with oauth_pg.start_postgres() as dsn:
        yield dsn


def run(db, coro_fn):
    return asyncio.run(coro_fn())


class Conn:
    """A connection acting as one role, calling functions and returning parsed JSON."""

    def __init__(self, dsn, role="anon"):
        self.dsn, self.role, self.c = dsn, role, None

    async def __aenter__(self):
        self.c = await asyncpg.connect(self.dsn)
        if self.role:
            await self.c.execute(f"set role {self.role}")
        return self

    async def __aexit__(self, *a):
        await self.c.close()

    async def fn(self, name, *args):
        ph = ", ".join(f"${i + 1}" for i in range(len(args)))
        row = await self.c.fetchval(f"select public.{name}({ph})", *args)
        return json.loads(row) if isinstance(row, str) else row


async def _signin(a: Conn, rid=None, *, ttl=900, email="alice@example.org", magic=None, poll=None):
    """Create a request and mail a link for it. Returns the secrets a caller would hold."""
    rid = rid or secrets.token_urlsafe(16)
    magic = magic or secrets.token_hex(32)
    poll = poll or secrets.token_hex(32)
    r = await a.fn("oauth_request_create", rid, "https://claude.ai/client.json", "https://claude.ai/api/mcp/auth_callback",
                   CHALLENGE, "agentbroker.tools", "st-1", "https://api.hatchloop.dev/mcp", ttl)
    assert r == {"created": True}
    r = await a.fn("oauth_request_set_email", rid, h(poll), h(magic), h(email), "a***@example.org", 0, 5)
    assert r["ok"] is True, r
    return rid, magic, poll, email


# ---------------------------------------------------------------------------
# privileges
# ---------------------------------------------------------------------------

TABLES = ["oauth_clients", "oauth_requests", "oauth_codes", "oauth_refresh_tokens", "oauth_account_links"]


def test_anon_can_call_the_functions_and_cannot_touch_any_table(db):
    async def go():
        async with Conn(db, "anon") as a:
            assert (await a.fn("oauth_ready")) == {"ready": True, "schema": 1}
            for t in TABLES:
                for stmt in (f"select * from public.{t}", f"delete from public.{t}",
                             f"insert into public.{t} default values"):
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await a.c.execute(stmt)
    run(db, go)


def test_authenticated_and_public_cannot_execute_any_of_them(db):
    async def go():
        for role in ("authenticated",):
            async with Conn(db, role) as u:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await u.fn("oauth_ready")
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await u.fn("oauth_code_consume", "0" * 64)
        # a role that is a member of nothing: PUBLIC's grant was revoked
        c = await asyncpg.connect(db)
        try:
            await c.execute("do $$ begin if not exists (select 1 from pg_roles where rolname='nobody_t') then create role nobody_t nologin; end if; end $$; grant usage on schema public to nobody_t")
            await c.execute("set role nobody_t")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.fetchval("select public.oauth_ready()")
        finally:
            await c.close()
    run(db, go)


def test_every_function_in_the_migration_is_security_definer_with_a_pinned_search_path(db):
    async def go():
        c = await asyncpg.connect(db)
        try:
            rows = await c.fetch("""
                select p.proname, p.prosecdef, p.proconfig
                  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'public' and p.proname like 'oauth\\_%'""")
            assert len(rows) == 16, [r["proname"] for r in rows]
            for r in rows:
                assert r["prosecdef"], r["proname"]
                assert r["proconfig"] and any("search_path=public" in x for x in r["proconfig"]), r["proname"]
        finally:
            await c.close()
    run(db, go)


# ---------------------------------------------------------------------------
# the sign-in lifecycle
# ---------------------------------------------------------------------------

def test_a_signin_from_creation_to_a_spent_code(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, email = await _signin(a)

            # looking the link up does NOT spend it - a mail scanner opens links
            for _ in range(3):
                lk = await a.fn("oauth_request_lookup_magic", h(magic))
                assert lk["found"] and lk["status"] == "email_sent" and lk["request_id"] == rid
                assert lk["email_hint"] == "a***@example.org"

            # only the holder of the poll secret learns the state
            assert (await a.fn("oauth_request_poll", rid, h(poll)))["status"] == "email_sent"
            assert (await a.fn("oauth_request_poll", rid, h("wrong")))["status"] == "unknown"

            # not ready before the press
            assert (await a.fn("oauth_request_complete", rid, h(poll), h("c" * 8), 120))["reason"] == "not_ready"

            # the deliberate press
            d = await a.fn("oauth_request_decide", h(magic), True)
            assert d == {"ok": True, "request_id": rid, "status": "verified"}
            d2 = await a.fn("oauth_request_decide", h(magic), True)
            assert d2["ok"] is False and d2["reason"] == "already_used"

            assert (await a.fn("oauth_request_poll", rid, h(poll)))["status"] == "verified"

            # somebody who does not hold the poll secret cannot take the result
            code = secrets.token_urlsafe(32)
            assert (await a.fn("oauth_request_complete", rid, h("not-the-secret"), h(code), 120))["reason"] == "unknown"

            done = await a.fn("oauth_request_complete", rid, h(poll), h(code), 120)
            assert done["ok"] and done["outcome"] == "approved"
            assert done["redirect_uri"] == "https://claude.ai/api/mcp/auth_callback" and done["state"] == "st-1"
            # one delivery only
            assert (await a.fn("oauth_request_complete", rid, h(poll), h(secrets.token_urlsafe(32)), 120))["reason"] == "completed"

            c = await a.fn("oauth_code_consume", h(code))
            assert c["ok"] is True
            assert c["client_id"] == "https://claude.ai/client.json" and c["code_challenge"] == CHALLENGE
            assert c["email_hash"] == h(email) and c["resource"] == "https://api.hatchloop.dev/mcp"
            again = await a.fn("oauth_code_consume", h(code))
            assert again == {"ok": False, "reason": "reused"}
            assert (await a.fn("oauth_code_consume", h("never-issued")))["reason"] == "invalid"
    run(db, go)


def test_denying_is_delivered_once_and_never_yields_a_code(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)
            assert (await a.fn("oauth_request_decide", h(magic), False))["status"] == "denied"
            r = await a.fn("oauth_request_complete", rid, h(poll), h("x" * 4), 120)
            assert r["ok"] and r["outcome"] == "denied" and r["state"] == "st-1"
            assert (await a.fn("oauth_request_complete", rid, h(poll), h("x" * 4), 120))["reason"] == "completed"
            c = await asyncpg.connect(db)
            try:
                assert await c.fetchval("select count(*) from public.oauth_codes where request_id = $1", rid) == 0
            finally:
                await c.close()
    run(db, go)


def test_a_link_can_only_be_pressed_once_even_when_ten_press_at_the_same_moment(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)

        async def press():
            async with Conn(db) as b:
                return await b.fn("oauth_request_decide", h(magic), True)
        results = await asyncio.gather(*[press() for _ in range(10)])
        assert sum(1 for r in results if r["ok"]) == 1, results
    run(db, go)


def test_set_email_enforces_the_gap_the_ceiling_the_poll_secret_and_state(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)
            second = lambda m, p=poll, gap=0, mx=5: a.fn(  # noqa: E731
                "oauth_request_set_email", rid, h(p), h(m), h("alice@example.org"), "a***", gap, mx)
            # a different poll secret cannot hijack a request that already has one
            assert (await second(secrets.token_hex(32), p=secrets.token_hex(32)))["reason"] == "poll_mismatch"
            # the gap, enforced in the database
            assert (await second(secrets.token_hex(32), gap=300))["reason"] == "too_soon"
            # a resend replaces the link: the old one stops working
            new_magic = secrets.token_hex(32)
            assert (await second(new_magic))["ok"] is True
            assert (await a.fn("oauth_request_lookup_magic", h(magic)))["found"] is False
            assert (await a.fn("oauth_request_lookup_magic", h(new_magic)))["found"] is True
            # the ceiling
            for _ in range(3):
                assert (await second(secrets.token_hex(32)))["ok"] is True
            assert (await second(secrets.token_hex(32)))["reason"] == "too_many"
            # once pressed, no more mail for this sign-in
            last = secrets.token_hex(32)
            rid2, m2, p2, _ = await _signin(a)
            await a.fn("oauth_request_decide", h(m2), True)
            assert (await a.fn("oauth_request_set_email", rid2, h(p2), h(last), h("a@b.c"), "x", 0, 5))["reason"] == "bad_state"
            assert (await a.fn("oauth_request_set_email", "nonexistent-request-id", h(p2), h(last), h("a@b.c"), "x", 0, 5))["reason"] == "not_found"
    run(db, go)


def test_an_expired_signin_can_do_nothing(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)
            c = await asyncpg.connect(db)
            try:
                await c.execute("update public.oauth_requests set expires_at = now() - interval '1 second' where request_id = $1", rid)
            finally:
                await c.close()
            assert (await a.fn("oauth_request_get", rid))["status"] == "expired"
            assert (await a.fn("oauth_request_decide", h(magic), True))["reason"] == "expired"
            assert (await a.fn("oauth_request_poll", rid, h(poll)))["status"] == "expired"
            assert (await a.fn("oauth_request_complete", rid, h(poll), h("z" * 5), 120))["reason"] == "expired"
            assert (await a.fn("oauth_request_set_email", rid, h(poll), h(secrets.token_hex(32)), h("a@b.c"), "x", 0, 5))["reason"] == "expired"
    run(db, go)


def test_an_expired_code_is_invalid_and_does_not_count_as_reuse(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)
            await a.fn("oauth_request_decide", h(magic), True)
            code = secrets.token_urlsafe(32)
            await a.fn("oauth_request_complete", rid, h(poll), h(code), 120)
            c = await asyncpg.connect(db)
            try:
                await c.execute("update public.oauth_codes set expires_at = now() - interval '1 second' where code_hash = $1", h(code))
            finally:
                await c.close()
            assert (await a.fn("oauth_code_consume", h(code)))["reason"] == "invalid"
    run(db, go)


def test_ten_simultaneous_redemptions_of_one_code_have_one_winner(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, _ = await _signin(a)
            await a.fn("oauth_request_decide", h(magic), True)
            code = secrets.token_urlsafe(32)
            await a.fn("oauth_request_complete", rid, h(poll), h(code), 120)

        async def redeem():
            async with Conn(db) as b:
                return await b.fn("oauth_code_consume", h(code))
        results = await asyncio.gather(*[redeem() for _ in range(10)])
        assert sum(1 for r in results if r["ok"]) == 1, results
        assert all(r.get("reason") == "reused" for r in results if not r["ok"]), results
    run(db, go)


def test_replaying_a_spent_code_revokes_the_refresh_tokens_it_started(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, email = await _signin(a)
            await a.fn("oauth_request_decide", h(magic), True)
            code = secrets.token_urlsafe(32)
            await a.fn("oauth_request_complete", rid, h(poll), h(code), 120)
            assert (await a.fn("oauth_code_consume", h(code)))["ok"]
            rt = secrets.token_urlsafe(32)
            await a.fn("oauth_refresh_store", h(rt), rid, "https://claude.ai/client.json", h(email),
                       "agentbroker.tools", "https://api.hatchloop.dev/mcp", 2592000, 7776000)
            # the code comes back (the first redemption may have been the thief's)
            assert (await a.fn("oauth_code_consume", h(code)))["reason"] == "reused"
            r = await a.fn("oauth_refresh_rotate", h(rt), h(secrets.token_urlsafe(32)), "https://claude.ai/client.json", 2592000)
            assert r == {"ok": False, "reason": "invalid"}      # revoked
    run(db, go)


# ---------------------------------------------------------------------------
# refresh tokens
# ---------------------------------------------------------------------------

CLIENT = "https://claude.ai/client.json"
RES = "https://api.hatchloop.dev/mcp"


async def _store_refresh(a, rt, fam=None, email="bob@example.org", ttl=2592000, fam_ttl=7776000):
    fam = fam or secrets.token_urlsafe(16)
    await a.fn("oauth_refresh_store", h(rt), fam, CLIENT, h(email), "agentbroker.tools", RES, ttl, fam_ttl)
    return fam


def test_a_refresh_token_rotates_and_a_second_use_of_the_old_one_kills_the_chain(db):
    async def go():
        async with Conn(db) as a:
            rt1, rt2 = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            fam = await _store_refresh(a, rt1)
            r = await a.fn("oauth_refresh_rotate", h(rt1), h(rt2), CLIENT, 2592000)
            assert r["ok"] and r["family_id"] == fam and r["email_hash"] == h("bob@example.org")
            assert r["scope"] == "agentbroker.tools" and r["resource"] == RES
            # the successor works
            rt3 = secrets.token_urlsafe(32)
            assert (await a.fn("oauth_refresh_rotate", h(rt2), h(rt3), CLIENT, 2592000))["ok"]
            # the first one is presented again: reuse - and now NOTHING in the chain works
            assert (await a.fn("oauth_refresh_rotate", h(rt1), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["reason"] == "reuse"
            assert (await a.fn("oauth_refresh_rotate", h(rt3), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["reason"] == "invalid"
    run(db, go)


def test_another_client_presenting_a_token_is_refused_and_revokes_nothing(db):
    async def go():
        async with Conn(db) as a:
            rt = secrets.token_urlsafe(32)
            await _store_refresh(a, rt)
            r = await a.fn("oauth_refresh_rotate", h(rt), h(secrets.token_urlsafe(32)), "https://evil.example/c.json", 2592000)
            assert r["reason"] == "client_mismatch"
            assert (await a.fn("oauth_refresh_rotate", h(rt), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["ok"]
    run(db, go)


def test_an_expired_refresh_token_and_an_expired_family_are_refused(db):
    async def go():
        async with Conn(db) as a:
            rt = secrets.token_urlsafe(32)
            await _store_refresh(a, rt)
            c = await asyncpg.connect(db)
            try:
                await c.execute("update public.oauth_refresh_tokens set expires_at = now() - interval '1 second' where token_hash = $1", h(rt))
                assert (await a.fn("oauth_refresh_rotate", h(rt), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["reason"] == "expired"
                rt2 = secrets.token_urlsafe(32)
                await _store_refresh(a, rt2)
                await c.execute("update public.oauth_refresh_tokens set family_expires_at = now() - interval '1 second' where token_hash = $1", h(rt2))
                assert (await a.fn("oauth_refresh_rotate", h(rt2), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["reason"] == "expired"
            finally:
                await c.close()
    run(db, go)


def test_a_successor_never_outlives_its_family(db):
    async def go():
        async with Conn(db) as a:
            rt1, rt2 = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            await _store_refresh(a, rt1, fam_ttl=3600)
            assert (await a.fn("oauth_refresh_rotate", h(rt1), h(rt2), CLIENT, 7776000))["ok"]
            c = await asyncpg.connect(db)
            try:
                row = await c.fetchrow("select expires_at, family_expires_at from public.oauth_refresh_tokens where token_hash = $1", h(rt2))
                assert row["expires_at"] <= row["family_expires_at"]
            finally:
                await c.close()
    run(db, go)


def test_ten_simultaneous_refreshes_of_one_token_have_one_winner(db):
    async def go():
        async with Conn(db) as a:
            rt = secrets.token_urlsafe(32)
            await _store_refresh(a, rt)

        async def rot():
            async with Conn(db) as b:
                return await b.fn("oauth_refresh_rotate", h(rt), h(secrets.token_urlsafe(32)), CLIENT, 2592000)
        results = await asyncio.gather(*[rot() for _ in range(10)])
        assert sum(1 for r in results if r["ok"]) == 1, results
    run(db, go)


def test_revoking_a_refresh_token_revokes_its_chain_and_answers_the_same_for_a_stranger(db):
    async def go():
        async with Conn(db) as a:
            rt1, rt2 = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            await _store_refresh(a, rt1)
            await a.fn("oauth_refresh_rotate", h(rt1), h(rt2), CLIENT, 2592000)
            assert (await a.fn("oauth_refresh_revoke", h(rt2), "https://other.example/c.json")) == {"revoked": False}
            assert (await a.fn("oauth_refresh_revoke", h(rt2), CLIENT)) == {"revoked": True}
            assert (await a.fn("oauth_refresh_rotate", h(rt2), h(secrets.token_urlsafe(32)), CLIENT, 2592000))["reason"] == "invalid"
            assert (await a.fn("oauth_refresh_revoke", h("never-existed"), CLIENT)) == {"revoked": False}
    run(db, go)


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def test_client_registration_roundtrip_and_input_limits(db):
    async def go():
        async with Conn(db) as a:
            cid = "dcr_" + secrets.token_urlsafe(18)
            uris = ["https://claude.ai/api/mcp/auth_callback", "http://127.0.0.1/callback"]
            assert (await a.fn("oauth_client_register", cid, "My Assistant", json.dumps(uris), h("1.2.3.4"))) == {"stored": True}
            got = await a.fn("oauth_client_get", cid)
            assert got["found"] and got["client_name"] == "My Assistant" and got["redirect_uris"] == uris
            assert (await a.fn("oauth_client_get", "dcr_unknown_unknown_x")) == {"found": False}
            for bad_id in ("short", "has space and more than sixteen chars", "x" * 80, ""):
                with pytest.raises(asyncpg.PostgresError):
                    await a.fn("oauth_client_register", bad_id, "n", json.dumps(uris), None)
            for bad_uris in ("[]", json.dumps(["https://a.example/%d" % i for i in range(11)]), '"str"', "{}"):
                with pytest.raises(asyncpg.PostgresError):
                    await a.fn("oauth_client_register", "dcr_" + secrets.token_urlsafe(18), "n", bad_uris, None)
    run(db, go)


def test_request_creation_validates_its_inputs_and_sweeps_old_rows(db):
    async def go():
        async with Conn(db) as a:
            ok = ("https://c.example/c.json", "https://c.example/cb", CHALLENGE, "agentbroker.tools", "s", "https://api.hatchloop.dev/mcp", 900)
            for bad_challenge in ("short", "!" * 43, "A" * 44):
                with pytest.raises(asyncpg.PostgresError):
                    await a.fn("oauth_request_create", secrets.token_urlsafe(16), ok[0], ok[1], bad_challenge, *ok[3:])
            with pytest.raises(asyncpg.PostgresError):
                await a.fn("oauth_request_create", "bad id!", *ok)
            with pytest.raises(asyncpg.PostgresError):
                await a.fn("oauth_request_create", secrets.token_urlsafe(16), ok[0], ok[1], CHALLENGE, ok[3], "s" * 3000, ok[5], 900)
            # old rows are swept by the next creation (no scheduler needed)
            old = secrets.token_urlsafe(16)
            await a.fn("oauth_request_create", old, *ok)
            c = await asyncpg.connect(db)
            try:
                await c.execute("update public.oauth_requests set expires_at = now() - interval '3 days' where request_id = $1", old)
                await a.fn("oauth_request_create", secrets.token_urlsafe(16), *ok)
                assert await c.fetchval("select count(*) from public.oauth_requests where request_id = $1", old) == 0
            finally:
                await c.close()
    run(db, go)


# ---------------------------------------------------------------------------
# which paid account an email stands for
# ---------------------------------------------------------------------------

def test_the_explicit_link_wins_and_cannot_be_overwritten(db):
    async def go():
        async with Conn(db) as a:
            eh = h("carol@example.org")
            assert (await a.fn("oauth_account_for_email", eh)) == {"found": False}
            assert (await a.fn("oauth_account_link", eh, "sub_cus_111", "cus_111", "developer")) == {"linked": True}
            got = await a.fn("oauth_account_for_email", eh)
            assert got == {"found": True, "account_id": "sub_cus_111", "customer_id": "cus_111", "plan": "developer"}
            # first writer wins: a second call - even one naming somebody else's account - changes nothing
            assert (await a.fn("oauth_account_link", eh, "sub_cus_VICTIM", "cus_victim", "business")) == {"linked": False}
            assert (await a.fn("oauth_account_for_email", eh))["account_id"] == "sub_cus_111"
            # only subscription accounts can be linked
            for bad in ("free_abcdef0123456789", "sub_", "admin", "sub_a b"):
                with pytest.raises(asyncpg.PostgresError):
                    await a.fn("oauth_account_link", h("dave@example.org"), bad, None, None)
            with pytest.raises(asyncpg.PostgresError):
                await a.fn("oauth_account_link", "not-a-hash", "sub_x", None, None)
    run(db, go)


def test_an_account_created_before_the_link_existed_is_found_by_its_email_digest(db):
    async def go():
        c = await asyncpg.connect(db)
        try:
            await c.execute("insert into public.credit_accounts (account_id, customer_id, email, plan) values "
                            "('sub_cus_old', 'cus_old', '  Erin@Example.ORG ', 'business'), "
                            "('free_notme', null, 'frank@example.org', 'free'), "
                            "('sub_cus_noemail', 'cus_ne', null, 'developer') on conflict do nothing")
        finally:
            await c.close()
        async with Conn(db) as a:
            got = await a.fn("oauth_account_for_email", h("erin@example.org"))
            assert got == {"found": True, "account_id": "sub_cus_old", "customer_id": "cus_old", "plan": "business"}
            # a free_ account is never a paid account, even when its email matches
            assert (await a.fn("oauth_account_for_email", h("frank@example.org"))) == {"found": False}
            assert (await a.fn("oauth_account_for_email", "garbage")) == {"found": False}
    run(db, go)


# ---------------------------------------------------------------------------
# nothing secret leaves
# ---------------------------------------------------------------------------

def test_no_function_output_ever_contains_a_digest_or_a_hash_column(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, email = await _signin(a)
            outs = [
                await a.fn("oauth_request_get", rid),
                await a.fn("oauth_request_lookup_magic", h(magic)),
                await a.fn("oauth_request_poll", rid, h(poll)),
            ]
            await a.fn("oauth_request_decide", h(magic), True)
            code = secrets.token_urlsafe(32)
            outs.append(await a.fn("oauth_request_complete", rid, h(poll), h(code), 120))
            outs.append(await a.fn("oauth_code_consume", h(code)))
            blob = json.dumps(outs)
            for secret_value in (h(magic), h(poll), h(code), magic, poll, code):
                assert secret_value not in blob
            for key in ("poll_hash", "magic_hash", "code_hash", "token_hash"):
                assert key not in blob
            # the only place the person's identity appears is the digest handed to the redeemer of a code
            assert "alice@example.org" not in blob
    run(db, go)


def test_the_tables_hold_digests_never_the_raw_values_or_an_email(db):
    async def go():
        async with Conn(db) as a:
            rid, magic, poll, email = await _signin(a, email="Gina@Example.org".lower())
            await a.fn("oauth_request_decide", h(magic), True)
            code, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            await a.fn("oauth_request_complete", rid, h(poll), h(code), 120)
            await a.fn("oauth_refresh_store", h(rt), rid, CLIENT, h(email), "agentbroker.tools", RES, 2592000, 7776000)
        c = await asyncpg.connect(db)
        try:
            dump = ""
            for t in TABLES:
                dump += json.dumps([dict(r) for r in await c.fetch(f"select * from public.{t}")], default=str)
            for raw in (magic, poll, code, rt, "gina@example.org", "Gina@Example.org"):
                assert raw not in dump, "a raw secret or an email address is stored"
        finally:
            await c.close()
    run(db, go)


# ---------------------------------------------------------------------------
# the operator's before/after verifier agrees with the database (scripts/verify_spine_011.py)
# ---------------------------------------------------------------------------

def test_the_verifier_passes_a_correct_migration_fails_a_sabotaged_one_and_reads_before_correctly(db):
    psycopg2 = pytest.importorskip("psycopg2")
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_spine_011", oauth_pg.ROOT / "scripts" / "verify_spine_011.py")
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)

    async def make_fresh():
        c = await asyncpg.connect(db)
        try:
            await c.execute("drop database if exists verify_fresh")
            await c.execute("create database verify_fresh")
        finally:
            await c.close()
        base = db.rsplit("/", 1)[0]
        f = await asyncpg.connect(base + "/verify_fresh")
        try:
            await f.execute(oauth_pg.PREPARE_SQL)
        finally:
            await f.close()
        return base + "/verify_fresh"
    fresh = asyncio.run(make_fresh())

    before = v.catalog(fresh)
    assert v.judge("before", before, None) == [] and not any(before["functions"].values())
    assert v.judge("after", before, None), "an empty database must fail the 'after' check"

    async def migrate(sql):
        f = await asyncpg.connect(fresh)
        try:
            await f.execute(sql)
        finally:
            await f.close()
    asyncio.run(migrate(oauth_pg.MIGRATION.read_text(encoding="utf-8")))
    after = v.catalog(fresh)
    assert v.judge("after", after, None) == [], v.judge("after", after, None)
    assert v.judge("before", after, None), "an applied migration must fail the 'before' check"
    assert after["row_counts"] == {t: 0 for t in v.TABLES}

    for sabotage, expect in (
        ("grant execute on function public.oauth_ready() to authenticated", "authenticated/public"),
        ("grant select on public.oauth_codes to anon", "direct grants"),
        ("alter table public.oauth_requests disable row level security", "row-level security OFF"),
        ("revoke execute on function public.oauth_code_consume(text) from anon", "not executable by anon"),
        ("alter function public.oauth_client_get(text) reset search_path", "search_path"),
        ("alter function public.oauth_client_get(text) security invoker", "not SECURITY DEFINER"),
    ):
        asyncio.run(migrate(sabotage))
        problems = v.judge("after", v.catalog(fresh), None)
        assert any(expect in p for p in problems), (sabotage, problems)
        asyncio.run(migrate(oauth_pg.MIGRATION.read_text(encoding="utf-8")))        # the migration repairs its own grants
        if "search_path" in sabotage or "security invoker" in sabotage:
            continue
    assert v.judge("after", v.catalog(fresh), None) == []
