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

The SDK's shared dependency versions conflict with requirements.txt. Install mcp==1.26.0 in a separate
venv and set MCP_SDK_PYTHON to its Python. Only the client runs there; the real server and its fixtures
stay in the production environment. Missing or broken clients fail, never skip, these tests.
"""
from __future__ import annotations

import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import uvicorn
from tests.oauth_sdk_bridge import run_sdk

import config  # noqa: E402
import main  # noqa: E402
from agent_interface.identity import validate_token  # noqa: E402
from agent_interface.oauth import limits  # noqa: E402
from agent_interface.oauth.store import MemoryStore, set_store  # noqa: E402
from tests.oauth_support import (  # noqa: E402
    CLAUDE_CALLBACK, CLAUDE_CIMD, claude_document, install_cimd, install_mailbox, magic_of, poll_secret_of, rid_of,
)


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
    try:
        yield base
    finally:
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


def test_the_official_sdk_discovers_registers_signs_in_and_retries_the_same_call(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    person = Person(server, mailbox)
    result = run_sdk(server, "register", person)
    tokens = result["tokens"]
    assert len(person.visits) == 1 and len(mailbox.sent) == 1
    assert result["client"]["client_id"].startswith("dcr_")
    assert tokens["token_type"].lower() == "bearer" and tokens["refresh_token"]
    v = validate_token(tokens["access_token"])
    assert v.valid and v.identity.agent_id.startswith("free_")
    assert 3000 < tokens["expires_in"] <= 3600
    visit = parse_qs(urlsplit(person.visits[0]).query)
    assert visit["code_challenge_method"] == ["S256"] and visit["resource"] == [f"{server}/mcp"]   # RFC 8707, sent by the SDK


def test_the_sdk_refreshes_an_expired_token_and_the_spent_refresh_token_is_dead(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    person = Person(server, mailbox)
    result = run_sdk(server, "refresh", person)
    tokens, first = result["tokens"], result["first"]

    assert len(person.visits) == 1, "refreshing must not send the person through sign-in again"
    assert tokens["refresh_token"] != first["refresh"] and tokens["access_token"] != first["access"]
    assert validate_token(tokens["access_token"]).valid

    # the refresh token the SDK spent is dead - and presenting it kills the chain the SDK now holds
    replay = httpx.post(f"{server}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh"],
                                                       "client_id": result["client"]["client_id"]})
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    after = httpx.post(f"{server}/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                                      "client_id": result["client"]["client_id"]})
    assert after.json()["error"] == "invalid_grant"


def test_the_sdk_identifies_itself_by_metadata_document_when_the_server_advertises_support(server, monkeypatch):
    mailbox = install_mailbox(monkeypatch)
    _, calls = install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document()})
    person = Person(server, mailbox)
    result = run_sdk(server, "cimd", person, redirect_uri=CLAUDE_CALLBACK, cimd=CLAUDE_CIMD)

    assert result["client"]["client_id"] == CLAUDE_CIMD        # no registration happened: the URL IS the client id
    assert len(calls) >= 1 and calls[0].headers["host"] == "claude.ai"
    assert validate_token(result["tokens"]["access_token"]).valid
    assert mailbox.sent[0]["app"] == "claude.ai"
