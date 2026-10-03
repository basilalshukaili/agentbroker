"""HTTP surface of the OAuth "Connect" sign-in (MCP authorization, current specification).

    GET  /.well-known/oauth-protected-resource[/<path>]   RFC 9728: what this resource is and who issues tokens
    GET  /.well-known/oauth-authorization-server          RFC 8414: where the endpoints are, what is supported
    GET  /.well-known/openid-configuration                the same document (clients try both; see below)
    POST /oauth/register                                  RFC 7591 dynamic client registration (public clients)
    GET  /oauth/authorize                                 start: names the app, asks for an email address
    POST /oauth/authorize/email                           mail the one-time link
    POST /oauth/authorize/poll                            the waiting page asks "has it been confirmed?"
    GET  /oauth/verify?t=...                              the mailed link: shows what is being approved
    POST /oauth/verify                                    the deliberate press: Confirm / Cancel
    POST /oauth/token                                     authorization_code and refresh_token grants
    POST /oauth/revoke                                    RFC 7009

THE SHAPE OF A SIGN-IN, and why it is shaped this way:

  1. The assistant sends the person to /oauth/authorize. We establish WHO is asking (a metadata-document or
     registered client, redirect URI matched exactly) before showing anything; an unknown client or a
     redirect URI that does not match is an error PAGE, never a redirect - redirecting to an unvalidated
     address is how a sign-in page becomes an open redirector.
  2. The person types an email. We mail a link. They are told nothing about whether the address "exists":
     there is no account to find, an address simply stands for one.
  3. The link opens a page that names the app and has a Confirm button. Opening a link never spends it - mail
     scanners and link previewers open every link - only the button does.
  4. The AUTHORIZATION CODE is handed to the browser that STARTED the sign-in, not to whoever pressed the
     button: that browser proves it is the originator with a poll secret only it holds (kept in the page it
     was given and in a cookie). So the link can be opened on a phone while the assistant runs on a laptop,
     and a stranger who is sent someone else's link and presses Confirm hands the code to nobody.
  5. The code is useless without the PKCE verifier only the app has, works once, and lives two minutes.

DISCOVERY. `/.well-known/openid-configuration` serves the same OAuth metadata as the RFC 8414 path because
clients are required to try both; it is not an OpenID Provider (no ID tokens, no userinfo) and does not claim
to be - it carries no OIDC-only fields.

None of these routes is behind the /mcp rate limiter; each endpoint that an anonymous caller can use to cause
work (an email, a registration, an outbound fetch) has its own ceiling in agent_interface/oauth/limits.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from agent_interface.oauth import emailer, limits, pages, resources, settings, tokens
from agent_interface.oauth.clients import (
    ClientError, ClientInfo, check_redirect_uri, redirect_host, redirect_matches, resolve_client,
    validate_registration,
)
from agent_interface.oauth.store import StoreUnavailable, get_store

log = logging.getLogger("smb_broker.oauth")

router = APIRouter(tags=["OAuth"])

COOKIE = "hl_oauth"
_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# ASCII local part only: a look-alike (full-width, Cyrillic...) address is a different mailbox that merely
# LOOKS like another, and internationalised addresses are not something we can promise to route.
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}$")
_MAX_BODY = 16 * 1024

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, MCP-Protocol-Version",
    "Access-Control-Max-Age": "86400",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _off() -> Optional[Response]:
    """404 for every route when the operator has switched the feature off - indistinguishable from absent."""
    return Response(status_code=404) if not settings.enabled() else None


def _ip(request: Request) -> str:
    from core.client_ip import resolve_client_ip
    h = request.headers
    peer = request.client.host if request.client else None
    return resolve_client_ip(peer, h.get("x-forwarded-for"), h.get("x-real-ip"))


def _ipkey(request: Request) -> str:
    return hashlib.sha256(_ip(request).encode()).hexdigest()[:24]


def _json(content: dict, status: int = 200, extra: Optional[dict] = None, cors: bool = True) -> JSONResponse:
    h = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if cors:
        h.update(_CORS)
    if extra:
        h.update(extra)
    return JSONResponse(content=content, status_code=status, headers=h)


def _oauth_error(error: str, description: str, status: int = 400, extra: Optional[dict] = None) -> JSONResponse:
    return _json({"error": error, "error_description": description}, status, extra)


def _unavailable() -> JSONResponse:
    return _oauth_error("temporarily_unavailable",
                        "The sign-in service is not available right now. Please try again in a minute.",
                        503, {"Retry-After": "5"})


def _page(html_text: str, nonce: str, status: int = 200, *, form_action_self: bool = True) -> HTMLResponse:
    return HTMLResponse(html_text, status_code=status, headers=pages.headers(nonce, form_action_self=form_action_self))


def _message(title: str, message: str, status: int = 200, *, error: bool = False) -> HTMLResponse:
    n = pages.new_nonce()
    return _page(pages.message_page(title, message, n, error=error), n, status)


async def _read(request: Request) -> bytes:
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise ValueError("body too large")
    return body


async def _form(request: Request) -> dict:
    """application/x-www-form-urlencoded (RFC 6749 section 4.1.3). Parsed by hand so the token endpoint does
    not depend on a multipart library, and so a JSON body - which some clients wrongly send - still works."""
    body = await _read(request)
    ctype = (request.headers.get("content-type") or "").lower()
    if "json" in ctype:
        data = json.loads(body.decode("utf-8") or "{}")
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict = {}
    for k, v in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
        out.setdefault(k, v)
    return out


def build_redirect(redirect_uri: str, params: dict) -> str:
    """redirect_uri with `params` appended to its query (existing query kept), None values dropped, and `iss`
    (RFC 9207) always included by the callers. The URI was validated at registration/authorize time."""
    p = urlsplit(redirect_uri)
    q = parse_qsl(p.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q, quote_via=quote), ""))


def _err_redirect(redirect_uri: str, error: str, description: str, state: Optional[str]) -> RedirectResponse:
    return RedirectResponse(build_redirect(redirect_uri, {
        "error": error, "error_description": description, "state": state or None, "iss": settings.issuer()}),
        status_code=302, headers={"Cache-Control": "no-store"})


def _grant_scope(requested: Optional[str]) -> str:
    """The scope we will grant. We have one real scope; a client that asks for others (openid, profile...)
    gets the one it can use rather than an error, and the response says what was granted."""
    return settings.SCOPE_TOOLS


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def authorization_server_metadata() -> dict:
    iss = settings.issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "revocation_endpoint": f"{iss}/oauth/revoke",
        "scopes_supported": list(settings.SERVER_SCOPES),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": "https://hatchloop.dev/agents.md",
    }


def protected_resource_metadata(resource: str) -> dict:
    return {
        "resource": resource,
        "authorization_servers": [settings.issuer()],
        "scopes_supported": list(settings.RESOURCE_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "HatchLoop AgentBroker",
        "resource_documentation": "https://hatchloop.dev/agents.md",
    }


def _discovery(doc: dict) -> JSONResponse:
    return JSONResponse(doc, headers={**_CORS, "Cache-Control": "public, max-age=300"})


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{suffix:path}")
async def protected_resource(request: Request, suffix: str = ""):
    if (r := _off()) is not None:
        return r
    host = request.query_params.get("host") or request.headers.get("host")
    resource = resources.resource_for(host, suffix)
    if resource is None:
        return Response(status_code=404)
    return _discovery(protected_resource_metadata(resource))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
async def authorization_server(request: Request):
    if (r := _off()) is not None:
        return r
    return _discovery(authorization_server_metadata())


@router.options("/.well-known/oauth-protected-resource")
@router.options("/.well-known/oauth-protected-resource/{suffix:path}")
@router.options("/.well-known/oauth-authorization-server")
@router.options("/.well-known/openid-configuration")
@router.options("/oauth/register")
@router.options("/oauth/token")
@router.options("/oauth/revoke")
async def preflight(request: Request, suffix: str = ""):
    if (r := _off()) is not None:
        return r
    return Response(status_code=204, headers=_CORS)


# ---------------------------------------------------------------------------
# dynamic client registration
# ---------------------------------------------------------------------------

@router.post("/oauth/register")
async def register(request: Request):
    if (r := _off()) is not None:
        return r
    key = _ipkey(request)
    if not (limits.check("register_ip", key) and limits.check("register_global", "all")):
        return _oauth_error("temporarily_unavailable", "Too many registrations. Try again later.", 429,
                            {"Retry-After": "600"})
    try:
        data = json.loads((await _read(request)).decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return _oauth_error("invalid_client_metadata", "The body must be a JSON object.")
    clean, problem = validate_registration(data)
    if problem:
        return _oauth_error(problem[0], problem[1])
    client_id = settings.DCR_PREFIX + tokens.new_secret(18)
    try:
        stored = await get_store().client_register(client_id, clean["client_name"], clean["redirect_uris"], key)
    except StoreUnavailable:
        return _unavailable()
    if not stored:
        return _oauth_error("temporarily_unavailable", "Registration is full. Try again later.", 503)
    return _json({
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": clean["client_name"],
        "redirect_uris": clean["redirect_uris"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": settings.SCOPE_TOOLS,
    }, 201)


# ---------------------------------------------------------------------------
# authorize: start
# ---------------------------------------------------------------------------

@router.get("/oauth/authorize")
async def authorize(request: Request):
    if (r := _off()) is not None:
        return r
    q = request.query_params
    store = get_store()
    ipk = _ipkey(request)
    client_id, redirect_uri = q.get("client_id"), q.get("redirect_uri")

    # --- 1. establish the client and the return address. Failures here are PAGES, never redirects. ---
    if not limits.check("signin_start_ip", ipk):
        return _message("Too many attempts", "Please wait a little while and try again from the app.", 429, error=True)
    if isinstance(client_id, str) and client_id.startswith("https://"):
        host = (urlsplit(client_id).hostname or "").lower()
        if not (limits.check("metadata_fetch_ip", ipk) and limits.check("metadata_fetch_host", host or "?")):
            return _message("Too many attempts", "Please wait a little while and try again from the app.", 429, error=True)
    try:
        client = await resolve_client(store, client_id)
    except ClientError as exc:
        return _message("Cannot connect this app", exc.message + " Go back to the app and try again.", 400, error=True)
    except StoreUnavailable:
        return _message("Temporarily unavailable", "Please try again in a minute.", 503, error=True)
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
    if (not redirect_uri or check_redirect_uri(redirect_uri, allow_private_scheme=(client.kind == "dcr"))
            or not redirect_matches(client.redirect_uris, redirect_uri)):
        return _message("Cannot connect this app",
                        "The app's return address does not match what it registered, so nothing was sent "
                        "to it. Go back to the app and try again.", 400, error=True)

    # --- 2. from here the return address is trusted: errors go back to the app. ---
    state = q.get("state")
    if state is not None and len(state) > 2048:
        return _err_redirect(redirect_uri, "invalid_request", "state is too long", None)
    if q.get("response_type") != "code":
        return _err_redirect(redirect_uri, "unsupported_response_type", "Only response_type=code is supported.", state)
    challenge = q.get("code_challenge")
    if not tokens.valid_challenge(challenge) or q.get("code_challenge_method") != "S256":
        return _err_redirect(redirect_uri, "invalid_request",
                             "PKCE with code_challenge_method=S256 is required.", state)
    resource = resources.default_resource()
    if q.get("resource"):
        resource = resources.canonical_resource(q.get("resource"))
        if resource is None:
            return _err_redirect(redirect_uri, "invalid_target", "That resource is not served here.", state)
    scope = _grant_scope(q.get("scope"))

    rid = tokens.new_secret(18)
    try:
        await store.request_create(rid, client.client_id, redirect_uri, challenge, scope, state, resource,
                                   settings.SIGNIN_TTL_S)
    except StoreUnavailable:
        return _err_redirect(redirect_uri, "temporarily_unavailable", "The sign-in service is not available right now.", state)
    n = pages.new_nonce()
    # The poll secret is minted with the sign-in and rides in the page's own form, so a retry after a
    # mistyped address binds the SAME secret the waiting page will later use - and no other browser has it.
    return _page(pages.start_page(client, redirect_uri, rid, n, poll_secret=tokens.new_secret(24)), n)


# ---------------------------------------------------------------------------
# authorize: mail the link, and wait
# ---------------------------------------------------------------------------

def _cookie_value(request: Request) -> Optional[tuple]:
    raw = request.cookies.get(COOKIE) or ""
    rid, _, poll = raw.partition(".")
    return (rid, poll) if _ID.match(rid) and _ID.match(poll) else None


async def _client_for_request(store, req: dict) -> Optional[ClientInfo]:
    try:
        return await resolve_client(store, req["client_id"])
    except (ClientError, StoreUnavailable):
        return None


@router.post("/oauth/authorize/email")
async def authorize_email(request: Request):
    if (r := _off()) is not None:
        return r
    store = get_store()
    try:
        form = await _form(request)
    except (ValueError, UnicodeDecodeError):
        return _message("Invalid request", "Please go back to the app and try again.", 400, error=True)
    rid = str(form.get("rid") or "")
    email = tokens.normalise_email(str(form.get("email") or ""))
    given_poll = str(form.get("poll_secret") or "")
    if not _ID.match(rid) or (given_poll and not _ID.match(given_poll)):
        return _message("Invalid request", "Please go back to the app and try again.", 400, error=True)
    ipk = _ipkey(request)
    if not (limits.check("email_ip", ipk) and limits.check("email_global", "all")):
        return _message("Too many attempts", "Please wait a little while and try again.", 429, error=True)

    try:
        req = await store.request_get(rid)
    except StoreUnavailable:
        return _message("Temporarily unavailable", "Please try again in a minute.", 503, error=True)
    if not req or req.get("status") == "expired":
        return _message("This sign-in expired",
                        "Go back to the app and choose Connect again.", 400, error=True)
    client = await _client_for_request(store, req)
    if client is None:
        return _message("Cannot connect this app", "Go back to the app and try again.", 400, error=True)

    poll_secret = given_poll or tokens.new_secret(24)

    def _retry(msg: str, status: int = 400) -> HTMLResponse:
        n = pages.new_nonce()
        return _page(pages.start_page(client, req["redirect_uri"], rid, n, error=msg, poll_secret=poll_secret),
                     n, status)

    local = email.partition("@")[0]
    if (not _EMAIL.match(email) or len(email) > 254 or ".." in local or local.startswith(".") or local.endswith(".")):
        return _retry("That does not look like an email address.")
    digest = tokens.email_hash(email)
    if not limits.check("email_recipient", digest):
        return _message("Too many emails", "A link was sent to that address several times already. "
                        "Check your inbox and spam folder, or try again in an hour.", 429, error=True)

    magic = tokens.new_secret(32)
    try:
        res = await store.request_set_email(
            rid, tokens.sha256_hex(poll_secret), tokens.sha256_hex(magic), digest, tokens.mask_email(email),
            settings.MAGIC_RESEND_GAP_S, settings.MAGIC_MAX_SENDS)
    except StoreUnavailable:
        return _message("Temporarily unavailable", "Please try again in a minute.", 503, error=True)
    if not res.get("ok"):
        reason = res.get("reason")
        if reason == "too_soon":
            return _retry("A link was sent a moment ago. Please wait about half a minute before asking again.", 429)
        if reason == "too_many":
            return _message("Too many emails", "This sign-in has used all of its emails. Go back to the app "
                            "and choose Connect to start again.", 429, error=True)
        if reason == "poll_mismatch":
            return _message("Invalid request", "This page does not belong to that sign-in. Go back to the app "
                            "and choose Connect again.", 400, error=True)
        if reason == "bad_state":
            return _message("Already confirmed", "This sign-in was already confirmed. Go back to the app - it "
                            "finishes connecting by itself.", 400, error=True)
        return _message("This sign-in expired", "Go back to the app and choose Connect again.", 400, error=True)

    link = f"{settings.issuer()}/oauth/verify?t={magic}"
    outcome = await emailer.send_signin_link(email, link, client.label, redirect_host(req["redirect_uri"]))
    if outcome == emailer.REJECTED:
        return _retry("That address was not accepted. Please check it and try again.")
    if outcome != emailer.SENT:
        return _retry("We could not send the email just now. Nothing was sent. Please try again in a minute.", 503)

    log.info("oauth_signin_email_sent client=%s kind=%s", client.host or client.client_id[:12], client.kind)
    n = pages.new_nonce()
    resp = _page(pages.wait_page(rid, poll_secret, tokens.mask_email(email), n, max_s=settings.SIGNIN_TTL_S,
                                 match_code=tokens.match_code(rid)), n)
    resp.set_cookie(COOKIE, f"{rid}.{poll_secret}", max_age=settings.SIGNIN_TTL_S, path="/oauth",
                    httponly=True, secure=settings.is_https(), samesite="lax")
    return resp


async def _deliver(store, rid: str, poll_secret: str) -> dict:
    """Hand the result of a confirmed sign-in to the party that holds the poll secret - exactly once.

    {"status": "redirect", "location": ...} when there is something to deliver (the code, or the denial);
    otherwise the sign-in's state, so the waiting page knows whether to keep waiting."""
    ph = tokens.sha256_hex(poll_secret)
    status = await store.request_poll(rid, ph)
    if status not in ("verified", "denied"):
        return {"status": "pending" if status in ("new", "email_sent") else status}
    code = tokens.new_secret(32)
    out = await store.request_complete(rid, ph, tokens.sha256_hex(code), settings.CODE_TTL_S)
    if not out.get("ok"):
        return {"status": "pending" if out.get("reason") == "not_ready" else str(out.get("reason") or "unknown")}
    iss = settings.issuer()
    if out.get("outcome") == "denied":
        location = build_redirect(out["redirect_uri"], {
            "error": "access_denied", "error_description": "The user declined.",
            "state": out.get("state") or None, "iss": iss})
    else:
        location = build_redirect(out["redirect_uri"], {"code": code, "state": out.get("state") or None, "iss": iss})
    return {"status": "redirect", "location": location}


