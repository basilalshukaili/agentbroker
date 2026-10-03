"""The official MCP Python SDK's OAuth client, run against this server over real HTTP.

Everything else in the OAuth tests is the server talking to our own idea of a client. This file removes our
idea from the loop: `mcp.client.auth.OAuthClientProvider` - the reference implementation of MCP authorization
that Claude Code, Cursor-style clients and agent frameworks are built on - does its own discovery, its own
registration, its own PKCE and its own token handling against a uvicorn server running the real app on
loopback. If our discovery documents, our 401, our registration response, our authorization redirect or our
token response were subtly off in a way only a spec-driven client notices, this is where it fails.

What the SDK does that these tests make it do:
  * call a protected tool anonymously, receive the 401, read `resource_metadata` out of WWW-Authenticate,
    fetch the protected-resource document, check its `resource` against the URL it connected to, fetch the
    authorization-server metadata, register (or, in the second test, identify itself by metadata document),
    send the person to /oauth/authorize with a PKCE challenge and a `resource`, receive `code`/`state`/`iss`
    on its callback, exchange the code, and RETRY THE SAME CALL with the bearer token;
  * later, find its access token expired and refresh it, receiving a rotated refresh token.

The "browser" is an httpx client that does what a person does: open the page, type an address, open the
mailed link, press Confirm. The mailbox is a fake; nothing leaves the machine.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("mcp.client.auth")

from mcp import ClientSession  # noqa: E402
from mcp.client.auth import OAuthClientProvider  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
from agent_interface.identity import validate_token  # noqa: E402
from agent_interface.oauth import limits  # noqa: E402
from agent_interface.oauth.store import MemoryStore, set_store  # noqa: E402
from tests.oauth_support import (  # noqa: E402
    CLAUDE_CALLBACK, CLAUDE_CIMD, claude_document, install_cimd, install_mailbox, magic_of, poll_secret_of, rid_of,
)


class Storage:
    def __init__(self):
        self.tokens: OAuthToken | None = None
        self.client: OAuthClientInformationFull | None = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, info):
        self.client = info


@pytest.fixture
def server(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("OAUTH_ISSUER", base)
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    set_store(MemoryStore())
    limits.LIMITS.reset()
    main._rl_buckets.clear()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "the test server did not start"
    yield base
    srv.should_exit = True
    t.join(timeout=10)
    set_store(None)


class Person:
    """Does what a person does when the SDK sends them to the authorization URL."""

    def __init__(self, base, mailbox, email="sdk.person@example.org"):
        self.base, self.mailbox, self.email = base, mailbox, email
        self.visits = []
        self.callback = None

    async def redirect_handler(self, auth_url: str) -> None:
        self.visits.append(auth_url)
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as browser:
            page = await browser.get(auth_url)
            assert page.status_code == 200, page.text
            assert "Email me a sign-in link" in page.text
            sent = await browser.post(f"{self.base}/oauth/authorize/email", data={
                "rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text), "email": self.email})
            assert sent.status_code == 200 and "Check your email" in sent.text
            link = self.mailbox.last_link
            assert (await browser.get(link)).status_code == 200            # the mailed link, opened
            done = await browser.post(f"{self.base}/oauth/verify", data={"t": magic_of(link), "decision": "approve"})
            assert done.status_code == 303, done.text                       # same browser: handed straight back
            self.callback = done.headers["location"]

    async def callback_handler(self):
        q = parse_qs(urlsplit(self.callback).query)
        assert q["iss"][0] == self.base                                     # RFC 9207: the issuer rides the response
        return q["code"][0], q.get("state", [None])[0]


def _metadata(redirect="http://localhost:8765/callback"):
    return OAuthClientMetadata(client_name="SDK conformance", redirect_uris=[AnyUrl(redirect)],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                               token_endpoint_auth_method="none")


def test_the_official_sdk_discovers_registers_signs_in_and_retries_the_same_call(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    person, storage = Person(server, mailbox), Storage()
    provider = OAuthClientProvider(f"{server}/mcp", _metadata(), storage, person.redirect_handler, person.callback_handler)

    async def scenario():
        async with streamablehttp_client(f"{server}/mcp", auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {t.name for t in tools}
                assert "get_conversation" in names and "screen_sanctions" in names
                assert person.visits == [], "listing tools must not require sign-in"
                free = await session.call_tool("check_quota", {})
                assert not free.isError and person.visits == [], "a keyless tool must not require sign-in"

                # the protected call: 401 -> discovery -> registration -> sign-in -> exchange -> SAME call retried
                result = await session.call_tool("get_conversation", {"reference": "1234", "business_number": "+15550001111"})
                assert len(person.visits) == 1 and len(mailbox.sent) == 1
                text = result.content[0].text
                assert "identity_required" not in text and "auth_required" not in text, text[:300]
    asyncio.run(scenario())

    assert storage.client is not None and storage.client.client_id.startswith("dcr_")
    assert storage.tokens and storage.tokens.token_type.lower() == "bearer" and storage.tokens.refresh_token
    v = validate_token(storage.tokens.access_token)
    assert v.valid and v.identity.agent_id.startswith("free_")
    assert 3000 < storage.tokens.expires_in <= 3600
    visit = parse_qs(urlsplit(person.visits[0]).query)
    assert visit["code_challenge_method"] == ["S256"] and visit["resource"] == [f"{server}/mcp"]   # RFC 8707, sent by the SDK


def test_the_sdk_refreshes_an_expired_token_and_the_spent_refresh_token_is_dead(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    person, storage = Person(server, mailbox), Storage()
    provider = OAuthClientProvider(f"{server}/mcp", _metadata(), storage, person.redirect_handler, person.callback_handler)
    args = {"reference": "1234", "business_number": "+15550001111"}
    first = {}

    async def scenario():
        async with streamablehttp_client(f"{server}/mcp", auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("get_conversation", args)
                first["refresh"] = storage.tokens.refresh_token
                first["access"] = storage.tokens.access_token
                provider.context.token_expiry_time = time.time() - 60          # the access token "expired"
                again = await session.call_tool("get_conversation", args)
                assert "identity_required" not in again.content[0].text
    asyncio.run(scenario())

    assert len(person.visits) == 1, "refreshing must not send the person through sign-in again"
    assert storage.tokens.refresh_token != first["refresh"] and storage.tokens.access_token != first["access"]
    assert validate_token(storage.tokens.access_token).valid

    # the refresh token the SDK spent is dead - and presenting it kills the chain the SDK now holds
    replay = httpx.post(f"{server}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh"],
                                                       "client_id": storage.client.client_id})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    after = httpx.post(f"{server}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": storage.tokens.refresh_token,
                                                      "client_id": storage.client.client_id})
    assert after.json()["error"] == "invalid_grant"


def test_the_sdk_identifies_itself_by_metadata_document_when_the_server_advertises_support(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    _, calls = install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document()})
    person, storage = Person(server, mailbox), Storage()
    provider = OAuthClientProvider(f"{server}/mcp", _metadata(CLAUDE_CALLBACK), storage, person.redirect_handler,
                                   person.callback_handler, client_metadata_url=CLAUDE_CIMD)

    async def scenario():
        async with streamablehttp_client(f"{server}/mcp", auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_conversation", {"reference": "1234", "business_number": "+15550001111"})
                assert "identity_required" not in result.content[0].text
    asyncio.run(scenario())

    assert storage.client.client_id == CLAUDE_CIMD              # no registration happened: the URL IS the client id
    assert len(calls) >= 1 and calls[0].headers["host"] == "claude.ai"
    assert validate_token(storage.tokens.access_token).valid
    assert mailbox.sent[0]["app"] == "claude.ai"
