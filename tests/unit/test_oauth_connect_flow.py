"""The whole Connect sign-in, driven through the real app, against BOTH stores.

Every test here runs twice: once on the in-memory store and once on the production `SpineStore` code talking
to the real SQL of migration 011 in a throwaway PostgreSQL (see tests/oauth_pg.py; skipped with a stated
reason when docker or the image is missing). That is what keeps the in-memory twin honest: a scenario that
passes on one and fails on the other is a bug in whichever one diverged.

What is pinned (each is a way the sign-in could silently be wrong):
  * the happy path for a registered client AND for a metadata-document client, ending in an access token the
    service's own `validate_token` accepts, bound to the right resource, one hour long, carrying the account
    the email stands for;
  * the code goes to the browser that STARTED the sign-in - never to whoever pressed Confirm - including when
    the link is opened on another device;
  * a mail scanner opening the link spends nothing; replacing the link kills the old one;
  * a code works once; replaying it revokes what it started; PKCE, redirect URI, client and resource are all
    bound; a failed attempt burns the code;
  * refresh tokens rotate, replay kills the chain, another client cannot use them, a revoked customer is cut
    off, and a purchase made on the website shows up as a paid identity at the next refresh;
  * errors before the return address is trusted are pages, after it are redirects with `state` and `iss`;
  * nothing is sent to an address we could not mail, nobody is told to check an inbox when nothing was sent;
  * one switch turns the whole thing into 404s.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import main
from agent_interface.identity import validate_token
from agent_interface.oauth import clients as oclients
from agent_interface.oauth import emailer, limits, settings, tokens
from agent_interface.oauth.link import link_purchase
from agent_interface.oauth.store import MemoryStore, SpineStore, StoreUnavailable, set_store
from tests import oauth_pg
from tests.oauth_support import (
    CLAUDE_CALLBACK, CLAUDE_CIMD, HTTPS_BASE, claude_document, code_of, install_cimd, install_mailbox,
    magic_of, match_code_of, pkce, poll_secret_of, query_of, rid_of, unique_email,
)

DCR_REDIRECT = "https://assistant.example.org/callback"
RESOURCE = "https://api.hatchloop.dev/mcp"


# ---------------------------------------------------------------------------
# the two backends
# ---------------------------------------------------------------------------

class Env:
    """The store under test, plus the few things a test needs to do to it that no endpoint allows."""

    def __init__(self, kind, store, dsn=None):
        self.kind, self.store, self.dsn = kind, store, dsn

    def _sql(self, sql, *args):
        return asyncio.run(oauth_pg.query(self.dsn, sql, *args))

    def age_resend(self, rid):
        if self.kind == "memory":
            self.store.requests[rid]["last_email_at"] -= 3600
        else:
            self._sql("update public.oauth_requests set last_email_at = now() - interval '1 hour' where request_id = $1", rid)

    def expire_signin(self, rid):
        if self.kind == "memory":
            self.store.requests[rid]["expires_at"] = 0
        else:
            self._sql("update public.oauth_requests set expires_at = now() - interval '1 second' where request_id = $1", rid)

    def expire_all_codes(self):
        if self.kind == "memory":
            for c in self.store.codes.values():
                c["expires_at"] = 0
        else:
            self._sql("update public.oauth_codes set expires_at = now() - interval '1 second'")

    def link(self, email, account_id, customer_id, plan):
        return asyncio.run(self.store.account_link(tokens.email_hash(email), account_id, customer_id, plan))

    def refresh_rows(self):
        if self.kind == "memory":
            return len(self.store.refresh)
        return self._sql("select count(*) as n from public.oauth_refresh_tokens")[0]["n"]


@pytest.fixture(scope="module")
def _pg_dsn():
    why = oauth_pg.docker_unavailable_reason()
    if why:
        pytest.skip(why)
    with oauth_pg.start_postgres() as dsn:
        yield dsn


@pytest.fixture(params=["memory", "postgres"])
def env(request, monkeypatch):
    if request.param == "memory":
        store = MemoryStore()
        e = Env("memory", store)
    else:
        dsn = request.getfixturevalue("_pg_dsn")
        store = SpineStore(rpc=oauth_pg.PgRpc(dsn))
        e = Env("postgres", store, dsn)
    set_store(store)
    limits.LIMITS.reset()
    oclients.FETCHER.clear()
    yield e
    set_store(None)
    limits.LIMITS.reset()


@pytest.fixture
def mailbox(monkeypatch, env):
    return install_mailbox(monkeypatch)


@pytest.fixture
def browser(env):
    """A browser: cookies kept between calls, redirects NOT followed (the test reads them)."""
    c = TestClient(main.app, base_url=HTTPS_BASE, follow_redirects=False, raise_server_exceptions=True)
    yield c
    c.close()


def other_device():
    return TestClient(main.app, base_url=HTTPS_BASE, follow_redirects=False)


# ---------------------------------------------------------------------------
# scenario helpers
# ---------------------------------------------------------------------------

def register(c, redirect_uris=None, name="Test Assistant") -> str:
    r = c.post("/oauth/register", json={"redirect_uris": redirect_uris or [DCR_REDIRECT], "client_name": name,
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(c, client_id, redirect_uri, challenge, *, state="st-123", resource=None, extra=None):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "scope": "agentbroker.tools offline_access"}
    if resource:
        params["resource"] = resource
    params.update(extra or {})
    return c.get("/oauth/authorize", params=params)


def start_signin(c, mailbox, client_id, redirect_uri, challenge, email, **kw):
    """authorize + email. Returns rid, poll_secret, link, magic."""
    page = authorize(c, client_id, redirect_uri, challenge, **kw)
    assert page.status_code == 200, page.text
    rid = rid_of(page.text)
    sent = c.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": poll_secret_of(page.text), "email": email})
    assert sent.status_code == 200, sent.text
    assert "Check your email" in sent.text
    link = mailbox.last_link
    return {"rid": rid, "poll_secret": poll_secret_of(sent.text), "link": link, "magic": magic_of(link),
            "wait": sent.text, "code": match_code_of(sent.text)}


def press(c, magic, decision="approve", code=None):
    data = {"t": magic, "decision": decision}
    if code is not None:
        data["code"] = code
    return c.post("/oauth/verify", data=data)


def poll(c, rid, secret):
    return c.post("/oauth/authorize/poll", json={"rid": rid, "poll_secret": secret})


def exchange(c, client_id, redirect_uri, code, verifier, **extra):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": verifier}
    data.update(extra)
    return c.post("/oauth/token", data=data)


def refresh(c, client_id, refresh_token, **extra):
    return c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                        "client_id": client_id, **extra})


def full_signin(c, mailbox, client_id=None, redirect_uri=DCR_REDIRECT, email=None, **kw):
    """Run a complete sign-in in one browser and return (token response json, context)."""
    client_id = client_id or register(c)
    email = email or unique_email()
    verifier, challenge = pkce()
    s = start_signin(c, mailbox, client_id, redirect_uri, challenge, email, **kw)
    r = press(c, s["magic"])
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    t = exchange(c, client_id, redirect_uri, code_of(loc), verifier)
    assert t.status_code == 200, t.text
    return t.json(), {"client_id": client_id, "email": email, "location": loc, "verifier": verifier, **s}


# ---------------------------------------------------------------------------
# the happy paths
# ---------------------------------------------------------------------------

def test_a_registered_client_signs_in_and_gets_a_one_hour_key_bound_to_the_resource(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox, state="state-abc")
    q = query_of(ctx["location"])
    assert q["state"] == "state-abc" and q["iss"] == HTTPS_BASE      # RFC 9207: the issuer rides every response
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600 and tok["scope"] == "agentbroker.tools"
    assert tok["refresh_token"] and tok["access_token"] != tok["refresh_token"]

    v = validate_token(tok["access_token"])
    assert v.valid, v.error
    assert v.identity.agent_id == f"free_{tokens.email_hash(ctx['email'])[:16]}"       # the id /keys/verify derives
    assert v.identity.scope.budget_cap == 0.0                                          # free tier: no credit spend
    ttl = v.identity.expiry.timestamp() - time.time()
    assert 3500 < ttl <= 3600
    claims = tokens.__dict__  # noqa: F841 - keep the module imported for the audience assertion below
    from agent_interface.identity import _verify
    c = _verify(tok["access_token"])
    assert c["aud"] == RESOURCE and c["scp"] == "agentbroker.tools"
    assert c["principal"]["type"] == "human"
    assert ctx["email"] not in str(c) and tokens.email_hash(ctx["email"]) not in str(c)    # the key carries no email


def test_a_metadata_document_client_signs_in_without_ever_registering(env, browser, mailbox, monkeypatch):
    _, calls = install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document()})
    tok, ctx = full_signin(browser, mailbox, client_id=CLAUDE_CIMD, redirect_uri=CLAUDE_CALLBACK)
    assert validate_token(tok["access_token"]).valid
    assert len(calls) == 1                                    # fetched once, then cached across the whole sign-in
    assert calls[0].url.host == "160.79.104.10"               # connected to the address that was checked ...
    assert calls[0].headers["host"] == "claude.ai"            # ... while presenting the name
    assert env.refresh_rows() >= 1
    assert mailbox.sent[-1]["app"] == "claude.ai" and mailbox.sent[-1]["host"] == "claude.ai"


def test_the_signin_page_names_the_host_not_the_self_chosen_name(env, browser, monkeypatch):
    install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document(client_name="Totally Legit Bank")})
    _, challenge = pkce()
    page = authorize(browser, CLAUDE_CIMD, CLAUDE_CALLBACK, challenge)
    assert "Connect claude.ai to AgentBroker" in page.text
    assert "Totally Legit Bank" in page.text and "chosen by the app, not verified" in page.text


# ---------------------------------------------------------------------------
# who receives the code
# ---------------------------------------------------------------------------

def test_the_link_opened_on_another_device_hands_the_code_to_the_page_that_started_it(env, browser, mailbox):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())

    phone = other_device()
    # opening the link on the phone does not spend it - twice, like a mail scanner would
    for _ in range(2):
        page = phone.get(f"/oauth/verify?t={s['magic']}")
        assert page.status_code == 200 and "Confirm and connect" in page.text
    # still waiting
    assert poll(browser, s["rid"], s["poll_secret"]).json()["status"] == "pending"

    r = press(phone, s["magic"], code=s["code"])        # a different browser: it must present the code the starting page shows
    assert r.status_code == 200 and "go back to the app" in r.text.lower()        # the phone gets NO redirect and NO code
    assert "code=" not in r.text and "location" not in r.headers

    # a stranger holding only the request id learns nothing and receives nothing
    assert poll(other_device(), s["rid"], "x" * 32).json()["status"] == "unknown"

    got = poll(browser, s["rid"], s["poll_secret"]).json()
    assert got["status"] == "redirect"
    code = code_of(got["location"])
    assert exchange(browser, client_id, DCR_REDIRECT, code, verifier).status_code == 200
    # delivered exactly once
    assert poll(browser, s["rid"], s["poll_secret"]).json()["status"] == "completed"


def test_pressing_confirm_in_the_same_browser_finishes_without_waiting_for_the_poll(env, browser, mailbox):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    r = press(browser, s["magic"])
    assert r.status_code == 303 and r.headers["location"].startswith(DCR_REDIRECT + "?")
    assert "no-store" in r.headers["cache-control"]


def test_a_cookie_from_a_different_signin_delivers_nothing(env, browser, mailbox):
    client_id = register(browser)
    _, ch1 = pkce()
    first = start_signin(browser, mailbox, client_id, DCR_REDIRECT, ch1, unique_email())      # leaves the cookie for sign-in 1
    victim = other_device()
    _, ch2 = pkce()
    second = start_signin(victim, mailbox, client_id, DCR_REDIRECT, ch2, unique_email())
    r = press(browser, second["magic"])             # sign-in 1's browser presses sign-in 2's link: the cookie is for another sign-in
    assert r.status_code == 400 and "location" not in r.headers and "does not match" in r.text
    assert poll(browser, first["rid"], first["poll_secret"]).json()["status"] == "pending"
    ok = press(browser, second["magic"], code=second["code"])
    assert ok.status_code == 200 and "location" not in ok.headers


def test_denying_returns_access_denied_with_state_and_issuer_once(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email(), state="deny-me")
    r = press(browser, s["magic"], "deny")
    assert r.status_code == 303
    q = query_of(r.headers["location"])
    assert q["error"] == "access_denied" and q["state"] == "deny-me" and q["iss"] == HTTPS_BASE and "code" not in q
    assert poll(browser, s["rid"], s["poll_secret"]).json()["status"] == "completed"


def test_replacing_the_link_kills_the_old_one_and_a_pressed_link_is_spent(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    email = unique_email()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, email)
    env.age_resend(s["rid"])
    again = browser.post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": s["poll_secret"], "email": email})
    assert again.status_code == 200 and len(mailbox.sent) == 2
    new_magic = magic_of(mailbox.last_link)
    assert new_magic != s["magic"]
    stale = browser.get(f"/oauth/verify?t={s['magic']}")
    assert stale.status_code == 400 and "not valid" in stale.text
    assert press(browser, new_magic).status_code == 303
    assert "already used" in browser.get(f"/oauth/verify?t={new_magic}").text.lower()


# ---------------------------------------------------------------------------
# the authorization code and what it is bound to
# ---------------------------------------------------------------------------

def test_a_code_works_once_and_replaying_it_revokes_the_tokens_it_started(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox)
    code = code_of(ctx["location"])
    replay = exchange(browser, ctx["client_id"], DCR_REDIRECT, code, ctx["verifier"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    # the refresh token handed out by the first redemption no longer works
    r = refresh(browser, ctx["client_id"], tok["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_wrong_pkce_verifier_fails_and_burns_the_code(env, browser, mailbox):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    code = code_of(press(browser, s["magic"]).headers["location"])
    bad = exchange(browser, client_id, DCR_REDIRECT, code, pkce()[0])
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    good = exchange(browser, client_id, DCR_REDIRECT, code, verifier)
    assert good.status_code == 400                       # the thief's failed attempt spent it


@pytest.mark.parametrize("field,value", [("redirect_uri", "https://assistant.example.org/other"),
                                          ("client_id", "dcr_someone_else_entirely_x")])
def test_a_code_is_bound_to_its_client_and_redirect_uri(env, browser, mailbox, field, value):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    code = code_of(press(browser, s["magic"]).headers["location"])
    args = {"client_id": client_id, "redirect_uri": DCR_REDIRECT}
    args[field] = value
    r = exchange(browser, args["client_id"], args["redirect_uri"], code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_the_resource_is_bound_at_authorization_and_checked_at_the_token_endpoint(env, browser, mailbox):
    site = "https://hatchloop.dev/mcp/agent-broker"
    tok, ctx = full_signin(browser, mailbox, resource=site)
    from agent_interface.identity import _verify
    assert _verify(tok["access_token"])["aud"] == site

    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email(), resource=site)
    code = code_of(press(browser, s["magic"]).headers["location"])
    wrong = exchange(browser, client_id, DCR_REDIRECT, code, verifier, resource="https://api.hatchloop.dev/mcp")
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"


def test_an_expired_code_is_refused(env, browser, mailbox):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    code = code_of(press(browser, s["magic"]).headers["location"])
    env.expire_all_codes()
    assert exchange(browser, client_id, DCR_REDIRECT, code, verifier).json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# refresh tokens
# ---------------------------------------------------------------------------

def test_refreshing_rotates_the_token_and_replaying_the_old_one_ends_the_chain(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox)
    cid = ctx["client_id"]
    r1 = refresh(browser, cid, tok["refresh_token"])
    assert r1.status_code == 200, r1.text
    second = r1.json()
    assert second["refresh_token"] != tok["refresh_token"] and validate_token(second["access_token"]).valid
    assert validate_token(second["access_token"]).identity.agent_id == validate_token(tok["access_token"]).identity.agent_id
    # the spent token comes back: reuse -> nothing in the chain works any more
    assert refresh(browser, cid, tok["refresh_token"]).json()["error"] == "invalid_grant"
    assert refresh(browser, cid, second["refresh_token"]).json()["error"] == "invalid_grant"


def test_another_client_cannot_use_a_refresh_token_and_that_does_not_burn_it(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox)
    other = register(browser, ["https://other.example.org/cb"])
    assert refresh(browser, other, tok["refresh_token"]).json()["error"] == "invalid_grant"
    assert refresh(browser, ctx["client_id"], tok["refresh_token"]).status_code == 200


def test_revoking_the_refresh_token_ends_the_chain_and_always_answers_200(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox)
    rev = browser.post("/oauth/revoke", data={"token": tok["refresh_token"], "client_id": ctx["client_id"]})
    assert rev.status_code == 200
    assert refresh(browser, ctx["client_id"], tok["refresh_token"]).json()["error"] == "invalid_grant"
    stranger = browser.post("/oauth/revoke", data={"token": "never-issued-" * 3, "client_id": ctx["client_id"]})
    assert stranger.status_code == 200 and stranger.json() == {}


def test_a_purchase_made_on_the_website_appears_as_a_paid_identity_at_the_next_refresh(env, browser, mailbox):
    email = unique_email("buyer")
    tok, ctx = full_signin(browser, mailbox, email=email)
    before = validate_token(tok["access_token"]).identity
    assert before.agent_id.startswith("free_") and before.scope.budget_cap == 0.0

    assert asyncio.run(link_purchase(email, "sub_cus_BUY123", "cus_BUY123", "developer")) is True
    r = refresh(browser, ctx["client_id"], tok["refresh_token"])
    assert r.status_code == 200
    after = validate_token(r.json()["access_token"]).identity
    assert after.agent_id == "sub_cus_BUY123" and after.principal.id == "cus_BUY123"
    assert after.scope.budget_cap > 0
    # the link is first-writer-wins: a second claim to the same email changes nothing
    assert asyncio.run(link_purchase(email, "sub_cus_OTHER", "cus_OTHER", "business")) is False


def test_a_refunded_customer_is_cut_off_at_the_next_refresh(env, browser, mailbox):
    from agent_interface import identity as ident
    email = unique_email("refunded")
    tok, ctx = full_signin(browser, mailbox, email=email)
    cust = "cus_REFUND_" + email.split("@")[0][-6:]
    asyncio.run(link_purchase(email, f"sub_{cust}", cust, "developer"))
    r = refresh(browser, ctx["client_id"], tok["refresh_token"])
    assert r.status_code == 200
    ident._revoked_customer_ids.add(cust)
    try:
        assert not validate_token(r.json()["access_token"]).valid              # the token itself stops working ...
        assert refresh(browser, ctx["client_id"], r.json()["refresh_token"]).json()["error"] == "invalid_grant"   # ... and so does renewal
    finally:
        ident._revoked_customer_ids.discard(cust)


# ---------------------------------------------------------------------------
# errors: pages before the return address is trusted, redirects after
# ---------------------------------------------------------------------------

def test_an_unknown_client_is_a_page_not_a_redirect(env, browser):
    _, challenge = pkce()
    r = authorize(browser, "dcr_never_registered_anywhere", DCR_REDIRECT, challenge)
    assert r.status_code == 400 and "location" not in r.headers and "Cannot connect" in r.text


def test_a_return_address_that_was_not_registered_gets_nothing_sent_to_it(env, browser):
    client_id = register(browser)
    _, challenge = pkce()
    for bad in ("https://evil.example.net/cb", DCR_REDIRECT + "/extra", "http://assistant.example.org/callback",
                "javascript:alert(1)"):
        r = authorize(browser, client_id, bad, challenge)
        assert r.status_code == 400 and "location" not in r.headers, bad


def test_a_missing_return_address_is_filled_in_only_when_exactly_one_is_registered(env, browser):
    _, challenge = pkce()
    single = register(browser)
    page = browser.get("/oauth/authorize", params={"response_type": "code", "client_id": single,
                                                   "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 200
    two = register(browser, [DCR_REDIRECT, "https://assistant.example.org/second"])
    page = browser.get("/oauth/authorize", params={"response_type": "code", "client_id": two,
                                                   "code_challenge": challenge, "code_challenge_method": "S256"})
    assert page.status_code == 400 and "location" not in page.headers


def test_protocol_errors_after_the_return_address_is_trusted_redirect_with_state_and_issuer(env, browser):
    client_id = register(browser)
    _, challenge = pkce()
    cases = [
        ({"code_challenge": ""}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://evil.example.net/mcp"}, "invalid_target"),
    ]
    for override, error in cases:
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": DCR_REDIRECT,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "keep-me"}
        params.update(override)
        r = browser.get("/oauth/authorize", params=params)
        assert r.status_code == 302, (override, r.status_code)
        q = query_of(r.headers["location"])
        assert q["error"] == error and q["state"] == "keep-me" and q["iss"] == HTTPS_BASE, override


def test_unrecognised_scopes_are_ignored_and_the_response_says_what_was_granted(env, browser, mailbox):
    tok, _ = full_signin(browser, mailbox, extra=None)
    assert tok["scope"] == "agentbroker.tools"
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email(),
                     extra={"scope": "openid profile email"})
    code = code_of(press(browser, s["magic"]).headers["location"])
    assert exchange(browser, client_id, DCR_REDIRECT, code, verifier).json()["scope"] == "agentbroker.tools"


def test_a_signin_that_has_expired_cannot_be_mailed_or_pressed(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    env.expire_signin(s["rid"])
    assert "expired" in browser.get(f"/oauth/verify?t={s['magic']}").text
    assert press(browser, s["magic"]).status_code == 400
    assert poll(browser, s["rid"], s["poll_secret"]).json()["status"] == "expired"
    r = browser.post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": s["poll_secret"], "email": unique_email()})
    assert r.status_code == 400 and "expired" in r.text.lower()


# ---------------------------------------------------------------------------
# the email
# ---------------------------------------------------------------------------

def _page(c, client_id, challenge):
    return authorize(c, client_id, DCR_REDIRECT, challenge)


def test_a_malformed_address_is_sent_nothing(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    pg = _page(browser, client_id, challenge)
    rid, ps = rid_of(pg.text), poll_secret_of(pg.text)
    for bad in ("", "no-at-sign", "a@b", "a b@example.org", "<script>@example.org", "x@" + "a" * 300 + ".org"):
        r = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": bad})
        assert r.status_code == 400 and "does not look like an email" in r.text, bad
    assert mailbox.sent == []


def test_an_address_the_provider_rejects_is_a_correctable_error_and_an_outage_is_not_a_success(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    pg = _page(browser, client_id, challenge)
    rid, ps = rid_of(pg.text), poll_secret_of(pg.text)
    mailbox.outcome = emailer.REJECTED
    r = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": unique_email()})
    assert r.status_code == 400 and "not accepted" in r.text and "Check your email" not in r.text
    # the page offered again carries the SAME poll secret, so correcting the address can succeed
    assert poll_secret_of(r.text) == ps and rid_of(r.text) == rid
    env.age_resend(rid)
    mailbox.outcome = emailer.UNAVAILABLE
    r = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": unique_email()})
    assert r.status_code == 503 and "Nothing was sent" in r.text and "Check your email" not in r.text
    assert mailbox.sent == []
    env.age_resend(rid)
    mailbox.outcome = emailer.SENT
    r = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": unique_email()})
    assert r.status_code == 200 and "Check your email" in r.text and len(mailbox.sent) == 1


def test_resending_too_quickly_and_too_often_is_refused(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    email = unique_email()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, email)
    quick = browser.post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": s["poll_secret"], "email": email})
    assert quick.status_code == 429 and len(mailbox.sent) == 1
    for _ in range(settings.MAGIC_MAX_SENDS - 1):
        env.age_resend(s["rid"])
        assert browser.post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": s["poll_secret"],
                                                           "email": unique_email()}).status_code == 200
    env.age_resend(s["rid"])
    over = browser.post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": s["poll_secret"], "email": email})
    assert over.status_code == 429 and "used all of its emails" in over.text


def test_a_different_poll_secret_cannot_take_over_a_signin(env, browser, mailbox):
    client_id = register(browser)
    _, challenge = pkce()
    email = unique_email()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, email)
    env.age_resend(s["rid"])
    r = other_device().post("/oauth/authorize/email", data={"rid": s["rid"], "poll_secret": "z" * 32, "email": email})
    assert r.status_code == 400 and len(mailbox.sent) == 1


def test_one_recipient_cannot_be_mailed_without_limit(env, browser, mailbox):
    client_id = register(browser)
    email = unique_email("victim")
    seen = []
    for _ in range(6):
        _, challenge = pkce()
        dev = other_device()
        pg = _page(dev, client_id, challenge)
        r = dev.post("/oauth/authorize/email", data={"rid": rid_of(pg.text), "poll_secret": poll_secret_of(pg.text), "email": email})
        seen.append(r.status_code)
    assert seen[:4] == [200] * 4 and 429 in seen[4:]
    assert len(mailbox.sent) == 4


def test_one_address_cannot_start_unlimited_emails(env, browser, mailbox):
    client_id = register(browser)
    seen = []
    for i in range(10):
        _, challenge = pkce()
        pg = _page(browser, client_id, challenge)
        seen.append(browser.post("/oauth/authorize/email", data={"rid": rid_of(pg.text), "poll_secret": poll_secret_of(pg.text),
                                                                "email": unique_email()}).status_code)
    assert seen[:8] == [200] * 8 and set(seen[8:]) == {429}


def test_the_mail_names_the_app_and_contains_no_secret_but_the_one_link(env, browser, mailbox):
    start_signin(browser, mailbox, register(browser, name="Acme <b>Bot</b>"), DCR_REDIRECT, pkce()[1], unique_email())
    sent = mailbox.sent[-1]
    assert sent["link"].startswith(f"{HTTPS_BASE}/oauth/verify?t=") and sent["host"] == "assistant.example.org"
    subject, html_body, text_body = emailer.compose(sent["link"], "Acme <b>Bot</b>", "assistant.example.org")
    assert "<b>Bot</b>" not in html_body and "&lt;b&gt;Bot&lt;/b&gt;" in html_body        # a name cannot inject markup
    assert "Authorization" not in html_body and "access_token" not in html_body


# ---------------------------------------------------------------------------
# the token endpoint's manners
# ---------------------------------------------------------------------------

def test_the_token_endpoint_speaks_form_and_json_and_never_caches(env, browser, mailbox):
    tok, ctx = full_signin(browser, mailbox)
    r = browser.post("/oauth/token", json={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                           "client_id": ctx["client_id"]})
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"] and r.headers["access-control-allow-origin"] == "*"


def test_token_errors_are_rfc6749_errors(env, browser):
    assert browser.post("/oauth/token", data={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"
    assert browser.post("/oauth/token", data={"grant_type": "authorization_code"}).json()["error"] == "invalid_request"
    assert browser.post("/oauth/token", data={"grant_type": "refresh_token"}).json()["error"] == "invalid_request"
    r = refresh(browser, "dcr_anything_at_all_here", "not-a-real-token-" * 3)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_when_the_database_is_down_the_token_endpoint_says_try_again_not_something_that_looks_like_success(env, browser, mailbox, monkeypatch):
    tok, ctx = full_signin(browser, mailbox)

    async def down(*a, **k):
        raise StoreUnavailable("down")
    monkeypatch.setattr(env.store, "refresh_rotate", down)
    r = refresh(browser, ctx["client_id"], tok["refresh_token"])
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable" and r.headers["retry-after"]


def test_if_only_the_refresh_token_cannot_be_stored_the_person_still_gets_their_access_token(env, browser, mailbox, monkeypatch):
    client_id = register(browser)
    verifier, challenge = pkce()
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, challenge, unique_email())
    code = code_of(press(browser, s["magic"]).headers["location"])

    async def down(*a, **k):
        raise StoreUnavailable("down")
    monkeypatch.setattr(env.store, "refresh_store", down)
    r = exchange(browser, client_id, DCR_REDIRECT, code, verifier)
    assert r.status_code == 200 and "refresh_token" not in r.json() and validate_token(r.json()["access_token"]).valid


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_registration_accepts_loopback_and_native_redirects_and_refuses_the_rest(env, browser):
    ok = browser.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:8123/cb", "cursor://anysphere.cursor/oauth/cb"],
                                               "client_name": "Local\x00 Tool"})
    assert ok.status_code == 201 and ok.json()["client_name"] == "Local  Tool"
    assert ok.json()["client_id"].startswith("dcr_") and ok.json()["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in ok.json()
    for bad in ({}, {"redirect_uris": []}, {"redirect_uris": ["http://evil.example.net/cb"]},
                {"redirect_uris": ["javascript:alert(1)"]}, {"redirect_uris": ["https://a.example.org/cb#frag"]},
                {"redirect_uris": ["https://a.example.org/cb"], "token_endpoint_auth_method": "client_secret_basic"},
                {"redirect_uris": ["https://a.example.org/cb"], "grant_types": ["client_credentials"]},
                {"redirect_uris": ["https://u:p@a.example.org/cb"]}, [], "x"):
        r = browser.post("/oauth/register", json=bad)
        assert r.status_code == 400 and r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata"), bad


def test_registration_is_rate_limited(env, browser):
    codes = [browser.post("/oauth/register", json={"redirect_uris": [DCR_REDIRECT]}).status_code for _ in range(22)]
    assert codes[:20] == [201] * 20 and set(codes[20:]) == {429}


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------

def test_switching_the_feature_off_makes_every_route_a_404(env, browser, monkeypatch):
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "0")
    for method, path in (("get", "/.well-known/oauth-protected-resource"), ("get", "/.well-known/oauth-protected-resource/mcp"),
                         ("get", "/.well-known/oauth-authorization-server"), ("get", "/.well-known/openid-configuration"),
                         ("get", "/oauth/authorize"), ("get", "/oauth/verify?t=" + "a" * 40), ("post", "/oauth/register"),
                         ("post", "/oauth/token"), ("post", "/oauth/revoke"), ("post", "/oauth/authorize/email"),
                         ("post", "/oauth/authorize/poll"), ("post", "/oauth/verify")):
        assert getattr(browser, method)(path).status_code == 404, path
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "1")
    assert browser.get("/.well-known/oauth-authorization-server").status_code == 200


def test_the_pages_are_served_with_the_security_headers(env, browser, mailbox):
    _, challenge = pkce()
    page = _page(browser, register(browser), challenge)
    h = page.headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    assert "no-store" in h["cache-control"] and h["referrer-policy"] == "no-referrer"
    assert "form-action 'self'" in h["content-security-policy"]
    nonce = h["content-security-policy"].split("style-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in page.text and "<script" not in page.text      # no script on the start page at all
    # the confirmation page must NOT restrict form-action: its reply is a redirect to the app
    client_id = register(browser)
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, pkce()[1], unique_email())
    conf = browser.get(f"/oauth/verify?t={s['magic']}")
    assert "form-action" not in conf.headers["content-security-policy"] and "frame-ancestors 'none'" in conf.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# consent phishing: someone starts a sign-in with YOUR address
# ---------------------------------------------------------------------------

def test_a_victim_who_did_not_start_the_signin_cannot_complete_it_without_the_code(env, mailbox):
    """The attack: A starts a sign-in for their own client but types B's address. B gets a genuine email. If
    pressing Confirm were enough, A's page would receive B's authorization code. The link opened outside A's
    browser therefore asks for the 4-digit code only A's page shows - which B, who started nothing, does not have."""
    attacker, victim = other_device(), other_device()
    client_id = register(attacker, ["https://attacker.example.net/cb"])
    verifier, challenge = pkce()
    victim_email = unique_email("victim")
    s = start_signin(attacker, mailbox, client_id, "https://attacker.example.net/cb", challenge, victim_email)

    page = victim.get(f"/oauth/verify?t={s['magic']}")              # the victim opens the genuine email
    assert page.status_code == 200 and 'name="code"' in page.text and "attacker.example.net" in page.text

    for attempt in (None, "", "0000" if s["code"] != "0000" else "1111", "abcd", "12"):
        r = press(victim, s["magic"], code=attempt)
        assert r.status_code == 400 and "does not match" in r.text and "location" not in r.headers, attempt
    assert poll(attacker, s["rid"], s["poll_secret"]).json()["status"] == "pending"      # nothing was delivered
    assert env.store is not None
    # Cancel needs no code - declining is always safe
    assert press(victim, s["magic"], "deny").status_code == 200
    got = poll(attacker, s["rid"], s["poll_secret"]).json()
    assert got["status"] == "redirect" and query_of(got["location"])["error"] == "access_denied"