@router.post("/oauth/authorize/poll")
async def authorize_poll(request: Request):
    if (r := _off()) is not None:
        return r
    try:
        data = json.loads((await _read(request)).decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"status": "unknown"}, status_code=400, headers={"Cache-Control": "no-store"})
    rid = str(data.get("rid") or "") if isinstance(data, dict) else ""
    poll = str(data.get("poll_secret") or "") if isinstance(data, dict) else ""
    if not _ID.match(rid) or not _ID.match(poll):
        return JSONResponse({"status": "unknown"}, status_code=400, headers={"Cache-Control": "no-store"})
    if not limits.check("poll_request", rid):
        return JSONResponse({"status": "pending"}, status_code=429, headers={"Cache-Control": "no-store", "Retry-After": "5"})
    try:
        out = await _deliver(get_store(), rid, poll)
    except StoreUnavailable:
        return JSONResponse({"status": "pending"}, status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "5"})
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# the mailed link
# ---------------------------------------------------------------------------

@router.get("/oauth/verify")
async def verify_page(request: Request):
    if (r := _off()) is not None:
        return r
    t = request.query_params.get("t") or ""
    if not _ID.match(t):
        return _message("This link is not valid", "Go back to the app and choose Connect to get a new one.", 400, error=True)
    if not limits.check("verify_ip", _ipkey(request)):
        return _message("Too many attempts", "Please wait a little while and try again.", 429, error=True)
    store = get_store()
    try:
        info = await store.request_lookup_magic(tokens.sha256_hex(t))
    except StoreUnavailable:
        return _message("Temporarily unavailable", "Please try again in a minute.", 503, error=True)
    if not info:
        return _message("This link is not valid", "It may have been replaced by a newer one. Check your inbox "
                        "for the latest email, or choose Connect in the app again.", 400, error=True)
    if info["status"] == "expired":
        return _message("This link expired", "Go back to the app and choose Connect again.", 400, error=True)
    if info["status"] != "email_sent":
        return _message("This link was already used", "If you just confirmed it, go back to the app - it "
                        "finishes connecting by itself.")
    cookie = _cookie_value(request)
    started_here = bool(cookie and cookie[0] == info["request_id"])
    return await _confirm_page(store, info, t, ask_code=not started_here)


