"""Turn a finished MCP request into the few facts worth recording about how it went.

Everything here is a pure function over data the handler already holds, and every one of them is
conservative about what it lets into a log line, because the inputs are controlled by strangers:

  * ARGUMENT NAMES, never values. A name is kept only if it looks like an identifier; anything else
    is recorded as "<other>". A caller who puts a secret in a key name gets "<other>", not the secret.
  * A TOOL NAME THAT IS NOT OURS is recorded (that is how we learn what callers expect us to have),
    but only if it is short and identifier-shaped, and never if it looks like a key.
  * CLIENT NAME/VERSION come from the `initialize` handshake, which is a separate HTTP request from
    the `tools/call` that follows (the server is stateless). A small TTL cache keyed by a hash of
    (address, user agent) carries the name forward so a failing `tools/call` can be attributed to
    "Cursor 0.45" rather than just "node".
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Optional

_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.\-/:]{1,64}$")
_CLIENT = re.compile(r"[^\w .\-+/()@]")          # keep word chars, space, and a few version marks
_MAX_ARG_NAMES = 40


def safe_arg_names(arguments: Any) -> list:
    """Sorted argument NAMES, bounded, identifier-shaped, deduplicated. [] for non-objects."""
    if not isinstance(arguments, dict):
        return []
    names = set()
    for key in arguments.keys():
        k = str(key)
        names.add(k if _NAME.match(k) and not _looks_like_secret(k) else "<other>")
    return sorted(names)[:_MAX_ARG_NAMES]


def _looks_like_secret(text: str) -> bool:
    # Long base64/hex-ish runs and JWT/key shapes are never legitimate argument names.
    return len(text) > 40 or text.startswith("eyJ") or bool(re.search(r"[0-9a-f]{32,}", text))


def safe_requested_name(name: Any) -> Optional[str]:
    """A tool name the caller asked for, sanitised for storage; None if absent or unusable."""
    if not isinstance(name, str) or not name:
        return None
    if _TOOL.match(name) and not _looks_like_secret(name):
        return name
    return "<invalid>"


_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_MAX_PATH_SEGMENTS = 4
_MAX_PATH_CHARS = 120


def safe_path(path: Any) -> str:
    """A URL path made safe to store. The path is attacker-controlled text: a caller who pastes their key
    into it (`/mcp/<key>`) would otherwise have the first part of it written to usage_events.detail, kept
    out of harm only by a length cut-off that happened to land before the signature (gate finding, P3).

    Each segment is kept only if it is identifier-shaped and not secret-shaped; anything else becomes
    "<other>". Depth is bounded, and a trailing slash is preserved so `/mcp/x/` and `/mcp/x` stay
    distinguishable in the log."""
    if not isinstance(path, str) or not path:
        return "/"
    trailing = path.endswith("/") and len(path) > 1
    segments = [s for s in path.split("/") if s != ""]
    out = []
    for seg in segments[:_MAX_PATH_SEGMENTS]:
        out.append(seg if _SEGMENT.match(seg) and not _looks_like_secret(seg) else "<other>")
    if len(segments) > _MAX_PATH_SEGMENTS:
        out.append("...")
        trailing = False
    text = "/" + "/".join(out) + ("/" if trailing else "")
    return text[:_MAX_PATH_CHARS]


def safe_client_info(params: Any) -> tuple:
    """(name, version) from an `initialize` request's clientInfo; (None, None) if absent."""
    if not isinstance(params, dict):
        return None, None
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        return None, None

    def clean(v: Any, limit: int) -> Optional[str]:
        if not isinstance(v, str):
            return None
        v = _CLIENT.sub("", v).strip()[:limit]
        return v or None

    return clean(info.get("name"), 128), clean(info.get("version"), 64)


def classify_tool_result(result: Any) -> tuple:
    """(outcome, error_code) for a `tools/call` result dict.

    The MCP convention is `isError: true` with the typed failure inside content[0].text as JSON
    (our receipts carry `reason_code`; _ToolError carries `error_code`). A result that is not an
    error is "ok" even when its status is pending_async or partial: the call did what it says.
    """
    if not isinstance(result, dict) or not result.get("isError"):
        return "ok", None
    code = None
    try:
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        body = json.loads(text) if isinstance(text, str) else None
        if isinstance(body, dict):
            code = body.get("error_code") or body.get("reason_code")
    except Exception:  # noqa: BLE001
        code = None
    return "tool_failure", (str(code)[:64] if code else None)


# ---------------------------------------------------------------------------
# initialize -> later calls
# ---------------------------------------------------------------------------

def client_fingerprint(ip: Optional[str], user_agent: Optional[str]) -> str:
    return hashlib.sha256(f"{ip or ''}|{user_agent or ''}".encode()).hexdigest()[:16]


class ClientRegistry:
    """Bounded, expiring map fingerprint -> (client_name, client_version)."""

    def __init__(self, ttl_s: float = 6 * 3600.0, max_entries: int = 2000) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._data: "OrderedDict[str, tuple]" = OrderedDict()

    def remember(self, fingerprint: str, name: Optional[str], version: Optional[str],
                 now: Optional[float] = None) -> None:
        if not name:
            return
        now = time.monotonic() if now is None else now
        self._data[fingerprint] = (name, version, now)
        self._data.move_to_end(fingerprint)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def recall(self, fingerprint: str, now: Optional[float] = None) -> tuple:
        now = time.monotonic() if now is None else now
        hit = self._data.get(fingerprint)
        if not hit:
            return None, None
        name, version, at = hit
        if now - at > self.ttl_s:
            self._data.pop(fingerprint, None)
            return None, None
        return name, version


CLIENTS = ClientRegistry()