def test_the_code_is_attempt_limited_so_it_cannot_be_guessed(env, mailbox):
    attacker, victim = other_device(), other_device()
    client_id = register(attacker, ["https://attacker.example.net/cb"])
    s = start_signin(attacker, mailbox, client_id, "https://attacker.example.net/cb", pkce()[1], unique_email("v"))
    wrong = "0000" if s["code"] != "0000" else "1111"
    codes = [press(victim, s["magic"], code=wrong).status_code for _ in range(5)]
    assert codes == [400] * 5
    locked = press(victim, s["magic"], code=s["code"])         # even the right code is refused after five tries
    assert locked.status_code == 429 and "location" not in locked.headers
    assert poll(attacker, s["rid"], s["poll_secret"]).json()["status"] == "pending"


def test_the_starting_browser_needs_no_code_and_a_stranger_with_the_right_code_does(env, browser, mailbox):
    client_id = register(browser)
    s = start_signin(browser, mailbox, client_id, DCR_REDIRECT, pkce()[1], unique_email())
    assert 'name="code"' not in browser.get(f"/oauth/verify?t={s['magic']}").text              # same browser: nothing to type
    assert press(browser, s["magic"]).status_code == 303
    s2 = start_signin(browser, mailbox, client_id, DCR_REDIRECT, pkce()[1], unique_email())
    stranger = other_device()
    assert 'name="code"' in stranger.get(f"/oauth/verify?t={s2['magic']}").text
    ok = press(stranger, s2["magic"], code=f" {s2['code'][:2]} {s2['code'][2:]} ")             # spaces tolerated
    assert ok.status_code == 200 and "Confirmed" in ok.text