async def _confirm_page(store, info: dict, t: str, *, ask_code: bool, error: str = "", status: int = 200):
    client = await _client_for_request(store, info)
    label = client.label if client else redirect_host(info["redirect_uri"])
    n = pages.new_nonce()
    return _page(pages.confirm_page(label, info["redirect_uri"], info.get("email_hint") or "your account", t, n,
                                    client=client, ask_code=ask_code, error=error), n, status, form_action_self=False)


@router.post("/oauth/verify")
async def verify_decide(request: Request):
    if (r := _off()) is not None:
        return r
    try:
        form = await _form(request)
    except (ValueError, UnicodeDecodeError):
        return _message("Invalid request", "Please open the link from your email again.", 400, error=True)
    t = str(form.get("t") or "")
    decision = str(form.get("decision") or "")
    if not _ID.match(t) or decision not in ("approve", "deny"):
        return _message("Invalid request", "Please open the link from your email again.", 400, error=True)
    if not limits.check("verify_ip", _ipkey(request)):
        return _message("Too many attempts", "Please wait a little while and try again.", 429, error=True)
    store = get_store()
    cookie = _cookie_value(request)
    try:
        info = await store.request_lookup_magic(tokens.sha256_hex(t))
        if info and decision == "approve" and info["status"] == "email_sent" and not (cookie and cookie[0] == info["request_id"]):
            # Not the browser that started this sign-in: the person must prove they can see the page that did.
            if not limits.check("match_attempt", tokens.sha256_hex(t)[:32]):
                return _message("Too many attempts", "That code was entered wrongly too many times. Go back to the "
                                "app and choose Connect again.", 429, error=True)
            if not tokens.match_code_ok(info["request_id"], str(form.get("code") or "")):
                return await _confirm_page(store, info, t, ask_code=True, status=400,
                                           error="That code does not match. It is shown on the page where you started connecting.")
        res = await store.request_decide(tokens.sha256_hex(t), decision == "approve")
    except StoreUnavailable:
        return _message("Temporarily unavailable", "Please try again in a minute.", 503, error=True)
    if not res.get("ok") and res.get("reason") in ("not_found",):
        return _message("This link is not valid", "Check your inbox for the latest email.", 400, error=True)
    if not res.get("ok") and res.get("reason") == "expired":
        return _message("This link expired", "Go back to the app and choose Connect again.", 400, error=True)

    # Same browser as the one that started the sign-in? Then finish the hand-back right here.
    rid = res.get("request_id")
    if cookie and rid and cookie[0] == rid:
        try:
            out = await _deliver(store, rid, cookie[1])
        except StoreUnavailable:
            out = {"status": "pending"}
        if out.get("status") == "redirect":
            return RedirectResponse(out["location"], status_code=303, headers={"Cache-Control": "no-store"})

    if not res.get("ok"):
        return _message("This link was already used", "If you just confirmed it, go back to the app - it "
                        "finishes connecting by itself.")
    if decision == "deny":
        return _message("Cancelled", "Nothing was connected. You can close this page.")
    return _message("Confirmed", "You can close this page and go back to the app where you started - it "
                    "finishes connecting by itself within a few seconds.")


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def _token_response(access, refresh: Optional[str], scope: str) -> JSONResponse:
    body = {"access_token": access.token, "token_type": "Bearer", "expires_in": settings.ACCESS_TTL_S,
            "scope": scope}
    if refresh:
        body["refresh_token"] = refresh
    return _json(body)


