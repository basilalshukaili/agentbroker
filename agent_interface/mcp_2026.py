"""MCP protocol revision 2026-07-28 ("modern era"), served alongside the legacy `initialize` handshake.

WHY THIS EXISTS (measured 2026-10-03, in the HatchLoop demand review kept outside this repo): 652 `server/discover`
calls from 20 distinct callers got -32601 in 47 hours, and (re-counted from the access log, 2026-09-29 to
2026-10-03) 939 POSTs to the MCP doors already carried `MCP-Protocol-Version: 2026-07-28`. The revision makes `server/discover` a server MUST and removes the
`initialize` handshake, so a discover-first client could not complete a handshake with us at all.

WHAT THE REVISION CHANGES FOR A TOOLS SERVER (https://modelcontextprotocol.io/specification/2026-07-28):

  * There is no handshake and no session. Every request carries `_meta` with
    `io.modelcontextprotocol/protocolVersion` and `io.modelcontextprotocol/clientCapabilities` (required)
    and `io.modelcontextprotocol/clientInfo` (SHOULD). On Streamable HTTP the version is also the
    `MCP-Protocol-Version` header, and `Mcp-Method` / `Mcp-Name` mirror the body.
  * `server/discover` returns {supportedVersions, capabilities, instructions?, _meta.serverInfo} and is
    cacheable. A version the server does not speak is answered with UnsupportedProtocolVersion (-32022,
    HTTP 400, data.supported / data.requested). A header that disagrees with the body is HeaderMismatch
    (-32020, HTTP 400). A missing required `_meta` field is -32602 (HTTP 400). An unknown method is -32601
    with HTTP 404.
  * Every result carries `resultType` ("complete"); list/read/discover results also carry `ttlMs` and
    `cacheScope`; servers SHOULD put `io.modelcontextprotocol/serverInfo` in the result `_meta`.

HOW A DUAL-ERA SERVER TELLS THE ERAS APART (spec, "Versioning and Compatibility"): a request whose `_meta`
declares a modern version is served statelessly under this revision; `initialize` selects the legacy
semantics. We follow that, with three deliberate leniencies that exist because of what callers really send:

  1. `initialize` is ALWAYS legacy, whatever headers or `_meta` ride along. Several scanners send
     `MCP-Protocol-Version: 2026-07-28` with `initialize` (no `Mcp-Method` header); answering them with a
     modern error would turn a working handshake into a failure.
  2. A request that has the 2026-07-28 header but NO `_meta` envelope ("header-only", what scanner-B,
     scanner-C and others send for `tools/list` today) is still served, in the modern shape, with the
     headers that ARE present validated. Rejecting it would break callers that work now. A request that
     carries the `_meta` envelope has opted in and gets the revision's full validation.
  3. `server/discover` is answered for every caller, with or without a version, because it is the probe
     that tells a client what we speak. Only an UNSUPPORTED version on it is refused (scanner-A sends
     `1999-01-01` and expects exactly that).

Header validation is applied to MODERN requests only. scanner-I sends an `Mcp-Method` header on
2025-06-18 requests; those are legacy and are left alone.

Everything here is a pure function over data the dispatcher already holds. It imports nothing from the
dispatcher (the legacy version tuple is passed in), so it can be tested without a server.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# Wire names
# ---------------------------------------------------------------------------

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Versions that convey version, identity and capabilities per request. The legacy tuple (the versions
# `initialize` negotiates) stays in mcp_server.SUPPORTED_PROTOCOL_VERSIONS and must NOT gain these: an
# `initialize` that answered "2026-07-28" would announce a handshake the revision removed.
MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)

# Error codes the revision allocates in the -32020..-32099 range reserved for the MCP specification.
ERR_HEADER_MISMATCH = -32020
ERR_MISSING_REQUIRED_CLIENT_CAPABILITY = -32021   # defined for completeness; we never need a client capability
ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022
ERR_INVALID_PARAMS = -32602

HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404

# The methods whose result is cacheable (spec: server/utilities/caching). Freshness hints are deliberately
# short for anything that can change with a deploy or a configuration change (tools/list annotates delivery
# channels from this deployment's configuration at request time), and an hour for literals in the code.
_FIVE_MINUTES_MS = 5 * 60 * 1000
_ONE_HOUR_MS = 60 * 60 * 1000
CACHE_TTL_MS = {
    "server/discover": _FIVE_MINUTES_MS,
    "tools/list": _FIVE_MINUTES_MS,
    "resources/read": _FIVE_MINUTES_MS,
    "prompts/list": _ONE_HOUR_MS,
    "resources/list": _ONE_HOUR_MS,
    "resources/templates/list": _ONE_HOUR_MS,
}

# Methods the revision REMOVED that we still answer for callers that have not opted in to it (legacy requests
# and the header-only shape below). `ping` is the one: monitors use it and answering costs nothing. A request
# that carries the revision's own `_meta` envelope has opted in, no conforming client sends `ping` there, and
# the spec's answer is 404 / -32601, so that is what it gets.
REMOVED_METHODS = frozenset({"ping"})

# `Mcp-Name` mirrors one body field, for exactly these methods (spec: Standard Request Headers).
_NAME_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

# The spec defines the Base64 sentinel by its two ends ALONE: a value that starts with `=?base64?` and ends with
# `?=` is one, which is why a client must itself Base64-encode a plain value that happens to look like it.
# So whether a header IS a sentinel must not depend on whether its payload is valid Base64.
_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="
_UNSAFE_FOR_MESSAGE = re.compile(r"[^0-9A-Za-z._:/ -]")


def all_versions(legacy_versions: Sequence[str]) -> list:
    """Every version we speak, newest first: the modern ones, then the legacy ones."""
    return list(MODERN_PROTOCOL_VERSIONS) + [v for v in legacy_versions if v not in MODERN_PROTOCOL_VERSIONS]


def _show(value: Any) -> str:
    """A caller-supplied value made safe to put in an error message: short, printable, no markup."""
    return _UNSAFE_FOR_MESSAGE.sub("?", str(value))[:40]


def safe_text(value: Any, limit: int = 40) -> str:
    """Caller text made safe to put in an error message: markup-free and at most `limit` characters. The
    dispatcher's own refusals use it for an unknown method name (a method name of more than 64 characters is not
    a method name)."""
    return _UNSAFE_FOR_MESSAGE.sub("?", str(value))[:limit]


# ---------------------------------------------------------------------------
# What a response carries back to the HTTP layer
# ---------------------------------------------------------------------------

class RpcReply(dict):
    """A JSON-RPC response object that also remembers the HTTP status Streamable HTTP wants for it.

    It is a plain dict in every other respect (equality, JSON encoding, indexing), so every existing
    caller of `handle_mcp_request` keeps working; only the HTTP layer reads `.http_status`. 200 is the
    default and the only status the legacy era ever used."""

    http_status = 200


def with_status(reply: dict, status: int) -> RpcReply:
    out = RpcReply(reply)
    out.http_status = status
    return out


def status_of(reply: Any) -> int:
    return getattr(reply, "http_status", 200) if isinstance(reply, dict) else 200


# ---------------------------------------------------------------------------
# Era resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Era:
    """How to serve one request.

    modern      -- answer in the 2026-07-28 shape (resultType, serverInfo in _meta, cache hints).
    envelope    -- the body carried a `_meta` protocolVersion (the client opted in to the revision's rules).
    version     -- the version the request declared (envelope first, then header), or None.
    client_info -- the `_meta` clientInfo object if one was sent, else None (for the outcome log).
    """

    modern: bool = False
    envelope: bool = False
    version: Optional[str] = None
    client_info: Optional[dict] = None


@dataclass(frozen=True)
class Rejection:
    """A request the revision says to refuse, with the HTTP status and JSON-RPC error that goes with it."""

    http_status: int
    code: int
    message: str
    data: Optional[dict]
    error_code: str            # the short label recorded in usage_events.error_code
    client_info: Optional[dict] = None
    # WHY, in a closed vocabulary of OUR OWN words (the REASON_* constants below), for usage_events.detail. Never
    # caller text. `header_mismatch` alone could not tell "the client omitted a header" from "the client sent one
    # that disagrees with its body", which is what decides whether the server is too strict or the client is wrong.
    reason: Optional[str] = None


def decode_header_value(raw: str) -> Optional[str]:
    """Undo the `=?base64?...?=` sentinel the spec uses for header values that are not plain ASCII.
    None when the sentinel is present but its payload is not valid Base64 / UTF-8."""
    if not (len(raw) >= len(_SENTINEL_PREFIX) + len(_SENTINEL_SUFFIX)
            and raw.startswith(_SENTINEL_PREFIX) and raw.endswith(_SENTINEL_SUFFIX)):
        return raw
    payload = raw[len(_SENTINEL_PREFIX):-len(_SENTINEL_SUFFIX)]
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 - any decode failure means "not a valid value"
        return None


# The closed vocabulary behind Rejection.reason (and `why=` in usage_events.detail). Short on purpose: the detail
# column holds 64 characters and also carries the door and protocol version.
REASON_VERSION_HEADER_MISSING = "hdr-ver-miss"
REASON_VERSION_HEADER_DIFFERS = "hdr-ver-diff"
REASON_VERSION_HEADER_VS_META = "hdr-ver-vs-meta"
REASON_METHOD_HEADER_MISSING = "hdr-meth-miss"
REASON_METHOD_HEADER_DIFFERS = "hdr-meth-diff"
REASON_NAME_HEADER_MISSING = "hdr-name-miss"
REASON_NAME_HEADER_DIFFERS = "hdr-name-diff"
REASON_NAME_HEADER_INVALID = "hdr-name-bad"
REASON_NAME_HEADER_NO_BODY = "hdr-name-nobody"
REASON_META_VERSION_INVALID = "meta-ver-bad"
REASON_META_CAPABILITIES_MISSING = "meta-caps-miss"


def _invalid_meta(message: str, key: str, client_info: Optional[dict], reason: Optional[str] = None) -> Rejection:
    return Rejection(HTTP_BAD_REQUEST, ERR_INVALID_PARAMS, message, {"field": f"params._meta.{key}"},
                     "invalid_meta", client_info, reason)


def _unsupported(requested: Any, versions: Sequence[str], client_info: Optional[dict]) -> Rejection:
    return Rejection(
        HTTP_BAD_REQUEST, ERR_UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
        {"supported": list(versions), "requested": str(requested)[:64]},
        "unsupported_protocol_version", client_info)


def _header_mismatch(message: str, client_info: Optional[dict], reason: Optional[str] = None) -> Rejection:
    return Rejection(HTTP_BAD_REQUEST, ERR_HEADER_MISMATCH, f"Header mismatch: {message}", None,
                     "header_mismatch", client_info, reason)


def _check_headers(method: str, raw_params: Any, headers: dict, declared: str, *, strict: bool,
                   client_info: Optional[dict]) -> Optional[Rejection]:
    """The Streamable HTTP request-metadata rules. `headers` is lower-cased.

    strict=True  -- the body carried the modern envelope: every required header must be present and agree.
    strict=False -- header-only modern request: validate what IS present, demand nothing that is absent.

    Values are never echoed back verbatim (a header is attacker-controlled text); the message names the
    header and the field, and shows the protocol versions only after `_show` has bounded them."""
    version_header = headers.get("mcp-protocol-version")
    if strict:
        if not isinstance(version_header, str) or not version_header.strip():
            return _header_mismatch("required header MCP-Protocol-Version is missing", client_info,
                                    REASON_VERSION_HEADER_MISSING)
        if version_header.strip() != declared:
            return _header_mismatch(
                f"MCP-Protocol-Version header ({_show(version_header.strip())}) does not match "
                f"_meta protocolVersion ({_show(declared)})", client_info, REASON_VERSION_HEADER_DIFFERS)

    method_header = headers.get("mcp-method")
    if method_header is None:
        if strict:
            return _header_mismatch("required header Mcp-Method is missing", client_info,
                                    REASON_METHOD_HEADER_MISSING)
    elif str(method_header).strip() != method:
        return _header_mismatch("Mcp-Method header does not match the request method", client_info,
                                REASON_METHOD_HEADER_DIFFERS)

    field = _NAME_FIELD.get(method)
    if field:
        body_value = raw_params.get(field) if isinstance(raw_params, dict) else None
        name_header = headers.get("mcp-name")
        if name_header is None:
            # Only demand it when there is a body value to mirror; otherwise the handler's own
            # "missing name" error is the more useful one.
            if strict and isinstance(body_value, str):
                return _header_mismatch("required header Mcp-Name is missing", client_info,
                                        REASON_NAME_HEADER_MISSING)
        else:
            decoded = decode_header_value(str(name_header))
            if decoded is None:
                return _header_mismatch("Mcp-Name header is not a valid value", client_info,
                                        REASON_NAME_HEADER_INVALID)
            if isinstance(body_value, str):
                if decoded != body_value:
                    return _header_mismatch(f"Mcp-Name header does not match params.{field}", client_info,
                                            REASON_NAME_HEADER_DIFFERS)
            elif strict:
                # The header names something the body does not carry (no params.name, or one that is not a
                # string): a header that does not mirror the body is a mismatch, not an invitation to guess.
                return _header_mismatch(f"Mcp-Name header is present but params.{field} is not a string",
                                        client_info, REASON_NAME_HEADER_NO_BODY)
    return None


def batch_declares_modern(batch: Any) -> bool:
    """Does any member of a JSON-RPC batch carry the 2026-07-28 envelope? The revision's transport says the POST
    body is ONE request or notification; a batch has one set of HTTP headers that cannot mirror several bodies,
    so serving modern members from an array would skip every header rule the revision exists to enforce."""
    if not isinstance(batch, list):
        return False
    for member in batch:
        params = member.get("params") if isinstance(member, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        declared = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
        if isinstance(declared, str) and declared in MODERN_PROTOCOL_VERSIONS:
            return True
    return False


def resolve_era(method: Any, raw_params: Any, headers: dict, legacy_versions: Sequence[str], *,
                check_headers: bool = True) -> "Era | Rejection":
    """Decide how a request is served, or refuse it. See the module docstring for the three leniencies.

    `raw_params` is the request's own `params` (before the dispatcher injects `_profile`), `headers` the
    lower-cased HTTP headers, `legacy_versions` the versions `initialize` negotiates. `check_headers` is
    False for the members of a JSON-RPC batch: one HTTP request carries one set of headers, so they cannot
    mirror several bodies (and the revision removes batching from the transport anyway)."""
    if not isinstance(method, str) or method == "initialize":
        return Era()

    meta = raw_params.get("_meta") if isinstance(raw_params, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    info = meta.get(META_CLIENT_INFO)
    client_info = info if isinstance(info, dict) else None
    versions = all_versions(legacy_versions)

    header_version = headers.get("mcp-protocol-version")
    header_version = header_version.strip() if isinstance(header_version, str) and header_version.strip() else None

    if META_PROTOCOL_VERSION in meta:
        declared = meta[META_PROTOCOL_VERSION]
        if not isinstance(declared, str) or not declared:
            return _invalid_meta(f"_meta {META_PROTOCOL_VERSION} must be a non-empty string",
                                 META_PROTOCOL_VERSION, client_info, REASON_META_VERSION_INVALID)
        if declared not in versions:
            return _unsupported(declared, versions, client_info)
        if declared not in MODERN_PROTOCOL_VERSIONS:
            # A legacy version named in _meta: served exactly as the legacy transport always was - unless the
            # HTTP header claims the revision, which contradicts the body (the revision's own HeaderMismatch).
            if check_headers and header_version in MODERN_PROTOCOL_VERSIONS:
                return _header_mismatch(
                    f"MCP-Protocol-Version header ({_show(header_version)}) does not match "
                    f"_meta protocolVersion ({_show(declared)})", client_info, REASON_VERSION_HEADER_VS_META)
            return Era(modern=False, envelope=True, version=declared, client_info=client_info)
        if method != "server/discover" and not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
            return _invalid_meta(
                f"_meta {META_CLIENT_CAPABILITIES} is required on every request (an object; {{}} if the "
                f"client has none)", META_CLIENT_CAPABILITIES, client_info, REASON_META_CAPABILITIES_MISSING)
        if check_headers:
            rejected = _check_headers(method, raw_params, headers, declared, strict=True, client_info=client_info)
            if rejected:
                return rejected
        return Era(modern=True, envelope=True, version=declared, client_info=client_info)

    # No envelope. The header is then the only claim about the version.
    if header_version is not None:
        if header_version in MODERN_PROTOCOL_VERSIONS:
            if check_headers:
                rejected = _check_headers(method, raw_params, headers, header_version, strict=False,
                                          client_info=None)
                if rejected:
                    return rejected
            return Era(modern=True, envelope=False, version=header_version)
        if method == "server/discover" and header_version not in versions:
            # server/discover exists only in the revision, so a version header on it IS a claim about the
            # revision's negotiation: answer it the way the revision says.
            return _unsupported(header_version, versions, None)
    return Era()


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------

def server_info(name: str, version: str) -> dict:
    return {"name": name, "version": version}


# What a tools server can truthfully declare. `logging` is omitted on purpose: the revision deprecates it,
# we have no logging/setLevel and emit no log notifications. Nothing here can change at runtime, and there
# is no subscriptions/listen stream, so every listChanged / subscribe flag is false.
def discover_capabilities() -> dict:
    return {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def build_discover_result(info: dict, instructions: Optional[str], legacy_versions: Sequence[str],
                          capabilities: Optional[dict] = None) -> dict:
    """The `server/discover` result. Public and cacheable: it says nothing about who is asking.

    `capabilities` is what the endpoint declared in `initialize` when it declares fewer than the default (the
    ChatGPT door serves tools only); omitted, it is the default every other endpoint has always answered."""
    result = {
        "resultType": "complete",
        "supportedVersions": all_versions(legacy_versions),
        "capabilities": capabilities if capabilities is not None else discover_capabilities(),
        "_meta": {META_SERVER_INFO: dict(info)},
        "ttlMs": CACHE_TTL_MS["server/discover"],
        "cacheScope": "public",
    }
    if instructions:
        result["instructions"] = instructions
    return result


def decorate_result(method: str, result: Any, info: dict) -> Any:
    """The 2026-07-28 shape of a successful result: `resultType`, `_meta` serverInfo, and the cache hints
    for the methods that carry them.

    Returns a NEW dict and never touches `result`: a tools/call result can be the object the idempotency
    gate stored for replay, and decorating it in place would bake this request's fields into every later
    replay (and into legacy-era replays of the same key)."""
    if not isinstance(result, dict):
        return result
    out: dict = {"resultType": "complete"}
    out.update(result)
    meta = dict(out["_meta"]) if isinstance(out.get("_meta"), dict) else {}
    meta.setdefault(META_SERVER_INFO, dict(info))
    out["_meta"] = meta
    ttl = CACHE_TTL_MS.get(method)
    if ttl is not None:
        out.setdefault("ttlMs", ttl)
        out.setdefault("cacheScope", "public")
    return out


def make_private(result: Any) -> None:
    """A result that has become specific to its caller must not be cached for anyone else.

    Used when a failing key makes `tools/list` carry that caller's `auth_warning`: a shared cache that
    stored the warning would show it to the next, innocent caller. No-op on a legacy-shaped result."""
    if isinstance(result, dict) and "cacheScope" in result:
        result["cacheScope"] = "private"
        result["ttlMs"] = 0
