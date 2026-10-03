"""The Caddyfile change that sends the retired MCP doors to the AgentBroker origin, as a pure text transform.

NOTHING HERE TOUCHES A SERVER. This turns the text of /etc/caddy/Caddyfile into the text it should become;
deploy/caddy/install_mcp_direct.py (`--change retired`) does the ssh. Like mcp_direct.py it is idempotent,
it refuses a Caddyfile that no longer looks like what it was written against, and it can be tested offline.

WHAT IT CHANGES. `https://hatchloop.dev/mcp/<retired>` (and `/mcp/<retired>/mcp`, with or without a trailing
slash) goes to the container on 127.0.0.1:8010 instead of the Next.js site. The origin answers a retired door
with a real MCP tombstone (agent_interface/retired_doors.py): a handshake that says RETIRED, one tool that
returns the live server's address, and 410 Gone for GET/HEAD. The site keeps every other path.

WHY IT IS A SEPARATE STEP FROM mcp_direct. That change is already applied on the live box (marker
`mcp_direct 2026-10-01`), and `mcp_direct.apply` returns an applied file untouched; the retired doors could
not ride along. This adds its own marker pair, so applying it twice, or after mcp_direct, changes nothing the
second time.

IT MUST BE APPLIED AFTER THE ORIGIN IS DEPLOYED, not before: until the origin knows these doors it answers
them `404 no such capability endpoint`, which is worse than the site's 410. `post_checks` below would fail
and the installer would restore the backup - but deploy in the right order and it never needs to.

DO NOT WIDEN THE MATCHER. The path list is explicit, one slug at a time, for the same reason mcp_direct's
is: `/mcp/*` would swallow every page the site serves under /mcp/ and change what visitors see.
"""
from __future__ import annotations

import re
from typing import Iterable

from mcp_direct import (  # noqa: F401  (re-exported: the installer treats both changes alike)
    CaddyfileShapeError, ORIGIN, detect_eol, unified_diff, _only,
)

MARK_BEGIN = "# >>> mcp_retired 2026-10-03"
MARK_END = "# <<< mcp_retired"


def _paths(slugs: Iterable[str]) -> str:
    out = []
    for d in slugs:
        out += [f"/mcp/{d}", f"/mcp/{d}/", f"/mcp/{d}/mcp", f"/mcp/{d}/mcp/"]
    return " ".join(out)


def retired_block(slugs: Iterable[str]) -> str:
    slugs = sorted(slugs)
    if not slugs:
        raise ValueError("no retired doors given; refusing to emit a block that would route none of them")
    return (
        f"\t{MARK_BEGIN}\n"
        "\t# RETIRED MCP DOORS: answered by the AgentBroker origin (agent_interface/retired_doors.py), not the site.\n"
        "\t# Directory scorers probed three of these ~440 times a day; the site answered 410 with a body that is not\n"
        "\t# a JSON-RPC message. The origin answers MCP (a handshake that says RETIRED, one tombstone tool) and\n"
        "\t# keeps 410 Gone for GET/HEAD. Same public URLs; only who answers changes.\n"
        "\t#\n"
        "\t# Explicit paths, never a /mcp/* wildcard (that would swallow the site's own pages under /mcp/).\n"
        "\t# The origin has no trailing-slash route, so the slash is stripped first (as mcp_direct does).\n"
        f"\t@mcp_retired path {_paths(slugs)}\n"
        "\thandle @mcp_retired {\n"
        "\t\turi strip_suffix /\n"
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


def apply(text: str, slugs: Iterable[str]) -> str:
    """The Caddyfile text with the retired-door route added. Idempotent. Raises CaddyfileShapeError (and
    changes nothing) if the anchors are missing or ambiguous."""
    slugs = list(slugs)
    eol = detect_eol(text)
    s = text.replace("\r\n", "\n")
    if is_applied(s):
        if s.count(MARK_BEGIN) != 1 or s.count(MARK_END) != 1 or "@mcp_retired" not in s:
            raise CaddyfileShapeError("mcp_retired is PARTIALLY applied; refusing to add to a half-edited file")
        return text

    snippet = _only(s, r"^\(hatchloop_site\) \{\n.*?^\}\n", "(hatchloop_site) snippet", re.S | re.M)
    body = snippet.group(0)
    catch_all = _only(
        body, r"^\thandle \{\n\t\treverse_proxy 127\.0\.0\.1:3000 \{\n\t\t\theader_up X-Forwarded-Proto https\n\t\t\}\n\t\}\n",
        "hatchloop.dev catch-all handle -> 127.0.0.1:3000", re.M)
    new_body = body[:catch_all.start()] + retired_block(slugs) + body[catch_all.start():]
    s = s[:snippet.start()] + new_body + s[snippet.end():]
    return s.replace("\n", eol) if eol != "\n" else s


# What must be true after a reload (probed from OUTSIDE, handshakes and GETs only - never a tool call that
# does work, never a message, never a call). Each: (method, url, json body or None, status, substring or "").
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "mcp-retired-verify", "version": "1"}}}


def post_checks(slugs: Iterable[str]) -> list:
    slugs = sorted(slugs)
    checks: list = []
    for d in slugs:
        checks += [
            ("POST", f"https://hatchloop.dev/mcp/{d}", INIT, 200, "(RETIRED)"),
            ("POST", f"https://hatchloop.dev/mcp/{d}/", INIT, 200, "(RETIRED)"),
            ("POST", f"https://hatchloop.dev/mcp/{d}/mcp", INIT, 200, "(RETIRED)"),
            ("GET", f"https://hatchloop.dev/mcp/{d}", None, 410, "server_retired"),
        ]
    checks += [
        # What must NOT have moved: the live server, a live door, the site.
        ("POST", "https://hatchloop.dev/mcp/agent-broker", INIT, 200, '"serverInfo"'),
        ("POST", "https://hatchloop.dev/mcp/sanctions-screening", INIT, 200, '"serverInfo"'),
        ("GET", "https://hatchloop.dev/", None, 200, ""),
        ("GET", "https://api.hatchloop.dev/health", None, 200, "healthy"),
        ("GET", "https://techmate.om/", None, 200, ""),
    ]
    return checks