async def _grant_authorization_code(store, form: dict) -> JSONResponse:
    code, redirect_uri = form.get("code"), form.get("redirect_uri")
    client_id, verifier = form.get("client_id"), form.get("code_verifier")
    if not all(isinstance(v, str) and v for v in (code, redirect_uri, client_id, verifier)):
        return _oauth_error("invalid_request", "code, redirect_uri, client_id and code_verifier are required.")
    if len(code) > 512:
        return _oauth_error("invalid_grant", "The authorization code is invalid, expired or already used.")
    got = await store.code_consume(tokens.sha256_hex(code))
    if not got.get("ok"):
        return _oauth_error("invalid_grant", "The authorization code is invalid, expired or already used.")
    # From here the code is spent: any mismatch below ends this attempt for good (RFC 6749 section 10.5).
    if got["client_id"] != client_id or got["redirect_uri"] != redirect_uri:
        return _oauth_error("invalid_grant", "The authorization code was issued to a different client or redirect URI.")
    if not tokens.pkce_matches(verifier, got["code_challenge"]):
        return _oauth_error("invalid_grant", "PKCE verification failed.")
    asked = form.get("resource")
    if asked and resources.canonical_resource(asked) != got["resource"]:
        return _oauth_error("invalid_target", "The resource does not match the one this code was issued for.")
    subject = await tokens.resolve_subject(store, got["email_hash"])
    if subject is None:
        return _oauth_error("invalid_grant", "This account is suspended.")
    access = tokens.mint_access_token(subject, resource=got["resource"], scope=got["scope"],
                                      client_id=client_id, family_id=got["request_id"])
    refresh = tokens.new_secret(48)
    try:
        await store.refresh_store(tokens.sha256_hex(refresh), got["request_id"], client_id, got["email_hash"],
                                  got["scope"], got["resource"], settings.REFRESH_TTL_S, settings.REFRESH_FAMILY_TTL_S)
    except StoreUnavailable:
        # The access token is good for an hour; failing the whole sign-in now would burn a code the person
        # confirmed for a reason they cannot see. They will be asked to connect again when it expires.
        log.error("oauth_refresh_store_failed -- issuing an access token without a refresh token")
        refresh = None
    log.info("oauth_token_issued grant=authorization_code paid=%s refresh=%s", subject.paid, bool(refresh))
    return _token_response(access, refresh, got["scope"])


