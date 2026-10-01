"""The Caddyfile change that takes /mcp off the Next.js proxy path, as pure text transforms.

NOTHING HERE TOUCHES A SERVER. This module turns the text of /etc/caddy/Caddyfile into the text it
should become; deploy/caddy/install_mcp_direct.py does the ssh. Keeping the edit pure is what lets it be
tested offline against a copy of the live file, and re-applied safely (it is idempotent).

WHAT IT CHANGES (key-holder audit 2026-09-30, fixes 4, 5 and 6):

1. hatchloop.dev/mcp/* goes straight to the AgentBroker container on 127.0.0.1:8010.
   Today those requests go Caddy -> Next.js (:3000) -> a rewrite that calls https://api.hatchloop.dev
   -> Caddy -> the container. Measured over four days of logs: 141 of 54,053 POSTs (0.26%) were 502,
   clustered at :27-:28 and :57-:58 past the hour (the site's auto-deploy restarting Next.js), and every
   caller reached the origin as the box's own address, so they all shared ONE rate-limit bucket.

   The PUBLIC URLs stay byte-for-byte identical. Only the paths Next.js already rewrites are routed:
   /mcp/agent-broker (served by the origin as /mcp) and the capability doors named in
   agent_interface/profiles.PROFILES. Everything else under /mcp/ - the human pages for retired
   servers that answer 410 from Next.js - keeps going to Next.js, because "/mcp/*" as a wildcard would
   have swallowed them and changed what visitors see.

   Caddy is deliberately NOT given `trusted_proxies`: with none configured it discards whatever
   X-Forwarded-For a client sent and sets the real peer address, which the app then believes only
   because the peer (this Caddy, on the Docker bridge) is one of ITS trusted proxies
   (core/client_ip.py). X-Real-IP is stripped on the way in for the same reason.

2. The log filters redact the documented key header. Caddy redacts `Authorization` by default and NOT
   `X-Agent-Identity`, which is the header our manifests, docs and every key email tell people to use.
   The first real key presented to hatchloop.dev would have been written to disk in clear text.
   `X-Api-Key` (the other header hosted connectors may send) is redacted too.

3. api.hatchloop.dev, which had NO access log at all, gets one with the same filter - so "who is
   calling the origin and how does it answer" has a record on the host that carries most of the traffic.

Idempotent: every inserted block carries a marker pair; running it on its own output changes nothing.
"""
from __future__ import annotations

import difflib
import re
from typing import Iterable

MARK_BEGIN = "# >>> mcp_direct 2026-10-01"
MARK_END = "# <<< mcp_direct"
LOG_MARK = "# mcp_direct: key headers never reach disk"

ORIGIN = "127.0.0.1:8010"
API_LOG = "/var/log/caddy/api.hatchloop.dev.log"


class CaddyfileShapeError(RuntimeError):
    """The live file no longer looks like what this edit was written against. Nothing is changed."""


def detect_eol(text: str) -> str:
    # Derived from the file, never assumed: a multi-line anchor built with bare "\n" matches nothing
    # in a CRLF file, while a single "\n" matches half of it (this estate's CLAUDE.md, 2026-09-28).
    return "\r\n" if "\r\n" in text else "\n"


def _paths(doors: Iterable[str]) -> str:
    out = []
    for d in doors:
        out += [f"/mcp/{d}", f"/mcp/{d}/"]
    return " ".join(out)


