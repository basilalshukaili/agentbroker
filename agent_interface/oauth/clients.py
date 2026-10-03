"""Who is asking to connect: client identification, redirect-URI validation, and the SSRF-safe fetch of a
Client ID Metadata Document.

TWO WAYS A CLIENT IDENTIFIES ITSELF (MCP authorization, client registration):

  * A Client ID Metadata Document (CIMD): `client_id` is an HTTPS URL; WE fetch it, check the document says
    it is that URL, and trust its `redirect_uris`. No registration, no table. Claude and ChatGPT use this.
  * Dynamic Client Registration (RFC 7591): the client POSTs its redirect URIs to /oauth/register and gets a
    `dcr_...` id back. Deprecated in the current specification but still how Gemini, Grok and older SDKs
    connect, so it stays.

Both produce the same `ClientInfo`, and both are SELF-ASSERTED: nothing here proves a client is who its name
says. So the consent page shows what cannot be faked cheaply - the host of the client_id URL, and the host the
person will be sent back to - and calls the self-chosen name exactly that.

THE SSRF GUARD. Fetching a URL a stranger chose is the one outbound request on this service whose target is
not ours. The fetch therefore: accepts only https on port 443 with a host name (no IP literals, no localhost,
no internal suffixes); resolves the name ITSELF and refuses unless EVERY address is globally routable; then
connects to the address it checked (a name that resolves differently a millisecond later cannot redirect the
request - DNS rebinding), presenting the original name for TLS; follows no redirects; caps the body at 64 KiB
and the time at 4 s; and is rate-limited per caller and per target host (agent_interface/oauth/limits.py).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit

from agent_interface.oauth import settings

log = logging.getLogger("smb_broker.oauth.clients")

MAX_REDIRECT_URIS = 10
MAX_DOC_BYTES = 64 * 1024
FETCH_TIMEOUT_S = 4.0

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss", "chrome",
                "view-source", "intent", "content", "resource", "gopher", "jar", "mailto", "tel", "sms"}
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]{0,62}$")
_CTRL = re.compile(r"[\x00-\x20\x7f-\x9f]")
_BAD_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".test",
                      ".example", ".invalid")


class ClientError(Exception):
    """A client could not be established. `code` is the OAuth error to use if one is ever sent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class ClientInfo:
    client_id: str
    kind: str                    # "cimd" | "dcr"
    name: str                    # self-asserted; shown as such
    host: str                    # host of the client_id URL (cimd); "" for dcr
    redirect_uris: tuple

    @property
    def label(self) -> str:
        return self.host if self.kind == "cimd" and self.host else (self.name or "an app")


# ---------------------------------------------------------------------------
# redirect URIs
# ---------------------------------------------------------------------------

