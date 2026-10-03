"""Turning "this tool needs an account" into the signal an assistant's Connect button listens for.

Until now a call to a key-requiring tool with no key came back as an ordinary tool result, HTTP 200, that said
(in prose) where to get a key. That is the right answer for an agent that can read prose and edit a config
file. It is the WRONG answer for a consumer assistant, which cannot edit anything: Claude documents that a
`200` carrying `isError` "is an application-level tool failure ... there is no auth prompt", and starts
sign-in only on an HTTP `401` with `WWW-Authenticate`. ChatGPT documents the opposite shape: a normal tool
result whose `_meta["mcp/www_authenticate"]` carries the challenge (and does not re-trigger sign-in from a
401). Both are implemented, chosen per client (`settings.challenge_style`):

  http401      the original JSON-RPC response is kept as the body - a client that reads bodies sees exactly
               what it saw before - but the status is 401 and `WWW-Authenticate: Bearer ... resource_metadata`
               points at the protected-resource document. Claude's documented example, nearly verbatim.
  tool_result  status 200, same body, plus `_meta["mcp/www_authenticate"]` on the refused result.

WHO GETS WHICH (`auto`). The 401 goes only to a caller known to open a sign-in from one: the connectors named in
settings.connector_agents() (Claude's web connector and CLI, by the User-Agent they present) and any caller that
already sent a bearer token. ChatGPT gets the tool result. EVERYONE ELSE gets the tool result too - the readable
answer plus the hint - because an agent with no OAuth support raises on an HTTP 401 before it reads the body that
tells it how to get a key, buy credits or pay per call (the official Python SDK without an auth provider does
exactly that; measured in the 2026-10-03 review). A client that does speak OAuth but is not on the list can
still connect through the discovery documents; it just is not prompted. Add its name to
OAUTH_CHALLENGE_401_CLIENTS once an assistant has been walked through live.

WHEN A CALL IS CHALLENGED. Only when ALL of these hold:
  * it is a response the dispatcher produced for a key-requiring tool (`auth_required` for the write tools,
    `identity_required` for get_conversation) - detected from the result itself, so a call the dispatcher
    allowed (a valid key, an x402 payment attached, REQUIRE_AUTH off) is never touched;
  * the request carried no valid credential: none at all, or a Bearer token that did not validate
    (an expired access token must get a 401 so the client refreshes). A bad `X-Agent-Identity` or `X-Api-Key`
    key keeps the previous answer - that caller chose the key path and its message tells them what is wrong
    with the key;
  * the sign-in can actually complete right now (`store.ready()`), and the operator has not switched it off.
Keyless tools are never challenged: nothing here runs for them.

`tools/list` additionally carries ChatGPT's per-tool `securitySchemes` so its connector knows which tools
work without an account (`noauth`) and which need one (`oauth2`).
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Optional

from fastapi.responses import JSONResponse

from agent_interface.oauth import resources, settings
from agent_interface.oauth.store import ready

log = logging.getLogger("smb_broker.oauth")

REFUSAL_CODES = frozenset({"auth_required", "identity_required"})


def _members(response) -> list:
    if isinstance(response, dict):
        return [response]
    if isinstance(response, list):
        return [m for m in response if isinstance(m, dict)]
    return []


def _refusal(member: dict) -> Optional[str]:
    """The refusal code when this JSON-RPC response is 'this tool needs an account', else None."""
    result = member.get("result")
    if not isinstance(result, dict) or result.get("isError") is not True:
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("error_code", "reason_code"):
        if body.get(key) in REFUSAL_CODES:
            return str(body[key])
    return None


def _credential_verdict(headers: dict) -> str:
    """'valid' | 'none' | 'bearer_bad' | 'other_bad' for the request's credentials."""
    from agent_interface.key_state import classify_key, KEY_VALID, KEY_NONE, KEY_EXPIRED
    xid = (headers.get("x-agent-identity") or "").strip()
    xapi = (headers.get("x-api-key") or "").strip()
    auth = (headers.get("authorization") or "").strip()
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if xid:
        return "valid" if classify_key(xid).state == KEY_VALID else "other_bad"
    if bearer:
        st = classify_key(bearer).state
        if st == KEY_VALID:
            return "valid"
        return "bearer_expired" if st == KEY_EXPIRED else "bearer_bad"
    if xapi:
        return "valid" if classify_key(xapi).state == KEY_VALID else "other_bad"
    return "none"