def mcp_block(doors: Iterable[str]) -> str:
    doors = sorted(doors)
    if not doors:
        raise ValueError("no capability doors given; refusing to emit a block that would route none of them")
    proxy = (
        f"\t\treverse_proxy {ORIGIN} {{\n"
        "\t\t\theader_up X-Forwarded-Proto https\n"
        "\t\t\theader_up -X-Real-IP\n"
        "\t\t}\n"
    )
    return (
        f"\t{MARK_BEGIN}\n"
        "\t# MCP STRAIGHT TO THE CONTAINER, NOT THROUGH NEXT.JS.\n"
        "\t# These were Next.js rewrites to https://api.hatchloop.dev: Caddy -> Next -> Caddy -> container.\n"
        "\t# 141 of 54,053 POSTs (0.26%) were 502 over four days, at :27-:28 and :57-:58 past the hour\n"
        "\t# (the site's auto-deploy restarting Next), and every caller reached the origin as this box's own\n"
        "\t# address, i.e. one shared rate-limit bucket. Same public URLs; only the hop is removed.\n"
        "\t#\n"
        "\t# ONLY the paths Next.js rewrote are routed (not a /mcp/* wildcard): the retired servers'\n"
        "\t# pages under /mcp/ answer 410 from Next.js and must keep doing so.\n"
        "\t#\n"
        "\t# NO trusted_proxies on purpose: with none, Caddy replaces any client-sent X-Forwarded-For with\n"
        "\t# the real peer address. The app believes that header only from its own proxy\n"
        "\t# (core/client_ip.py, TRUSTED_PROXY_CIDRS), and X-Real-IP is stripped here.\n"
        "\t@mcp_agent_broker path /mcp/agent-broker /mcp/agent-broker/\n"
        "\thandle @mcp_agent_broker {\n"
        "\t\trewrite * /mcp\n"
        + proxy +
        "\t}\n"
        f"\t@mcp_doors path {_paths(doors)}\n"
        "\thandle @mcp_doors {\n"
        # The origin has no trailing-slash route: /mcp/<door>/ would be answered 307 with an http://
        # Location, where the Next.js rewrite this replaces answers 200. Strip the slash BEFORE proxying
        # so both spellings keep behaving as they do today (gate finding, P2).
        "\t\turi strip_suffix /\n"
        + proxy +
        "\t}\n"
        f"\t{MARK_END}\n"
        "\n"
    )


_FILTER_ADDITION = (
    f"\t\t\t{LOG_MARK}\n"
    "\t\t\t# Caddy redacts Authorization by default and NOT these two. X-Agent-Identity is the header\n"
    "\t\t\t# every key we have issued tells people to send.\n"
    "\t\t\trequest>headers>X-Agent-Identity replace REDACTED\n"
    "\t\t\trequest>headers>X-Api-Key replace REDACTED\n"
)

_API_LOG = (
    "\n"
    f"\t{MARK_BEGIN} (api access log)\n"
    "\t# api.hatchloop.dev had NO access log: the host that carries most MCP traffic left no record of\n"
    "\t# who called or how it answered. Same rotation as the other sites, same key-header redaction.\n"
    "\t# `caddy validate` creates this file as whoever runs it; the installer chowns it to caddy\n"
    "\t# before the reload (a root-owned log makes a valid config fail to reload).\n"
    "\tlog {\n"
    f"\t\toutput file {API_LOG} {{\n"
    "\t\t\troll_size 20MiB\n"
    "\t\t\troll_keep 10\n"
    "\t\t}\n"
    "\t\tformat filter {\n"
    "\t\t\twrap json\n"
    f"\t\t\t{LOG_MARK}\n"
    "\t\t\trequest>headers>X-Agent-Identity replace REDACTED\n"
    "\t\t\trequest>headers>X-Api-Key replace REDACTED\n"
    # This host serves the emailed verification link (/keys/verify?token=...), the unsubscribe link
    # (/unsubscribe?t=...) and the WhatsApp handshake (hub.verify_token); one-time tokens must not
    # reach disk - the same rule the hatchloop.dev log applies to b= and t= (gate finding, P3).
    "\t\t\trequest>uri query {\n"
    "\t\t\t\tdelete token\n"
    "\t\t\t\tdelete t\n"
    "\t\t\t\tdelete b\n"
    "\t\t\t\tdelete hub.verify_token\n"
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}\n"
    f"\t{MARK_END}\n"
)


def _only(text: str, pattern: str, what: str, flags: int = 0) -> "re.Match[str]":
    found = list(re.finditer(pattern, text, flags))
    if len(found) != 1:
        raise CaddyfileShapeError(
            f"expected exactly one {what} in the Caddyfile, found {len(found)}; "
            "the file has changed shape since this edit was written - refusing to guess")
    return found[0]


def is_applied(text: str) -> bool:
    return MARK_BEGIN in text


