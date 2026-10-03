"""Shared scaffolding for the OAuth tests: a browser, a fake mailbox, PKCE, a fake client-metadata host.

Nothing here talks to the network. The mailbox replaces the one function that would call Resend; the metadata
host replaces the transport the SSRF-safe fetcher uses; the "browser" is a TestClient with a cookie jar.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx

from agent_interface.oauth import clients as oclients
from agent_interface.oauth import emailer

HTTPS_BASE = "https://api.hatchloop.dev"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CIMD = "https://claude.ai/oauth/mcp-oauth-client-metadata"


def pkce():
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def unique_email(tag="user"):
    return f"{tag}.{secrets.token_hex(6)}@example.org"


class Mailbox:
    """Replaces emailer.send_signin_link. `outcome` can be set to emailer.REJECTED / UNAVAILABLE."""

    def __init__(self):
        self.sent = []
        self.outcome = emailer.SENT

    async def send(self, to_email, link, app_label, return_host):
        if self.outcome == emailer.SENT:
            self.sent.append({"to": to_email, "link": link, "app": app_label, "host": return_host})
        return self.outcome

    @property
    def last_link(self):
        return self.sent[-1]["link"]


def install_mailbox(monkeypatch) -> Mailbox:
    box = Mailbox()
    monkeypatch.setattr(emailer, "send_signin_link", box.send)
    return box


def install_cimd(monkeypatch, documents: dict, calls: list = None):
    """Make `oclients.FETCHER` serve `documents` (url -> JSON-able) from a pretend public host."""
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = f"https://{request.headers['host']}{request.url.path}"
        doc = documents.get(url)
        if doc is None:
            return httpx.Response(404, json={"error": "nope"})
        return httpx.Response(200, content=json.dumps(doc), headers={"content-type": "application/json",
                                                                      "cache-control": "max-age=600"})

    async def resolver(host):
        return ["160.79.104.10"]

    fetcher = oclients.MetadataFetcher(resolver=resolver, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(oclients, "FETCHER", fetcher)
    return fetcher, calls


def claude_document(**over):
    doc = {"client_id": CLAUDE_CIMD, "client_name": "Claude", "client_uri": "https://claude.ai",
           "redirect_uris": [CLAUDE_CALLBACK, "http://localhost/callback", "http://127.0.0.1/callback"],
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "token_endpoint_auth_method": "none"}
    doc.update(over)
    return doc


_RID = re.compile(r'name="rid" value="([^"]+)"')
_POLL = re.compile(r'name="poll_secret" value="([^"]+)"')


def rid_of(html_text: str) -> str:
    return _RID.search(html_text).group(1)


def poll_secret_of(html_text: str) -> str:
    return _POLL.search(html_text).group(1)


_MATCH = re.compile(r'id="match-code">(\d{4})<')


def match_code_of(html_text: str) -> str:
    return _MATCH.search(html_text).group(1)


def code_of(location: str) -> str:
    return parse_qs(urlsplit(location).query)["code"][0]


def query_of(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def magic_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["t"][0]
