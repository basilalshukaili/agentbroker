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


# ---------------------------------------------------------------------------
# protocol version and result count (migration 013, verdict item A7)
# ---------------------------------------------------------------------------
# Two more facts about a request, both derived from things a stranger controls (a header, a `_meta` field, a tool
# result) and both returned as something that is either OUR OWN value, a plain number, or None - never caller
# text. The database function refuses anything else, and a refused call loses the whole row.

_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
# Written to be identical in Python and PostgreSQL: migration 013 validates p_protocol_version against it.
PROTOCOL_VERSION_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_PROTOCOL_VERSION = re.compile(PROTOCOL_VERSION_PATTERN)


def safe_protocol_version(value: Any, known: Any) -> Optional[str]:
    """`value` if it is exactly one of the versions in `known` (the ones WE speak), else None. A version we do
    not speak is not stored: it is caller text, and the request that carried it is already labelled by its
    error code (unsupported_protocol_version)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v in known else None


def declared_protocol_version(method: Any, raw_params: Any, headers: Any, known: Any,
                              reply: Any = None) -> Optional[str]:
    """The MCP revision a request ran under, or None when it did not say.

      * `initialize`: the version the server NEGOTIATED (the reply's protocolVersion), the one the
        connection will use; falls back to the header when there is no reply to read.
      * a request whose `_meta` carries a protocolVersion: that declaration and nothing else. If it names a
        version we do not speak the answer is None - the header is not consulted, because the body is what
        the request was judged on.
      * otherwise the MCP-Protocol-Version header, which clients of 2025-06-18 onwards send on every request.

    A stateless server cannot know what an earlier handshake agreed, so a legacy request with no header is
    None ("not stated"), not "old"."""
    if method == "initialize":
        result = reply.get("result") if isinstance(reply, dict) else None
        negotiated = safe_protocol_version(result.get("protocolVersion") if isinstance(result, dict) else None, known)
        if negotiated:
            return negotiated
    meta = raw_params.get("_meta") if isinstance(raw_params, dict) else None
    if isinstance(meta, dict) and _META_PROTOCOL_VERSION in meta:
        return safe_protocol_version(meta[_META_PROTOCOL_VERSION], known)
    header = headers.get("mcp-protocol-version") if isinstance(headers, dict) else None
    return safe_protocol_version(header, known)


# A list method counts what it listed. A tool counts ITS principal list, named here once per tool, because "the
# first list in the result" is a guess and a count nobody can explain is worse than none. Everything else is None.
_LIST_METHOD_FIELD = {
    "tools/list": "tools", "resources/list": "resources",
    "resources/templates/list": "resourceTemplates", "prompts/list": "prompts",
}
_TOOL_LIST_FIELD = {
    "find_business": "businesses", "screen_sanctions": "matches",
    "map_trade_restriction": "restrictions", "lookup_us_contracts": "awards",
}
MAX_RESULT_COUNT = 1_000_000          # the database column's ceiling (migration 013)
_MAX_RESULT_TEXT = 512 * 1024         # a result larger than this is not parsed just to be counted


def _plain_count(value: Any) -> Optional[int]:
    # bool is an int in Python: True must not become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_RESULT_COUNT else None


def result_count_of(method: Any, tool_name: Any, response: Any) -> Optional[int]:
    """How many items a successful reply handed back, or None when that has no plain meaning (a failure, a
    method or tool with no list, a result too large to be worth parsing). Never raises.

    `response` is the JSON-RPC reply the dispatcher built. For a tool the count is the tool's own
    `result_count` when it states one (find_business does) and otherwise the length of its principal list."""
    try:
        if not isinstance(response, dict):
            return None
        result = response.get("result")
        if not isinstance(result, dict):
            return None
        field = _LIST_METHOD_FIELD.get(method) if isinstance(method, str) else None
        if field:
            items = result.get(field)
            return _plain_count(len(items)) if isinstance(items, list) else None
        if method != "tools/call" or tool_name not in _TOOL_LIST_FIELD or result.get("isError"):
            return None
        content = result.get("content")
        first = content[0] if isinstance(content, list) and content else None
        text = first.get("text") if isinstance(first, dict) else None
        if not isinstance(text, str) or len(text) > _MAX_RESULT_TEXT:
            return None
        body = json.loads(text)
        if not isinstance(body, dict):
            return None
        payload = body.get("result") if isinstance(body.get("result"), dict) else body
        stated = _plain_count(payload.get("result_count"))
        if stated is not None:
            return stated
        items = payload.get(_TOOL_LIST_FIELD[tool_name])
        return _plain_count(len(items)) if isinstance(items, list) else None
    except Exception:  # noqa: BLE001 - a malformed result must never cost the row it describes
        return None