def _is_openai_client(headers: dict) -> bool:
    ua = (headers.get("user-agent") or "").lower()
    return "openai" in ua or "chatgpt" in ua or any(k.startswith("x-openai") for k in headers)


def _starts_signin_from_a_401(headers: dict, verdict: str) -> bool:
    """Does this caller turn a 401 into a sign-in? Known only for (a) a caller that already presented a bearer
    token - it speaks OAuth whatever it calls itself, and an expired token MUST get the 401 so it refreshes -
    and (b) the connectors named in settings.connector_agents(). Everyone else keeps the readable answer: a
    client with no OAuth support raises on a 401 before reading the body that says how to get a key."""
    if verdict in ("bearer_bad", "bearer_expired"):
        return True
    ua = (headers.get("user-agent") or "").lower()
    return any(p in ua for p in settings.connector_agents())


def _description(verdict: str) -> str:
    return {"bearer_expired": "The access token expired",
            "bearer_bad": "The access token is not valid"}.get(verdict, "Authentication required for this tool")


def www_authenticate_401(metadata_url: str, verdict: str) -> str:
    return (f'Bearer error="invalid_token", error_description="{_description(verdict)}", '
            f'resource_metadata="{metadata_url}", scope="{settings.SCOPE_TOOLS}"')


def www_authenticate_meta(metadata_url: str) -> str:
    # OpenAI: "make sure the value contains both an `error` and `error_description` parameter".
    return (f'Bearer resource_metadata="{metadata_url}", error="insufficient_scope", '
            'error_description="Login required"')


async def apply(response, request):
    """The response to send for `request`: `response` itself when nothing applies (the overwhelmingly common
    case, and a cheap one), a modified copy for ChatGPT, or a 401 JSONResponse carrying the original body."""
    try:
        if not settings.enabled() or settings.challenge_style() == "off":
            return response
        refused = [m for m in _members(response) if _refusal(m)]
        if not refused:
            return response
        headers = {k.lower(): v for k, v in request.headers.items()}
        verdict = _credential_verdict(headers)
        if verdict in ("valid", "other_bad"):
            return response
        if not await ready():
            return response
        url = resources.metadata_url(headers.get("host"), request.url.path)
        style = settings.challenge_style()
        if style == "auto":
            style = ("tool_result" if _is_openai_client(headers) or not _starts_signin_from_a_401(headers, verdict)
                     else "http401")
        marked = copy.deepcopy(response)
        for m in _members(marked):
            if _refusal(m):
                m["result"].setdefault("_meta", {})["mcp/www_authenticate"] = [www_authenticate_meta(url)]
        log.info("oauth_challenge style=%s verdict=%s members=%d", style, verdict, len(refused))
        if style == "tool_result":
            return marked
        return JSONResponse(marked, status_code=401, headers={
            "WWW-Authenticate": www_authenticate_401(url, verdict), "Cache-Control": "no-store"})
    except Exception:  # noqa: BLE001 - the challenge is a courtesy; it must never turn a good answer into a crash
        log.exception("oauth_challenge_failed -- sending the original response")
        return response


def schemes_for(tool_name: str) -> list:
    """ChatGPT's `securitySchemes` for one tool: what it needs from the caller."""
    from core import tool_auth
    oauth2 = {"type": "oauth2", "scopes": [settings.SCOPE_TOOLS]}
    cls = tool_auth.auth_class(tool_name)
    if cls == "needs_key":
        return [oauth2]
    if cls == "quota_free":
        return [{"type": "noauth"}, oauth2]
    return [{"type": "noauth"}]


async def annotate_tools(tools: list) -> list:
    """Add `securitySchemes` to each tool - at request time, to the response only (the cached manifest and the
    generated registry files are untouched). Nothing is added unless the sign-in can complete."""
    if not settings.enabled() or settings.challenge_style() == "off" or not await ready():
        return tools
    out = []
    for t in tools:
        t2 = dict(t)
        if isinstance(t2.get("name"), str):
            t2["securitySchemes"] = schemes_for(t2["name"])
        out.append(t2)
    return out