async def _grant_refresh_token(store, form: dict) -> JSONResponse:
    old, client_id = form.get("refresh_token"), form.get("client_id")
    if not all(isinstance(v, str) and v for v in (old, client_id)):
        return _oauth_error("invalid_request", "refresh_token and client_id are required.")
    if form.get("resource") and resources.canonical_resource(form.get("resource")) is None:
        return _oauth_error("invalid_target", "That resource is not served here.")
    if len(old) > 512:
        return _oauth_error("invalid_grant", "The refresh token is invalid, expired or revoked.")
    new = tokens.new_secret(48)
    out = await store.refresh_rotate(tokens.sha256_hex(old), tokens.sha256_hex(new), client_id, settings.REFRESH_TTL_S)
    if not out.get("ok"):
        log.info("oauth_refresh_refused reason=%s", out.get("reason"))
        return _oauth_error("invalid_grant", "The refresh token is invalid, expired or revoked.")
    subject = await tokens.resolve_subject(store, out["email_hash"])
    if subject is None:
        await store.refresh_revoke(tokens.sha256_hex(new), client_id)
        return _oauth_error("invalid_grant", "This account is suspended.")
    access = tokens.mint_access_token(subject, resource=out["resource"], scope=out["scope"],
                                      client_id=client_id, family_id=out["family_id"])
    log.info("oauth_token_issued grant=refresh_token paid=%s", subject.paid)
    return _token_response(access, new, out["scope"])