def test_the_code_belongs_to_one_signin_and_is_not_guessable_from_another(env, browser, mailbox):
    codes = set()
    client_id = register(browser)
    for _ in range(12):
        limits.LIMITS.reset()                    # this test is about the code, not about the mail ceilings
        s = start_signin(other_device(), mailbox, client_id, DCR_REDIRECT, pkce()[1], unique_email())
        codes.add(s["code"])
    assert len(codes) >= 8                       # derived per sign-in, not constant or sequential
    assert tokens.match_code("a" * 24) == tokens.match_code("a" * 24) != tokens.match_code("b" * 24)


def test_only_plain_ascii_addresses_are_mailed(env, browser, mailbox):
    client_id = register(browser)
    pg = _page(browser, client_id, pkce()[1])
    rid, ps = rid_of(pg.text), poll_secret_of(pg.text)
    for bad in ("user\uff20example.org", "\u0430lice@example.org", "al\u00efce@example.org", "a..b@example.org",
                ".a@example.org", "a.@example.org", "a@exa_mple.org", "a@example", "a@-example.org"):
        limits.LIMITS.reset()                    # this test is about validation, not about the mail ceilings
        r = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": bad})
        assert r.status_code == 400 and "does not look like an email" in r.text, repr(bad)
    assert mailbox.sent == []
    limits.LIMITS.reset()
    ok = browser.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": ps, "email": "o'brien+tag@example.org"})
    assert ok.status_code == 200 and mailbox.sent[-1]["to"] == "o'brien+tag@example.org"


