"""
Per-call usage telemetry — durable Supabase logging with human/crawler separation.

Design goals:
- Log every MCP call to `usage_events` (ts, tool, args_hash, ip_hash,
  user_agent, key_id, session_kind, method).
- Classify session_kind:
    'crawler'            — known registry UAs (Glama/Smithery/PulseMCP bots),
                          initialize-only sessions, tools/list-only sessions.
    'anon_agent'         — tools/call without a valid key
    'verified_human_key' — tools/call with a minted key (paid or free-verified)
- Fire-and-forget: NEVER block or raise — a logging failure must never break
  a tool call. Same fail-open pattern as durable_meter.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("smb_broker.usage_logger")

# ---------------------------------------------------------------------------
# Known crawler / registry bot User-Agent substrings (case-insensitive).
# These are the bots that enumerate MCP registries — they hit initialize +
# tools/list but never tools/call with meaningful args.
# ---------------------------------------------------------------------------
_CRAWLER_UA_FRAGMENTS: frozenset[str] = frozenset({
    "glama",
    "smithery",
    "pulsemcp",
    "mcpindex",
    "mcp-crawler",
    "mcp-bot",
    "registry-bot",
    "python-httpx",       # Smithery validator uses httpx with no custom UA
    "python-requests",    # common registry enumerators
    "go-http-client",     # several MCP indexers use Go
    "curl/",              # curl probes (almost always bots)
    # Add more as new registries appear
})

# Methods that on their own never indicate a real agent doing work.
_NON_WORK_METHODS: frozenset[str] = frozenset({
    "initialize",
    "tools/list",
    "resources/list",
    "prompts/list",
    "ping",
})


def classify_session_kind(
    method: str,
    tool_name: Optional[str],
    user_agent: str,
    key_id: Optional[str],
) -> str:
    """
    Classify the caller into one of three buckets:
      'crawler'            — registry bot, no meaningful work
      'anon_agent'         — tools/call with no key
      'verified_human_key' — tools/call with a minted key
    """
    ua_lower = (user_agent or "").lower()

    # UA-based crawler detection
    for fragment in _CRAWLER_UA_FRAGMENTS:
        if fragment in ua_lower:
            return "crawler"

    # Non-work method → crawler (or at least non-agent)
    if method in _NON_WORK_METHODS:
        return "crawler"

    # From here down: this is a tools/call
    if key_id and key_id != "anonymous":
        return "verified_human_key"

    return "anon_agent"


def _hash8(value: str) -> str:
    """8-char prefix of sha256 — enough to track patterns, not enough to reverse."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


async def log_usage_event(
    method: str,
    tool_name: Optional[str],
    arguments: Optional[dict],
    ip: Optional[str],
    user_agent: Optional[str],
    key_id: Optional[str],
) -> None:
    """
    Fire-and-forget insert into `usage_events`. Never raises.
    Designed to be called with asyncio.create_task() so it never delays the response.
    """
    try:
        args_hash = _hash8(str(sorted((arguments or {}).items()))) if arguments else None
        ip_hash = _hash8(ip) if ip else None
        ua = (user_agent or "")[:512]
        session_kind = classify_session_kind(method, tool_name, ua, key_id)
        clean_key_id = (key_id[:64] if key_id and key_id != "anonymous" else None)

        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name or method,
            "args_hash": args_hash,
            "ip_hash": ip_hash,
            "user_agent": ua,
            "key_id": clean_key_id,
            "session_kind": session_kind,
            "method": method,
        }

        from storage.supabase_client import insert_row
        await insert_row("usage_events", row)
    except Exception as exc:  # noqa: BLE001
        logger.debug("usage_log_failed method=%s tool=%s err=%s", method, tool_name, exc)


def fire_log_usage(
    method: str,
    tool_name: Optional[str],
    arguments: Optional[dict],
    ip: Optional[str],
    user_agent: Optional[str],
    key_id: Optional[str],
) -> None:
    """
    Schedule a fire-and-forget usage log. Safe to call from sync or async context.
    The task is scheduled on the running event loop and never awaited.
    If no loop is running (tests), silently skips.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(
                log_usage_event(method, tool_name, arguments, ip, user_agent, key_id)
            )
    except Exception:  # noqa: BLE001
        pass
