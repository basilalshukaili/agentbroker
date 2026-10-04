"""The Caddyfile change that gives hatchloop.dev's OAuth protected-resource documents the right `resource`, as a pure text transform.

NOTHING HERE TOUCHES A SERVER. This turns the text of /etc/caddy/Caddyfile into the text it should become;
deploy/caddy/install_mcp_direct.py (`--change oauth_prm`) does the ssh. Like mcp_direct.py and mcp_retired.py it is
idempotent, refuses a Caddyfile that no longer looks like what it was written against, and is tested offline.

THE DEFECT (measured 2026-10-04, public GETs). hatchloop.dev/.well-known/* is already proxied to the origin: a Next.js
rewrite in web_hatchloop_v2/next.config.ts sends `/.well-known/:path*` to https://api.hatchloop.dev. The release-1
receipt said it "still goes to Next.js" and "finds no OAuth metadata"; it finds the metadata. What it finds is wrong
for the doors. Next.js's proxy replaces the Host header with the destination's, the origin builds the protected-resource
document's `resource` from Host (agent_interface/oauth/resources.py), and so:

    https://hatchloop.dev/.well-known/oauth-protected-resource/mcp/sanctions-screening
        answers  "resource": "https://api.hatchloop.dev/mcp/sanctions-screening"
        must be  "resource": "https://hatchloop.dev/mcp/sanctions-screening"

A client that connected to the site URL and validates `resource` against it (RFC 9728 section 3.3, and the MCP
authorization specification says it MUST) refuses the metadata and never offers a sign-in. The full server's URL,
https://hatchloop.dev/mcp/agent-broker, works only because the origin special-cases that one path. The 401 a connector
gets from a door carries an explicit metadata URL with `?host=hatchloop.dev` (oauth/resources.metadata_url), so that
path never relied on the rewrite; discovery by probing, which is what ChatGPT does when a connector is created, does.

THE CHOICE, AND WHY IT IS CADDY. Two fixes would work: add `?host=hatchloop.dev` to the Next.js rewrite, or route this one
document family from Caddy straight to the container with Host intact. Caddy, because
  * it is the established way this estate fixes the same defect (mcp_direct, mcp_retired: same installer, same probes, same
    automatic restore) and the only one with a rollback that does not need a site deploy;
  * the site is a separate repository that auto-deploys its working tree every 30 minutes, so an edit there ships on a
    timer rather than as a decision;
  * it takes the Next.js hop out of discovery: that hop returned 502s whenever the site restarted (0.26% of MCP POSTs, at
    :27-:28 and :57-:58 past the hour, before mcp_direct), and a Connect that fails at the first request looks like a
    server that does not exist;
  * the origin learns the host from the one header that cannot be forged by a query string.

SCOPE IS ONE DOCUMENT FAMILY, on purpose. The protected-resource document is the only discovery document that depends
on Host. Every other /.well-known/* path (mcp.json, agent-card.json, the authorization-server metadata, the glama and
x402 files) is Host-independent and keeps going where it goes today, through the Next.js rewrite, which this does not
touch and which stays as the fallback if this route is ever removed. No wildcard over /.well-known/, so a file the
site puts there later is not swallowed.

NO `header_up Host`: Caddy's reverse_proxy passes the incoming Host through unchanged, which is the point. A test pins
that nobody "fixes" it by rewriting Host.

It does not need the origin to be redeployed: build 48e8b62 already answers by Host (the property is pinned by
tests/unit/test_discovery_hygiene.py), so this can be applied on its own, before or after the next release.
"""
from __future__ import annotations

import re
from typing import Iterable

from mcp_direct import (  # noqa: F401  (re-exported: the installer treats every change alike)
    CaddyfileShapeError, ORIGIN, detect_eol, unified_diff, _only,
)

MARK_BEGIN = "# >>> oauth_prm 2026-10-04"
MARK_END = "# <<< oauth_prm"

PRM = "/.well-known/oauth-protected-resource"


