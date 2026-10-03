"""Every tunable of the OAuth "Connect" sign-in, and the switch that turns it off.

Nothing here does I/O. The numbers are the ones the MCP authorization specification and the clients' own
documentation ask for, with the source named, so a change is a decision rather than a drift:

  * access tokens are SHORT-LIVED (spec: "SHOULD issue short-lived access tokens"): one hour. A client
    refreshes five minutes before expiry (Claude's documented behaviour), and a paid account's credits,
    revocations and plan are re-read at every refresh - this is what makes a purchase made on the website
    appear in the assistant within the hour without the person doing anything.
  * refresh tokens ROTATE on every use (spec: "For public clients, authorization servers MUST rotate refresh
    tokens"), live 30 days, and the chain ends 90 days after sign-in so a stolen-and-quietly-used token cannot
    live forever.
  * authorization codes live 2 minutes and work once.
  * a sign-in (the email-link round trip) lives 15 minutes, the same lifetime the portal's magic link has.

THE KILL SWITCH. `OAUTH_CONNECT_ENABLED=0` (or false/no/off) makes the whole feature disappear: discovery
documents answer 404, `/oauth/*` answers 404, no tool refusal is turned into a 401, `tools/list` carries no
`securitySchemes`. A deploy that has to be backed out of in a hurry needs a way to do it that is not a code
rollback, and "keyed behaviour must not regress" is easier to promise when there is one.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

SCOPE_TOOLS = "agentbroker.tools"
SCOPE_OFFLINE = "offline_access"

# What the PROTECTED RESOURCE advertises (RFC 9728 scopes_supported). The spec says a server SHOULD NOT put
# offline_access here - a refresh token is the authorization server's business, not a resource requirement.
RESOURCE_SCOPES = (SCOPE_TOOLS,)
# What the AUTHORIZATION SERVER advertises. offline_access is listed because Claude appends it, when present,
# to ask for a refresh token; we issue one to every authorization-code client regardless.
SERVER_SCOPES = (SCOPE_TOOLS, SCOPE_OFFLINE)

ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
REFRESH_FAMILY_TTL_S = 90 * 86400
CODE_TTL_S = 120
SIGNIN_TTL_S = 900
MAGIC_RESEND_GAP_S = 20
MAGIC_MAX_SENDS = 5

# The id the dynamic-registration endpoint hands out starts with this, so a stored client can never be
# mistaken for a metadata-document client (those are https URLs).
DCR_PREFIX = "dcr_"

_OFF = {"0", "false", "no", "off"}


def enabled() -> bool:
    """False only when an operator turned the feature off. On by default: a feature that must be switched on
    after the deploy that ships it is a feature nobody can tell is missing."""
    return os.getenv("OAUTH_CONNECT_ENABLED", "1").strip().lower() not in _OFF


def issuer() -> str:
    """The authorization server's identifier. Scheme + host, no path (so the RFC 8414 well-known URL is the
    simple one and clients need only the first of the three discovery attempts)."""
    # Every getenv here has a real, non-empty default ON PURPOSE: scripts/check_deploy_env.py derives the
    # variables a deploy REQUIRES from the code, and treats "no default" as required. This feature must not
    # make the production container demand a variable nobody has set.
    raw = os.getenv("OAUTH_ISSUER", os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev")).strip()
    return (raw or "https://api.hatchloop.dev").rstrip("/")


def issuer_origin() -> str:
    p = urlsplit(issuer())
    return f"{p.scheme}://{p.netloc}".lower()


def is_https() -> bool:
    return issuer().lower().startswith("https://")


def challenge_style() -> str:
    """How a refused call to a key-requiring tool is signalled to an OAuth-capable client.

      auto        (default) HTTP 401 + WWW-Authenticate, which is what Claude and the MCP specification
                  require - except for ChatGPT, whose documented protocol is a normal tool result carrying
                  `_meta["mcp/www_authenticate"]` and which does not re-trigger sign-in from a 401.
      http401     always the 401.
      tool_result always the 200 tool result with `_meta` (what every client had before, plus the hint).
      off         never signal: the previous behaviour, byte for byte.
    """
    v = os.getenv("OAUTH_CHALLENGE_STYLE", "auto").strip().lower()
    return v if v in {"auto", "http401", "tool_result", "off"} else "auto"


def state_secret() -> str:
    """Key for values derived from a sign-in id (the match code): the verification-link secret, falling back to
    the signing secret - exactly the chain agent_interface/key_request_logic.py uses, so this adds no variable
    a deploy must provide (scripts/check_deploy_env.py counts a getenv whose default is not a literal)."""
    return os.getenv("KEY_VERIFY_SECRET", os.getenv("JWT_SIGNING_SECRET", "dev-oauth-state-secret"))


def site_host() -> str:
    """The marketing-site host that also fronts the MCP doors (Caddy sends /mcp/* straight to the origin)."""
    return (os.getenv("OAUTH_SITE_HOST", "hatchloop.dev").strip() or "hatchloop.dev").lower()


def api_host() -> str:
    return urlsplit(issuer()).netloc.lower()


def known_hosts() -> frozenset:
    """Hosts a protected-resource document may name as its own. A closed list: the metadata endpoint must
    never echo an arbitrary Host header back as a resource identifier."""
    return frozenset({api_host(), site_host(), "api.hatchloop.dev", "hatchloop.dev"})