def apply(text: str, doors: Iterable[str]) -> str:
    """The Caddyfile text with all three edits applied. Idempotent. Raises CaddyfileShapeError (and
    changes nothing) if an anchor is missing or ambiguous."""
    eol = detect_eol(text)
    s = text.replace("\r\n", "\n")
    if is_applied(s):
        # Partial application is the one state worth refusing: it means someone edited by hand.
        needed = (MARK_BEGIN + "\n", "@mcp_agent_broker", "@mcp_doors", LOG_MARK)
        if not all(n in s for n in needed) or s.count(LOG_MARK) != 2 or s.count(MARK_END) != 2:
            raise CaddyfileShapeError("mcp_direct is PARTIALLY applied; refusing to add to a half-edited file")
        return text

    # --- 1. hatchloop_site snippet: MCP handles go BEFORE the catch-all `handle {` -----------------
    snippet = _only(s, r"^\(hatchloop_site\) \{\n.*?^\}\n", "(hatchloop_site) snippet", re.S | re.M)
    body = snippet.group(0)
    catch_all = _only(
        body, r"^\thandle \{\n\t\treverse_proxy 127\.0\.0\.1:3000 \{\n\t\t\theader_up X-Forwarded-Proto https\n\t\t\}\n\t\}\n",
        "hatchloop.dev catch-all handle -> 127.0.0.1:3000", re.M)
    new_body = body[:catch_all.start()] + mcp_block(doors) + body[catch_all.start():]

    # --- 2. hatchloop_site log filter ---------------------------------------------------------
    wrap = _only(new_body, r"^\t\tformat filter \{\n\t\t\twrap json\n", "hatchloop_site log filter", re.M)
    new_body = new_body[:wrap.end()] + _FILTER_ADDITION + new_body[wrap.end():]
    s = s[:snippet.start()] + new_body + s[snippet.end():]

    # --- 3. api.hatchloop.dev gets an access log ------------------------------------------------
    api = _only(s, r"^api\.hatchloop\.dev \{\n.*?^\}\n", "api.hatchloop.dev site block", re.S | re.M)
    api_body = api.group(0)
    if re.search(r"^\tlog \{", api_body, re.M):
        raise CaddyfileShapeError("api.hatchloop.dev already has a log block; not adding a second one")
    closing = api_body.rindex("}\n")
    new_api = api_body[:closing].rstrip("\n") + "\n" + _API_LOG + api_body[closing:]
    s = s[:api.start()] + new_api + s[api.end():]

    return s.replace("\n", eol) if eol != "\n" else s


def unified_diff(old: str, new: str, name: str = "Caddyfile", context: int = 3) -> str:
    """`context=0` gives an additions-only diff that quotes none of the live file - the form that is
    safe to commit to a public repo (the live Caddyfile's comments describe internal systems)."""
    a = old.replace("\r\n", "\n").splitlines(keepends=True)
    b = new.replace("\r\n", "\n").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}", n=context))


# ---------------------------------------------------------------------------
# What must still be true after a reload (probed from OUTSIDE, JSON-RPC handshake only - a probe must
# never place a call or send a message). Each: (method, url, json body or None, expected status,
# substring the body must contain or "").
# ---------------------------------------------------------------------------
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "mcp-direct-verify", "version": "1"}}}


def post_checks(doors: Iterable[str]) -> list:
    checks = [
        ("POST", "https://hatchloop.dev/mcp/agent-broker", INIT, 200, '"serverInfo"'),
        ("POST", "https://api.hatchloop.dev/mcp", INIT, 200, '"serverInfo"'),
    ]
    for d in sorted(doors):
        checks.append(("POST", f"https://hatchloop.dev/mcp/{d}", INIT, 200, '"serverInfo"'))
    # The trailing-slash spelling is a public URL too: the first version of this change shipped with no
    # probe for it and would have answered 307 (http:// Location) instead of 200.
    checks.append(("POST", "https://hatchloop.dev/mcp/agent-broker/", INIT, 200, '"serverInfo"'))
    for d in sorted(doors):
        checks.append(("POST", f"https://hatchloop.dev/mcp/{d}/", INIT, 200, '"serverInfo"'))
    checks += [
        # What must NOT have moved: the site, the retired-server pages, the moved dashboard.
        ("GET", "https://hatchloop.dev/", None, 200, ""),
        ("POST", "https://hatchloop.dev/mcp/data-enrichment", INIT, 410, ""),
        ("POST", "https://hatchloop.dev/mcp/pdf-generator", INIT, 410, ""),
        ("POST", "https://hatchloop.dev/mcp/url-shortener", INIT, 410, ""),
        ("GET", "https://hatchloop.dev/os-probe/", None, 410, "moved"),
        ("GET", "https://api.hatchloop.dev/health", None, 200, "healthy"),
        ("GET", "https://techmate.om/", None, 200, ""),
    ]
    return checks
