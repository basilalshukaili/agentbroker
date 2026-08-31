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
    principal_type: Optional[str] = None,
) -> str:
    """
    Classify the caller into one of four buckets:
      'crawler'             — registry bot, no meaningful work
      'anon_agent'          — tools/call with no key
      'verified_agent_key'  — tools/call with a minted key for a system/AI principal
      'verified_human_key'  — tools/call with a minted key for a human/consumer principal

    KEY PRESENCE WINS (FIX 3, 2026-08-23): a call carrying a valid key_id is
    always 'verified_*_key', regardless of User-Agent.  Registry bots and
    CI tooling sometimes use curl/python-httpx UAs while sending a real key;
    labelling those as 'crawler' poisoned the session_kind metric.
    Crawler classification is reserved for keyless traffic only.

    HUMAN vs AGENT (FIX, 2026-09-01): `principal_type` from the validated JWT
    distinguishes a human/consumer subscriber from an autonomous AI agent running
    under a system principal.  Without this, AI-agent sessions were logged as
    'verified_human_key', leading to billing discrepancy and wrong quota tracking.
    'system' principal → 'verified_agent_key'; 'human'/'consumer' → 'verified_human_key'.
    When principal_type is absent (older tokens / free-key callers), the safe
    default is 'verified_human_key' so existing quota paths are unchanged.
    """
    # Key presence wins — checked FIRST, before any UA inspection.
    if key_id and key_id not in ("", "anonymous"):
        # Non-work methods (initialize / tools/list) are still discovery, even
        # when keyed — mark them crawler so they don't inflate work counts.
        if method not in _NON_WORK_METHODS:
            # Distinguish AI agent (system principal) from human subscriber.
            if principal_type in ("system", "business"):
                return "verified_agent_key"
            return "verified_human_key"

    ua_lower = (user_agent or "").lower()

    # UA-based crawler detection (keyless traffic only from here down)
    for fragment in _CRAWLER_UA_FRAGMENTS:
        if fragment in ua_lower:
            return "crawler"

    # Non-work method => crawler / non-agent
    if method in _NON_WORK_METHODS:
        return "crawler"

    # tools/call with no key
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
    principal_type: Optional[str] = None,
) -> None:
    """
    Fire-and-forget insert into `usage_events`. Never raises.
    Designed to be called with asyncio.create_task() so it never delays the response.
    """
    try:
        args_hash = _hash8(str(sorted((arguments or {}).items()))) if arguments else None
        ip_hash = _hash8(ip) if ip else None
        ua = (user_agent or "")[:512]
        session_kind = classify_session_kind(method, tool_name, ua, key_id,
                                             principal_type=principal_type)
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
    principal_type: Optional[str] = None,
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
                log_usage_event(method, tool_name, arguments, ip, user_agent,
                                key_id, principal_type=principal_type)
            )
    except Exception:  # noqa: BLE001
        pass