def _hostname(p) -> str:
    try:
        return (p.hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_uri(uri: str) -> bool:
    try:
        p = urlsplit(uri)
    except ValueError:
        return False
    return p.scheme == "http" and _hostname(p) in _LOOPBACK_HOSTS


def redirect_host(uri: str) -> str:
    """What the consent page shows as 'where you will be sent back to'."""
    try:
        p = urlsplit(uri)
    except ValueError:
        return ""
    if p.scheme in ("http", "https"):
        return _hostname(p) or p.netloc
    return f"{p.scheme}://{p.netloc or p.path.lstrip('/').split('/')[0]}"


def check_redirect_uri(uri: object, *, allow_private_scheme: bool) -> Optional[str]:
    """None when acceptable, else the reason. OAuth 2.1 / MCP: redirect URIs are HTTPS or loopback; native
    apps that register a private-use scheme (RFC 8252 section 7.1) are allowed only for dynamic registration."""
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        return "redirect URI must be a string of at most 2048 characters"
    if _CTRL.search(uri):
        return "redirect URI must not contain spaces or control characters"
    try:
        p = urlsplit(uri)
    except ValueError:
        return "redirect URI is not a valid URL"
    if p.fragment or "#" in uri:
        return "redirect URI must not contain a fragment"
    if p.username or p.password:
        return "redirect URI must not contain credentials"
    scheme = p.scheme.lower()
    if scheme == "https":
        return None if _hostname(p) else "https redirect URI needs a host"
    if scheme == "http":
        return None if _hostname(p) in _LOOPBACK_HOSTS else "http redirect URIs are allowed only for localhost, 127.0.0.1 or [::1]"
    if not allow_private_scheme:
        return "redirect URI must be https or a loopback http URL"
    if scheme in _BAD_SCHEMES or not _SCHEME_RE.match(scheme):
        return f"redirect URI scheme {scheme!r} is not allowed"
    if not (p.netloc or p.path.strip("/")):
        return "redirect URI has no destination"
    return None


def redirect_matches(registered: Iterable[str], requested: str) -> bool:
    """EXACT match against a registered URI. The one relaxation is RFC 8252 section 7.3: for a loopback
    redirect the port is chosen by the client at run time, so it is ignored (scheme, host, path and query must
    still agree). Claude Code declares both `http://localhost/callback` and `http://127.0.0.1/callback`; each
    matches only itself, never the other."""
    registered = list(registered)
    if requested in registered:
        return True
    if not is_loopback_uri(requested):
        return False
    rq = urlsplit(requested)
    for reg in registered:
        if not is_loopback_uri(reg):
            continue
        rg = urlsplit(reg)
        if (_hostname(rg) == _hostname(rq) and rg.path == rq.path and rg.query == rq.query):
            return True
    return False


# ---------------------------------------------------------------------------
# dynamic registration
# ---------------------------------------------------------------------------

def validate_registration(body: object) -> tuple:
    """(normalised, None) or (None, (error_code, description)) for a POST /oauth/register body."""
    if not isinstance(body, dict):
        return None, ("invalid_client_metadata", "The body must be a JSON object.")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not (1 <= len(uris) <= MAX_REDIRECT_URIS):
        return None, ("invalid_redirect_uri", f"redirect_uris must list 1 to {MAX_REDIRECT_URIS} URIs.")
    seen, clean = set(), []
    for u in uris:
        problem = check_redirect_uri(u, allow_private_scheme=True)
        if problem:
            return None, ("invalid_redirect_uri", problem)
        if u not in seen:
            seen.add(u)
            clean.append(u)
    method = body.get("token_endpoint_auth_method")
    if method not in (None, "none"):
        return None, ("invalid_client_metadata",
                      "Only public clients are supported: token_endpoint_auth_method must be 'none' "
                      "(the proof of possession is PKCE).")
    grants = body.get("grant_types")
    if grants is not None:
        if (not isinstance(grants, list) or "authorization_code" not in grants
                or not set(grants) <= {"authorization_code", "refresh_token"}):
            return None, ("invalid_client_metadata",
                          "grant_types must include authorization_code and may include refresh_token.")
    rtypes = body.get("response_types")
    if rtypes is not None and rtypes != ["code"]:
        return None, ("invalid_client_metadata", "response_types must be [\"code\"].")
    name = body.get("client_name")
    if name is not None and not isinstance(name, str):
        return None, ("invalid_client_metadata", "client_name must be a string.")
    name = _CTRL.sub(" ", name or "").strip()[:100]
    return {"client_name": name, "redirect_uris": clean}, None


# ---------------------------------------------------------------------------
# Client ID Metadata Documents
# ---------------------------------------------------------------------------

Resolver = Callable[[str], Awaitable[list]]


async def _default_resolver(host: str) -> list:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def _check_client_id_url(url: str):
    """Parsed URL, or a ClientError. The URL form rules of the CIMD draft plus the SSRF host rules."""
    if not isinstance(url, str) or len(url) > 512 or _CTRL.search(url):
        raise ClientError("invalid_client", "client_id is not a usable URL.")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise ClientError("invalid_client", "client_id is not a valid URL.")
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or p.username or p.password or p.fragment:
        raise ClientError("invalid_client", "A metadata-document client_id must be an https URL.")
    if port not in (None, 443):
        raise ClientError("invalid_client", "A metadata-document client_id must use the standard https port.")
    if p.path in ("", "/"):
        raise ClientError("invalid_client", "A metadata-document client_id must include a path.")
    if not host.isascii() or "." not in host or host.endswith(_BAD_HOST_SUFFIXES) or host == "localhost":
        raise ClientError("invalid_client", "That client_id host cannot be used.")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return p
    raise ClientError("invalid_client", "A client_id host must be a name, not an IP address.")


def _parse_cache_ttl(headers) -> float:
    cc = (headers.get("cache-control") or "").lower()
    m = re.search(r"max-age=(\d+)", cc)
    ttl = float(m.group(1)) if m else 300.0
    if "no-store" in cc or "no-cache" in cc:
        ttl = 60.0
    return max(60.0, min(ttl, 3600.0))


class MetadataFetcher:
    """Fetches, validates and caches Client ID Metadata Documents. See the module docstring for the guard."""

    def __init__(self, resolver: Optional[Resolver] = None, transport=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._resolve = resolver or _default_resolver
        self._transport = transport
        self._clock = clock
        self._cache: dict = {}

    def clear(self) -> None:
        self._cache.clear()

    async def get(self, url: str) -> dict:
        p = _check_client_id_url(url)
        hit = self._cache.get(url)
        now = self._clock()
        if hit and hit[0] > now:
            if isinstance(hit[1], ClientError):
                raise hit[1]
            return hit[1]
        try:
            doc, ttl = await self._fetch(url, p)
        except ClientError as exc:
            self._remember(url, exc, now + 30.0)
            raise
        self._remember(url, doc, now + ttl)
        return doc

    def _remember(self, url, value, until) -> None:
        if len(self._cache) >= 1000:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:200]:
                self._cache.pop(k, None)
        self._cache[url] = (until, value)

    async def _fetch(self, url: str, p) -> tuple:
        host = p.hostname.lower()
        try:
            addrs = await asyncio.wait_for(self._resolve(host), timeout=FETCH_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            raise ClientError("invalid_client", "The client_id host could not be resolved.")
        if not addrs:
            raise ClientError("invalid_client", "The client_id host could not be resolved.")
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                raise ClientError("invalid_client", "The client_id host resolved to an unusable address.")
            if not ip.is_global or ip.is_multicast:
                raise ClientError("invalid_client", "The client_id host is not a public address.")
        pinned = addrs[0]
        netloc = f"[{pinned}]" if ":" in pinned else pinned
        target = f"https://{netloc}{p.path}" + (f"?{p.query}" if p.query else "")
        import httpx
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=FETCH_TIMEOUT_S,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream(
                        "GET", target,
                        headers={"Host": host, "Accept": "application/json", "User-Agent": "HatchLoop-OAuth/1"},
                        extensions={"sni_hostname": host}) as resp:
                    if resp.status_code != 200:
                        raise ClientError("invalid_client", f"The client metadata URL answered {resp.status_code}.")
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ctype:
                        raise ClientError("invalid_client", "The client metadata URL did not return JSON.")
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_DOC_BYTES:
                            raise ClientError("invalid_client", "The client metadata document is too large.")
                    ttl = _parse_cache_ttl(resp.headers)
        except ClientError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.info("oauth_cimd_fetch_failed host=%s err=%s", host, type(exc).__name__)
            raise ClientError("invalid_client", "The client metadata document could not be fetched.")
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ClientError("invalid_client", "The client metadata document is not valid JSON.")
        return self._validate_document(url, doc), ttl

    @staticmethod
    def _validate_document(url: str, doc: object) -> dict:
        if not isinstance(doc, dict):
            raise ClientError("invalid_client", "The client metadata document must be a JSON object.")
        if doc.get("client_id") != url:
            raise ClientError("invalid_client", "The document's client_id does not match the URL it was fetched from.")
        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not (1 <= len(uris) <= MAX_REDIRECT_URIS):
            raise ClientError("invalid_client", "The document must list 1 to 10 redirect_uris.")
        for u in uris:
            problem = check_redirect_uri(u, allow_private_scheme=False)
            if problem:
                raise ClientError("invalid_client", f"The document's {problem}.")
        method = doc.get("token_endpoint_auth_method")
        if "client_secret" in doc or (isinstance(method, str) and method.startswith("client_secret")):
            raise ClientError("invalid_client", "A metadata-document client must not use a shared secret.")
        grants = doc.get("grant_types")
        if grants is not None and (not isinstance(grants, list) or "authorization_code" not in grants):
            raise ClientError("invalid_client", "The document must allow the authorization_code grant.")
        name = doc.get("client_name")
        name = _CTRL.sub(" ", name).strip()[:100] if isinstance(name, str) else ""
        return {"client_name": name, "redirect_uris": list(uris)}


FETCHER = MetadataFetcher()


async def resolve_client(store, client_id: object, fetcher: Optional[MetadataFetcher] = None) -> ClientInfo:
    """The client a /authorize request is from, or a ClientError. Never raises anything else for a bad id."""
    if not isinstance(client_id, str) or not client_id or len(client_id) > 2048:
        raise ClientError("invalid_client", "client_id is missing.")
    if client_id.startswith("https://"):
        doc = await (fetcher or FETCHER).get(client_id)
        host = (urlsplit(client_id).hostname or "").lower()
        return ClientInfo(client_id=client_id, kind="cimd", name=doc["client_name"], host=host,
                          redirect_uris=tuple(doc["redirect_uris"]))
    if client_id.startswith(settings.DCR_PREFIX):
        rec = await store.client_get(client_id)
        if not rec:
            raise ClientError("invalid_client", "That client_id is not registered.")
        return ClientInfo(client_id=client_id, kind="dcr", name=str(rec.get("client_name") or ""), host="",
                          redirect_uris=tuple(rec.get("redirect_uris") or ()))
    raise ClientError("invalid_client", "That client_id is not recognised.")