def prm_block() -> str:
    return (
        f"\t{MARK_BEGIN}\n"
        "\t# OAUTH PROTECTED-RESOURCE DOCUMENTS, STRAIGHT TO THE CONTAINER WITH THE HOST HEADER INTACT.\n"
        "\t# The Next.js rewrite that proxies /.well-known/* replaces Host with api.hatchloop.dev, and the origin builds\n"
        "\t# the document's `resource` from Host, so a door's metadata named the API host instead of the URL the client\n"
        "\t# connected to (RFC 9728 requires them to match). Only this one family is routed here: it is the only\n"
        "\t# /.well-known document that depends on Host. No wildcard over /.well-known/, no `header_up Host`.\n"
        f"\t@oauth_prm path {PRM} {PRM}/*\n"
        "\thandle @oauth_prm {\n"
        f"\t\treverse_proxy {ORIGIN} {{\n"
        "\t\t\theader_up X-Forwarded-Proto https\n"
        "\t\t\theader_up -X-Real-IP\n"
        "\t\t}\n"
        "\t}\n"
        f"\t{MARK_END}\n"
        "\n"
    )


def is_applied(text: str) -> bool:
    return MARK_BEGIN in text


def apply(text: str, doors: Iterable[str] = ()) -> str:
    """The Caddyfile text with the protected-resource route added. Idempotent. Raises CaddyfileShapeError (and
    changes nothing) if the anchors are missing or ambiguous. `doors` is accepted so the installer can treat every
    change alike; this change does not depend on them."""
    eol = detect_eol(text)
    s = text.replace("\r\n", "\n")
    if is_applied(s):
        if s.count(MARK_BEGIN) != 1 or s.count(MARK_END) != 1 or "@oauth_prm" not in s:
            raise CaddyfileShapeError("oauth_prm is PARTIALLY applied; refusing to add to a half-edited file")
        return text

    snippet = _only(s, r"^\(hatchloop_site\) \{\n.*?^\}\n", "(hatchloop_site) snippet", re.S | re.M)
    body = snippet.group(0)
    catch_all = _only(
        body, r"^\thandle \{\n\t\treverse_proxy 127\.0\.0\.1:3000 \{\n\t\t\theader_up X-Forwarded-Proto https\n\t\t\}\n\t\}\n",
        "hatchloop.dev catch-all handle -> 127.0.0.1:3000", re.M)
    new_body = body[:catch_all.start()] + prm_block() + body[catch_all.start():]
    s = s[:snippet.start()] + new_body + s[snippet.end():]
    return s.replace("\n", eol) if eol != "\n" else s


# What must be true after a reload, probed from OUTSIDE with GETs and one handshake (never a tool call that does work,
# never a message, never a call). Each: (method, url, json body or None, status, substring or "").
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "oauth-prm-verify", "version": "1"}}}


def _resource(host: str, path: str) -> str:
    return f'"resource":"https://{host}{path}"'


def post_checks(doors: Iterable[str]) -> list:
    doors = sorted(doors)
    checks: list = [
        # THE FIX: the site URL of each endpoint is named as itself.
        ("GET", f"https://hatchloop.dev{PRM}/mcp/agent-broker", None, 200, _resource("hatchloop.dev", "/mcp/agent-broker")),
    ]
    for d in doors:
        checks.append(("GET", f"https://hatchloop.dev{PRM}/mcp/{d}", None, 200, _resource("hatchloop.dev", f"/mcp/{d}")))
    checks += [
        ("GET", f"https://hatchloop.dev{PRM}", None, 200, _resource("hatchloop.dev", "")),
        # What must NOT have moved: the API host still names itself, the other well-known documents still answer through
        # the site, and the site, the MCP door and the company site are up.
        ("GET", f"https://api.hatchloop.dev{PRM}/mcp", None, 200, _resource("api.hatchloop.dev", "/mcp")),
        ("GET", f"https://api.hatchloop.dev{PRM}/mcp/{doors[0]}", None, 200, _resource("api.hatchloop.dev", f"/mcp/{doors[0]}")),
        ("GET", "https://hatchloop.dev/.well-known/oauth-authorization-server", None, 200, '"issuer":"https://api.hatchloop.dev"'),
        ("GET", "https://hatchloop.dev/.well-known/mcp.json", None, 200, '"name":"agent-broker"'),
        ("GET", "https://hatchloop.dev/.well-known/agent-card.json", None, 200, ""),
        ("POST", "https://hatchloop.dev/mcp/agent-broker", INIT, 200, '"serverInfo"'),
        ("GET", "https://hatchloop.dev/", None, 200, ""),
        ("GET", "https://api.hatchloop.dev/health", None, 200, "healthy"),
        ("GET", "https://techmate.om/", None, 200, ""),
    ]
    return checks
