"""How the discovery documents describe the sign-in and the protocol versions - one derivation for all of them.

WHY THIS FILE EXISTS. Release 1 (2026-10-03) added OAuth "Connect" and the MCP 2026-07-28 handshake. Every
discovery document written before it - /llms.txt, /.well-known/mcp.json, the discovery card - kept saying "get a
key by email" and named no protocol version, so an agent that read only the documents could not find either.
Four documents now need the same two facts, and four typed copies is the drift core/tool_auth.py was written to end.

EVERYTHING HERE IS READ, NOTHING IS TYPED:
  * the endpoints, grants, PKCE methods and registration styles come from the OAuth router's own metadata
    (agent_interface/oauth/router.py), so a document cannot name a capability the metadata does not;
  * the tools that need an account come from core/tool_auth.py;
  * the protocol versions come from the list `server/discover` answers.

A SWITCH THAT IS OFF IS NOT ADVERTISED. With OAUTH_CONNECT_ENABLED=0 `oauth_block()` is None and every document
leaves the sign-in out, exactly as the endpoints answer 404. It follows the SWITCH, not the sign-in's database
readiness (agent_interface/oauth/store.ready()): that probe is what `tools/list` and a refused call consult, because
they put a Connect button in front of a person, whereas these are static documents that are also compiled into
snapshots offline, where a database probe would make the same document differ by where it was built.

NO ASSISTANT IS NAMED. docs/OAUTH-CONNECT.md ("Not done here"): nothing is quoted about a particular assistant
before it has been walked through live. These documents describe the protocol - what a client that speaks MCP
authorization will find - and stop there. They carry no price and no credit either: payment claims belong to the
payments block (agent_interface/well_known._payments_block) and its own tests.
"""
from __future__ import annotations

from typing import Optional

from agent_interface.oauth import settings
from core import tool_auth


def _mcp_resource_metadata_path() -> str:
    return "/.well-known/oauth-protected-resource/mcp"


def oauth_block() -> Optional[dict]:
    """The sign-in as a machine-readable block, or None when it is switched off."""
    if not settings.enabled():
        return None
    from agent_interface.oauth.router import authorization_server_metadata
    meta = authorization_server_metadata()
    iss = settings.issuer()
    registration = []
    if meta.get("client_id_metadata_document_supported"):
        registration.append("client_id_metadata_document")
    if meta.get("registration_endpoint"):
        registration.append("dynamic_client_registration")
    return {
        "authorization_server": meta["issuer"],
        # The document for the endpoint a client connects to (`<issuer>/mcp`). The bare well-known path names the
        # issuer's own origin as the resource, which is not an MCP endpoint, so a client that validates the
        # `resource` against the URL it connected to must be pointed at this one.
        "protected_resource_metadata_url": f"{iss}{_mcp_resource_metadata_path()}",
        "authorization_server_metadata_url": f"{iss}/.well-known/oauth-authorization-server",
        "grant_types": list(meta["grant_types_supported"]),
        "code_challenge_methods": list(meta["code_challenge_methods_supported"]),
        "client_registration": registration,
        "scopes": list(meta["scopes_supported"]),
        "sign_in": "An email address and a one-time link we send to it. No password.",
        "token": "The access token is an Agent-Identity key: send it as 'Authorization: Bearer <token>'.",
        "tools_that_need_an_account": sorted(tool_auth.TOOLS_REQUIRING_KEY),
    }


def protocol_versions() -> dict:
    """What this endpoint speaks, read from the list `server/discover` answers (newest first)."""
    from agent_interface.mcp_2026 import MODERN_PROTOCOL_VERSIONS
    from agent_interface.mcp_server import ALL_PROTOCOL_VERSIONS, SUPPORTED_PROTOCOL_VERSIONS
    return {
        "supported": list(ALL_PROTOCOL_VERSIONS),
        # The modern revision has no handshake: a client may call `server/discover` first, or send any request
        # with its version in `_meta`. The legacy versions are the ones that open with `initialize`.
        "modern": list(MODERN_PROTOCOL_VERSIONS),
        "legacy": list(SUPPORTED_PROTOCOL_VERSIONS),
        "server_discover": True,
    }


# How a registration style from `oauth_block()["client_registration"]` reads in a sentence. A style the router
# advertises that is not in this table is named as it is, never dropped.
_REGISTRATION_PHRASES = {
    "client_id_metadata_document": "with a Client ID Metadata Document",
    "dynamic_client_registration": "by dynamic registration",
}


def llms_txt_sign_in_lines(base_url: str) -> list:
    """The /llms.txt paragraph about the sign-in, or [] when it is switched off.

    The registration styles and the PKCE methods are read from the block, which reads them from the router's metadata:
    a router that drops dynamic registration, or moves PKCE off S256, changes this paragraph with it.

    "Free" is not "keyless" (core/tool_auth.py): two tools cost nothing and still refuse an anonymous call. So the
    paragraph names the tools that need an account and says of every other tool that it works without one."""
    block = oauth_block()
    if block is None:
        return []
    needs = ", ".join(f"`{t}`" for t in block["tools_that_need_an_account"])
    styles = " or ".join(_REGISTRATION_PHRASES.get(r, f"as `{r}`") for r in block["client_registration"])
    registers = f"registers {styles}, " if styles else ""
    methods = ", ".join(f"`{m}`" for m in block["code_challenge_methods"])
    flow = "runs the authorization-code flow" + (f" with PKCE ({methods})" if methods else "")
    return [
        "### Sign in with OAuth (MCP authorization)",
        "",
        "A client that supports MCP authorization can get a key without anyone handling one. It discovers "
        f"`{block['protected_resource_metadata_url']}` (RFC 9728), reads the authorization server from it "
        f"(`{block['authorization_server_metadata_url']}`, RFC 8414), {registers}and {flow}. The person is asked "
        "for an email address and presses Confirm on a one-time link we send there; there is no password. The access "
        f"token is an Agent-Identity key, so it also works as `Authorization: Bearer <token>` at `{base_url}/mcp`.",
        "",
        f"Tools that need an account: {needs}. Every other tool works without a key or a sign-in "
        "(the premium data tools within a daily quota). "
        "Sign-in and the key-by-email path above give the same kind of key.",
        "",
    ]


def llms_txt_protocol_lines() -> list:
    """The /llms.txt paragraph about the protocol versions."""
    v = protocol_versions()
    legacy = ", ".join(f"`{x}`" for x in v["legacy"])
    return [
        "**Protocol versions.** This endpoint speaks MCP "
        f"`{v['modern'][0]}`, which has no handshake: call `server/discover` to learn the supported versions "
        "and capabilities, or send any request with its version in `_meta`. It also answers the older `initialize` "
        f"handshake for {legacy}.",
        "",
    ]