# ---------------------------------------------------------------------------
# the store's own guarantees, below the endpoints (both implementations must give the same answers)
# ---------------------------------------------------------------------------

def test_the_store_itself_refuses_a_wrong_poll_secret_even_if_an_endpoint_forgot_to_check(env):
    store = env.store
    rid, magic, poll = tokens.new_secret(18), tokens.new_secret(32), tokens.new_secret(24)

    async def scenario():
        await store.request_create(rid, "dcr_x", "https://a.example.org/cb", "A" * 43, "agentbroker.tools", "s",
                                   "https://api.hatchloop.dev/mcp", 900)
        sent = await store.request_set_email(rid, tokens.sha256_hex(poll), tokens.sha256_hex(magic),
                                             tokens.email_hash("a@example.org"), "a***@example.org", 0, 5)
        assert sent["ok"] is True
        assert (await store.request_decide(tokens.sha256_hex(magic), True))["ok"] is True
        wrong = await store.request_complete(rid, tokens.sha256_hex("not-the-secret"), tokens.sha256_hex("c" * 40), 120)
        assert wrong == {"ok": False, "reason": "unknown"}
        assert await store.request_poll(rid, tokens.sha256_hex("not-the-secret")) == "unknown"
        right = await store.request_complete(rid, tokens.sha256_hex(poll), tokens.sha256_hex("c" * 40), 120)
        assert right["ok"] is True and right["outcome"] == "approved"
        again = await store.request_complete(rid, tokens.sha256_hex(poll), tokens.sha256_hex("d" * 40), 120)
        assert again == {"ok": False, "reason": "completed"}
    asyncio.run(scenario())


