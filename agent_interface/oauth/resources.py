"""Which URLs are "us" - the protected-resource identifiers a token may be issued for.

MCP authorization has the client name the server it intends to use the token with (RFC 8707 `resource`), and
the server MUST refuse tokens that were not issued for it. Our one deployment is reachable under several
URLs: the origin's `/mcp`, the site's `/mcp/agent-broker` (Caddy rewrites that to the origin's `/mcp`), and
five capability doors under `/mcp/<door>` on either host. They are all the same resource server, so a token
issued for any of them is valid at all of them - but a token issued for somebody else's server is not, and a
client that asks for a resource we do not serve is told `invalid_target` instead of being handed a token
bound to a name we cannot vouch for.

Pure functions, no I/O.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from agent_interface.oauth import settings

FULL_SERVER_SITE_PATH = "mcp/agent-broker"    # the public URL of the full server on the site host
FULL_SERVER_ORIGIN_PATH = "mcp"               # the same server at the origin


def _doors() -> tuple:
    # Not every door: the ChatGPT door (profiles `oauth: False`) is anonymous and must not publish protected-resource
    # metadata, which would invite a Connect sign-in for tools that need no account.
    from agent_interface import profiles
    return profiles.oauth_profiles()


def _origins() -> frozenset:
    """Every origin (scheme://host) that may appear in a resource identifier."""
    scheme = "https" if settings.is_https() else urlsplit(settings.issuer()).scheme
    origins = {settings.issuer_origin(), "https://api.hatchloop.dev", "https://hatchloop.dev"}
    for h in settings.known_hosts():
        origins.add(f"{scheme}://{h}")
    return frozenset(o.lower() for o in origins)


def _paths() -> frozenset:
    out = {"", FULL_SERVER_ORIGIN_PATH, FULL_SERVER_SITE_PATH}
    out.update(f"mcp/{d}" for d in _doors())
    return frozenset(out)


def canonical_resource(value: Optional[str]) -> Optional[str]:
    """The canonical form of a resource identifier we serve, or None when it is not one.

    Scheme and host are lower-cased, one trailing slash is dropped, and the query/fragment must be absent
    (RFC 8707 forbids a fragment; a query would let a caller mint a token bound to a name of their choosing).
    The result is always an exact member of the catalogue, never the caller's spelling."""
    if not value or not isinstance(value, str) or len(value) > 512:
        return None
    try:
        p = urlsplit(value.strip())
    except ValueError:
        return None
    if p.query or p.fragment or p.username or p.password or not p.scheme or not p.netloc:
        return None
    origin = f"{p.scheme}://{p.netloc}".lower()
    path = p.path.strip("/")
    if origin not in _origins() or path not in _paths():
        return None
    return f"{origin}/{path}" if path else origin


def default_resource() -> str:
    """What a client that sent no `resource` is bound to: the full server at the origin."""
    return f"{settings.issuer_origin()}/{FULL_SERVER_ORIGIN_PATH}"


def is_our_resource(aud: object) -> bool:
    return isinstance(aud, str) and canonical_resource(aud) is not None


def resource_for(host: Optional[str], suffix: str) -> Optional[str]:
    """The `resource` a protected-resource document must state, given the host it is being served for and
    the path suffix of the well-known URL (RFC 9728 section 3.1: the well-known segment is inserted before
    the resource's path, so `/.well-known/oauth-protected-resource/mcp/agent-broker` describes
    `<host>/mcp/agent-broker`).

    None when the combination names nothing we serve - the endpoint then answers 404 rather than reflecting
    the request."""
    suffix = (suffix or "").strip("/")
    host = (host or "").strip().lower()
    if host not in settings.known_hosts():
        host = settings.api_host()
    if suffix == FULL_SERVER_SITE_PATH:
        host = settings.site_host()           # that public path exists only on the site host
    scheme = "https" if settings.is_https() else urlsplit(settings.issuer()).scheme
    candidate = f"{scheme}://{host}/{suffix}" if suffix else f"{scheme}://{host}"
    return canonical_resource(candidate)


def metadata_suffix(request_host: Optional[str], request_path: str) -> tuple:
    """(suffix, host hint) for the protected-resource URL that describes the endpoint a request just reached.

    Caddy rewrites `hatchloop.dev/mcp/agent-broker` to the origin's `/mcp` before the request arrives, so the
    app sees `Host: hatchloop.dev` and path `/mcp` and must translate back to the name the person typed."""
    host = (request_host or "").strip().lower()
    path = (request_path or "/mcp").strip("/")
    if path not in _paths() or path in ("", FULL_SERVER_SITE_PATH):
        path = FULL_SERVER_ORIGIN_PATH          # only a known path is ever written into a header
    site, api = settings.site_host(), settings.api_host()
    if path == FULL_SERVER_ORIGIN_PATH and host == site and site != api:
        return FULL_SERVER_SITE_PATH, site
    return path, (host if host in settings.known_hosts() else "")


def metadata_url(request_host: Optional[str], request_path: str) -> str:
    """Absolute URL of the protected-resource document for the endpoint just reached - always served by the
    ORIGIN, so it works without any change to the site host's reverse proxy (a client may be given a
    metadata URL on any host; RFC 9728 only requires it be HTTPS)."""
    suffix, host = metadata_suffix(request_host, request_path)
    base = f"{settings.issuer()}/.well-known/oauth-protected-resource/{suffix}"
    if host and host != settings.api_host():
        base += f"?host={host}"
    return base
