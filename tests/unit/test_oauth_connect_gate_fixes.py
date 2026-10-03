"""Regression tests for the findings of the 2026-10-03 adversarial reviews and the integration gate.

Each test names the finding it closes. They were written BEFORE the fixes and failed on the reviewed commit
(0c9419f); the commit that adds the fixes makes them pass. All run on the in-memory store: the findings are
about the endpoints, not about the SQL.

  P1  cookie fixation   a cross-site form post could plant the "I started this sign-in" cookie in a victim's
                        browser, so the victim's own link skipped the match code and one press of Confirm
                        handed the authorization code to the attacker.
  P2  limiter flush     one anonymous caller could clear EVERY in-memory ceiling by flooding one endpoint.
  P2  slow drip         the metadata-document fetch had no TOTAL deadline.
  P2  host budget       cache hits spent the per-vendor fetch budget, so two throwaway addresses could lock
                        every Claude user out.
  P2  keyless 401       an agent with no OAuth support lost the readable "how to get a key" answer.
  P2  email label       a self-chosen app name was printed in a genuine email from our domain.
  P3  style off, vendor-sized ceilings.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import main
from agent_interface.oauth import challenge, emailer, limits, settings, tokens
from agent_interface.oauth import clients as oclients
from agent_interface.oauth import router as orouter
from agent_interface.oauth.store import MemoryStore, set_store
from tests.oauth_support import (
    CLAUDE_CALLBACK, CLAUDE_CIMD, HTTPS_BASE, claude_document, install_cimd, install_mailbox, magic_of,
    match_code_of, pkce, poll_secret_of, rid_of, unique_email,
)

DCR_REDIRECT = "https://assistant.example.org/callback"
ATTACKER_REDIRECT = "https://attacker.example.net/cb"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.delenv("OAUTH_CHALLENGE_STYLE", raising=False)
    monkeypatch.delenv("OAUTH_CONNECT_ENABLED", raising=False)
    monkeypatch.delenv("OAUTH_CHALLENGE_401_CLIENTS", raising=False)
    store = MemoryStore()
    set_store(store)
    limits.LIMITS.reset()
    oclients.FETCHER.clear()
    main._rl_buckets.clear()
    yield store
    set_store(None)
    limits.LIMITS.reset()
    main._rl_buckets.clear()


@pytest.fixture
def mailbox(monkeypatch):
    return install_mailbox(monkeypatch)


def device(**headers):
    # lower-case names: the test client's own default "user-agent" would otherwise be joined to ours
    return TestClient(main.app, base_url=HTTPS_BASE, follow_redirects=False,
                      headers={k.lower(): v for k, v in headers.items()} or None)


def register(c, uris=None, name="Test Assistant"):
    r = c.post("/oauth/register", json={"redirect_uris": uris or [DCR_REDIRECT], "client_name": name})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(c, client_id, redirect_uri=DCR_REDIRECT, challenge_=None):
    return c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": challenge_ or pkce()[1], "code_challenge_method": "S256", "state": "s"})


def set_cookie_names(resp):
    return [h.split("=", 1)[0] for h in resp.headers.get_list("set-cookie")]


# ---------------------------------------------------------------------------
# P1 - the originator cookie is bound to the page that was LOADED, never to a form field
# ---------------------------------------------------------------------------

def test_a_cross_site_post_cannot_plant_the_originator_cookie_or_mail_anyone(mailbox):
    """The reviewed attack, step for step. The attacker starts their own sign-in as a client whose return
    address they control, then makes the VICTIM's browser post the attacker's request id and poll secret with
    the victim's address. Before the fix: the victim got a genuine email AND a cookie saying their browser
    started the sign-in, so their link skipped the match code."""
    attacker, victim = device(), device()
    client_id = register(attacker, [ATTACKER_REDIRECT])
    page = authorize(attacker, client_id, ATTACKER_REDIRECT)
    rid, poll = rid_of(page.text), poll_secret_of(page.text)

    forged = victim.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": poll, "email": unique_email("victim")})

    assert forged.status_code == 400, forged.text
    assert not [n for n in set_cookie_names(forged) if n.startswith("hl_oauth")], "a cookie was planted"
    assert not victim.cookies, "the victim's browser stored something"
    assert mailbox.sent == [], "the victim was mailed on the strength of a form they never loaded"


def test_the_victims_own_link_still_asks_for_the_code_after_the_forged_post(mailbox):
    """... and the end state the review demonstrated is gone: a link that reaches the victim any other way still
    asks them for the code only the starting page shows."""
    attacker, victim = device(), device()
    client_id = register(attacker, [ATTACKER_REDIRECT])
    victim_email = unique_email("victim")
    page = authorize(attacker, client_id, ATTACKER_REDIRECT)
    victim.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                                "email": victim_email})
    # the attacker, who really did start it, types the victim's address themselves (the older, known attack)
    sent = attacker.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                                         "email": victim_email})
    assert sent.status_code == 200
    magic = magic_of(mailbox.last_link)
    assert 'name="code"' in victim.get(f"/oauth/verify?t={magic}").text
    r = victim.post("/oauth/verify", data={"t": magic, "decision": "approve"})
    assert r.status_code == 400 and "location" not in r.headers


def test_the_cookie_is_set_by_the_page_that_is_loaded_and_is_not_script_readable():
    c = device()
    page = authorize(c, register(c))
    rid, poll = rid_of(page.text), poll_secret_of(page.text)
    header = next(h for h in page.headers.get_list("set-cookie") if h.startswith(orouter.cookie_name(rid) + "="))
    assert header.split(";")[0].split("=", 1)[1] == poll              # the secret the page's own form carries
    low = header.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/oauth" in low


def test_the_email_step_requires_the_cookie_and_the_form_to_agree(mailbox):
    c, stranger = device(), device()
    page = authorize(c, register(c))
    rid, poll = rid_of(page.text), poll_secret_of(page.text)
    addr = unique_email()
    assert stranger.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": poll, "email": addr}).status_code == 400   # no cookie
    assert c.post("/oauth/authorize/email", data={"rid": rid, "poll_secret": "z" * 24, "email": addr}).status_code == 400      # cookie says otherwise
    assert c.post("/oauth/authorize/email", data={"rid": rid, "email": addr}).status_code == 200                              # field omitted: the cookie is the proof
    assert len(mailbox.sent) == 1


def test_a_browser_that_blocks_cookies_is_told_why_rather_than_failing_silently(mailbox):
    c = device()
    page = authorize(c, register(c))
    c.cookies.clear()
    r = c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                               "email": unique_email()})
    assert r.status_code == 400 and "cookie" in r.text.lower() and mailbox.sent == []


def test_two_sign_ins_in_one_browser_do_not_break_each_other(mailbox):
    c = device()
    client_id = register(c)
    first, second = authorize(c, client_id), authorize(c, client_id)
    for page in (first, second):
        r = c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                                   "email": unique_email()})
        assert r.status_code == 200, r.text
    assert len(mailbox.sent) == 2


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                                      {"Origin": "https://evil.example.net"}])
def test_a_post_that_the_browser_says_came_from_another_site_is_refused_even_with_the_cookie(mailbox, headers):
    c = device()
    page = authorize(c, register(c))
    r = c.post("/oauth/authorize/email", headers=headers,
               data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text), "email": unique_email()})
    assert r.status_code == 403 and mailbox.sent == []


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin"},
                                      {"Sec-Fetch-Site": "same-origin", "Origin": "null"},     # no-referrer pages post Origin: null
                                      {"Origin": HTTPS_BASE}])
def test_the_pages_own_form_post_is_accepted_whatever_the_browser_sends(mailbox, headers):
    c = device()
    page = authorize(c, register(c))
    r = c.post("/oauth/authorize/email", headers=headers,
               data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text), "email": unique_email()})
    assert r.status_code == 200 and len(mailbox.sent) == 1


def test_a_cross_site_confirm_press_is_refused(mailbox):
    c = device()
    page = authorize(c, register(c))
    c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text), "email": unique_email()})
    magic = magic_of(mailbox.last_link)
    r = c.post("/oauth/verify", data={"t": magic, "decision": "approve"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert c.post("/oauth/verify", data={"t": magic, "decision": "approve"}).status_code == 303      # the real press still works


# ---------------------------------------------------------------------------
# P2 - one flooded endpoint must not reopen the others
# ---------------------------------------------------------------------------

def test_flooding_the_poll_endpoint_with_distinct_ids_cannot_reopen_the_email_ceiling():
    for _ in range(400):
        assert limits.check("email_global", "all")
    assert not limits.check("email_global", "all")
    for i in range(25000):
        limits.check("poll_request", f"rid-{i:08d}")
    assert not limits.check("email_global", "all"), "the global email ceiling was cleared by an unrelated flood"
    assert not limits.check("email_global", "all")


def test_every_limiter_keeps_its_counters_when_another_one_overflows():
    rl = limits.RateLimiter(max_keys_per_name=100)
    assert rl.allow("a:one", 1, 3600) and not rl.allow("a:one", 1, 3600)
    for i in range(1000):
        rl.allow(f"b:{i}", 1, 3600)
    assert not rl.allow("a:one", 1, 3600)


def test_overflowing_one_limiter_drops_its_own_oldest_keys_not_all_of_them():
    rl = limits.RateLimiter(max_keys_per_name=100)
    for i in range(150):
        rl.allow(f"b:{i}", 1, 3600)
    assert len(rl._tables["b"]) <= 100
    assert not rl.allow("b:149", 1, 3600), "the newest key was forgotten"
    assert rl.allow("b:0", 1, 3600), "the oldest key should have been the one dropped"


def test_the_poll_endpoint_has_a_per_address_ceiling_that_a_random_id_cannot_dodge():
    c = device()
    codes = []
    for i in range(limits.POLICY["poll_ip"][0] + 20):
        codes.append(c.post("/oauth/authorize/poll", json={"rid": f"{i:016d}", "poll_secret": "p" * 24}).status_code)
    assert 429 in codes and codes.index(429) <= limits.POLICY["poll_ip"][0] + 1


def test_a_real_waiting_page_polling_every_two_seconds_never_meets_the_ceiling():
    per_minute = 60 / 2
    assert limits.POLICY["poll_ip"][0] / (limits.POLICY["poll_ip"][1] / 60) >= 5 * per_minute


# ---------------------------------------------------------------------------
# P2 - the metadata-document fetch has a total deadline
# ---------------------------------------------------------------------------

class _Drip(httpx.AsyncByteStream):
    def __init__(self, gap):
        self.gap = gap

    async def __aiter__(self):
        while True:
            await asyncio.sleep(self.gap)
            yield b" "


class _DripTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Drip(0.02))


def test_a_host_that_drips_one_byte_at_a_time_is_cut_off_at_the_total_deadline(monkeypatch):
    monkeypatch.setattr(oclients, "FETCH_TOTAL_S", 0.4)

    async def resolver(host):
        return ["160.79.104.10"]
    fetcher = oclients.MetadataFetcher(resolver=resolver, transport=_DripTransport())

    async def go():
        t0 = time.monotonic()
        with pytest.raises(oclients.ClientError):
            await fetcher.get("https://slow.example.org/client.json")
        return time.monotonic() - t0
    assert asyncio.run(go()) < 3.0


def test_a_compressed_metadata_document_is_refused_outright(monkeypatch):
    """A 64 KiB cap on the decoded size is only as good as the decoder; ask for identity and refuse the rest."""
    seen = {}

    def handler(request):
        seen["accept-encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, content=b"{}", headers={"content-type": "application/json", "content-encoding": "gzip"})

    async def resolver(host):
        return ["160.79.104.10"]
    fetcher = oclients.MetadataFetcher(resolver=resolver, transport=httpx.MockTransport(handler))

    async def go():
        with pytest.raises(oclients.ClientError):
            await fetcher.get("https://zip.example.org/client.json")
    asyncio.run(go())
    assert seen["accept-encoding"] == "identity"


# ---------------------------------------------------------------------------
# P2 - the per-vendor fetch budget is spent by fetches, not by sign-ins
# ---------------------------------------------------------------------------

def _rotating_addresses(monkeypatch):
    counter = itertools.count(1)

    def fresh(request):
        n = next(counter)
        return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"
    monkeypatch.setattr(orouter, "_ip", fresh)


def test_a_hundred_sign_ins_for_one_cached_client_cause_one_fetch_and_are_all_accepted(monkeypatch, mailbox):
    _rotating_addresses(monkeypatch)
    _, calls = install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document()})
    c = device()
    statuses = []
    for _ in range(100):
        statuses.append(authorize(c, CLAUDE_CIMD, CLAUDE_CALLBACK).status_code)
    assert statuses == [200] * 100
    assert len(calls) == 1


def test_two_throwaway_addresses_cannot_use_up_the_budget_that_claude_needs(monkeypatch):
    """The reviewed lock-out: junk paths on a vendor's host are cache misses that DO leave the box, so they are
    what spends the host budget - and a document that was good a minute ago is still served when the budget is
    spent, instead of locking the real vendor out."""
    _rotating_addresses(monkeypatch)
    docs = {CLAUDE_CIMD: claude_document()}
    fetcher, calls = install_cimd(monkeypatch, docs)
    clock = {"now": 0.0}
    fetcher._clock = lambda: clock["now"]
    c = device()
    assert authorize(c, CLAUDE_CIMD, CLAUDE_CALLBACK).status_code == 200                  # fetched and cached
    for i in range(limits.POLICY["metadata_fetch_host"][0] + 5):                          # junk on the same host
        authorize(c, f"https://claude.ai/junk/{i}", CLAUDE_CALLBACK)
    clock["now"] += 1800                                                                 # past its freshness (600 s), inside the grace
    again = authorize(c, CLAUDE_CIMD, CLAUDE_CALLBACK)
    assert again.status_code == 200, "a previously good client was locked out by a flood of junk paths"


def test_a_cold_client_is_told_to_wait_when_the_budget_really_is_spent(monkeypatch):
    _rotating_addresses(monkeypatch)
    install_cimd(monkeypatch, {})
    c = device()
    codes = [authorize(c, f"https://flood.example.org/c/{i}", "https://flood.example.org/cb").status_code
             for i in range(limits.POLICY["metadata_fetch_host"][0] + 5)]
    assert 429 in codes and codes[0] == 400


def test_simultaneous_first_requests_for_one_client_share_one_fetch(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=json.dumps(claude_document()),
                              headers={"content-type": "application/json", "cache-control": "max-age=600"})

    async def resolver(host):
        await asyncio.sleep(0.05)
        return ["160.79.104.10"]
    fetcher = oclients.MetadataFetcher(resolver=resolver, transport=httpx.MockTransport(handler))

    async def go():
        return await asyncio.gather(*[fetcher.get(CLAUDE_CIMD) for _ in range(20)])
    docs = asyncio.run(go())
    assert len(docs) == 20 and len(calls) == 1


# ---------------------------------------------------------------------------
# P2 - an agent that cannot do OAuth keeps the readable answer
# ---------------------------------------------------------------------------

def rpc_call(tool="send_message", args=None):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}


@pytest.mark.parametrize("ua", ["python-httpx/0.27.0", "mcp-python-sdk/1.26", "node", "curl/8.4.0", "scanner-I/0.1",
                                 "Grok/1.0", "", "Mozilla/5.0 (compatible; MCP-Stats-Prober; +https://github.com/anthropics/x)"])
def test_a_keyless_agent_that_is_not_a_known_connector_gets_the_readable_answer_with_the_hint(ua):
    r = device(**{"User-Agent": ua} if ua else {}).post("/mcp", json=rpc_call())
    assert r.status_code == 200 and "www-authenticate" not in r.headers
    result = r.json()["result"]
    assert result["isError"] is True and "auth_required" in result["content"][0]["text"]
    assert result["_meta"]["mcp/www_authenticate"][0].startswith("Bearer resource_metadata=")


@pytest.mark.parametrize("ua", ["Claude-User", "claude-user/1.0", "claude-code/1.0.3", "Claude-AI/2"])
def test_the_known_oauth_connectors_get_the_401_they_start_sign_in_from(ua):
    r = device(**{"User-Agent": ua}).post("/mcp", json=rpc_call())
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer ")
    assert r.json()["result"]["isError"] is True                           # the body is the answer they always got


def test_the_connector_list_can_be_extended_without_a_deploy(monkeypatch):
    monkeypatch.setenv("OAUTH_CHALLENGE_401_CLIENTS", "newassistant, other-bot")
    assert device(**{"User-Agent": "NewAssistant/3.1"}).post("/mcp", json=rpc_call()).status_code == 401
    assert device(**{"User-Agent": "python-httpx/0.27"}).post("/mcp", json=rpc_call()).status_code == 200


@pytest.mark.parametrize("auth", ["Bearer not.a.token", "Bearer " + "x" * 80])
def test_a_client_that_sent_a_bearer_token_is_oauth_capable_whatever_it_calls_itself(auth):
    r = device(**{"User-Agent": "python-httpx/0.27.0", "Authorization": auth}).post("/mcp", json=rpc_call())
    assert r.status_code == 401


def test_the_official_sdk_without_an_auth_provider_still_receives_the_account_guidance():
    """The reviewer's repro: with the feature on, the SDK raised HTTPStatusError and never read the body that
    tells an agent how to get a key, buy credits or pay per call."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    import uvicorn
    import threading
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    async def go():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("send_message", {})
    try:
        result = asyncio.run(go())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert result.isError is True
    assert "auth_required" in result.content[0].text