def test_a_forged_cookie_naming_the_right_signin_does_not_skip_the_match_code(env, mailbox):
    """The cookie's request id is a CLAIM: a browser can be made to send any cookie. Only a poll secret that
    hashes to the one the sign-in holds proves this is the starting browser (second adversarial-review round)."""
    attacker, victim = other_device(), other_device()
    client_id = register(attacker, ["https://attacker.example.net/cb"])
    s = start_signin(attacker, mailbox, client_id, "https://attacker.example.net/cb", pkce()[1], unique_email("v"))
    forged = f"{s['rid']}.{'x' * 32}"                          # right sign-in id, wrong poll secret
    victim.cookies.set("hl_oauth", forged, domain="api.hatchloop.dev", path="/oauth")

    assert 'name="code"' in victim.get(f"/oauth/verify?t={s['magic']}").text           # still asked for the code
    r = press(victim, s["magic"])
    assert r.status_code == 400 and "does not match" in r.text and "location" not in r.headers
    assert poll(attacker, s["rid"], s["poll_secret"]).json()["status"] == "pending"
    # even WITH the code, the forged cookie delivers nothing to the pressing browser
    ok = press(victim, s["magic"], code=s["code"])
    assert ok.status_code == 200 and "location" not in ok.headers
    got = poll(attacker, s["rid"], s["poll_secret"]).json()                               # the rightful starter still gets it
    assert got["status"] == "redirect" and "code=" in got["location"]