@router.post("/oauth/token")
async def token(request: Request):
    if (r := _off()) is not None:
        return r
    if not limits.check("token_ip", _ipkey(request)):
        return _oauth_error("temporarily_unavailable", "Too many requests.", 429, {"Retry-After": "60"})
    try:
        form = await _form(request)
    except (ValueError, UnicodeDecodeError):
        return _oauth_error("invalid_request", "The body could not be read.")
    grant = form.get("grant_type")
    store = get_store()
    try:
        if grant == "authorization_code":
            return await _grant_authorization_code(store, form)
        if grant == "refresh_token":
            return await _grant_refresh_token(store, form)
    except StoreUnavailable:
        return _unavailable()
    return _oauth_error("unsupported_grant_type", "Supported grants: authorization_code, refresh_token.")


@router.post("/oauth/revoke")
async def revoke(request: Request):
    if (r := _off()) is not None:
        return r
    try:
        form = await _form(request)
    except (ValueError, UnicodeDecodeError):
        return _oauth_error("invalid_request", "The body could not be read.")
    tok, client_id = form.get("token"), form.get("client_id")
    if not all(isinstance(v, str) and v for v in (tok, client_id)):
        return _oauth_error("invalid_request", "token and client_id are required.")
    if limits.check("token_ip", _ipkey(request)) and len(tok) <= 512:
        try:
            await get_store().refresh_revoke(tokens.sha256_hex(tok), client_id)
        except StoreUnavailable:
            return _unavailable()
    # RFC 7009: the answer is the same whether or not the token was known. Access tokens expire within the
    # hour and are not individually revocable; revoking the refresh token ends the chain.
    return _json({})