# ---------------------------------------------------------------------------
# P2 - a name the app chose for itself is never printed in our email
# ---------------------------------------------------------------------------

def test_the_email_never_carries_the_name_a_dynamically_registered_app_chose(mailbox):
    evil = "URGENT: HatchLoop invoice unpaid - reply to billing-help@evil.example within 24h"
    c = device()
    client_id = register(c, name=evil)
    page = authorize(c, client_id)
    r = c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                               "email": unique_email("victim")})
    assert r.status_code == 200
    sent = mailbox.sent[-1]
    subject, html_body, text_body = emailer.compose(sent["link"], sent["app"], sent["host"])
    for part in (sent["app"], subject, html_body, text_body):
        assert "evil.example" not in part and "URGENT" not in part and "invoice" not in part
    assert sent["host"] == "assistant.example.org"                # the one fact that cannot be faked: where it returns


def test_a_metadata_document_client_is_named_by_its_host_in_the_email(monkeypatch, mailbox):
    install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document(client_name="Totally Not Claude <script>")})
    c = device()
    page = authorize(c, CLAUDE_CIMD, CLAUDE_CALLBACK)
    c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text), "email": unique_email()})
    assert mailbox.sent[-1]["app"] == "claude.ai"


# ---------------------------------------------------------------------------
# P3 - the off switch is an off switch; ceilings sized for assistants that call from shared addresses
# ---------------------------------------------------------------------------

def test_with_the_style_off_the_tool_list_is_not_annotated_either(monkeypatch):
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", "off")
    tools = device().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert tools and all("securitySchemes" not in t for t in tools)


def test_ceilings_on_server_to_server_endpoints_are_sized_for_a_vendors_shared_address():
    assert limits.POLICY["token_ip"][0] >= 3000
    assert limits.POLICY["register_ip"][0] >= 100
    assert limits.POLICY["register_global"][0] >= 1000
