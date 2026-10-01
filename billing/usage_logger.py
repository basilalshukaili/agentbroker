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

AUDIT-2026-09-28: fire_log_usage scheduled its background write with
`asyncio.ensure_future(...)` and kept no reference to the returned Task.
CPython's own docs warn that a task nothing else references "may get
garbage collected at any time, even before it's done" (the event loop only
holds a weak reference), and the one place a failure WAS logged used
`logger.debug`, which the production container's LOG_LEVEL=INFO drops
entirely. Both are fixed here: `_pending_tasks` keeps a strong reference to
every scheduled write until it finishes (removed via `add_done_callback`,
never left to accumulate), and every failure path — an exception inside
`log_usage_event`, `insert_row` returning None, or the task never completing
at all — is logged at ERROR and recorded in `_stats`, which
`get_usage_logger_health()` exposes for a health check to read. See
agent_interface/self_test.py's `_check_metering_pipeline`.

AUDIT-2026-09-28 (cont'd, tm_requirements row 1181): the diagnostic half of
the fix above (ERROR-level logging) is what surfaced the REAL, second cause
the GC bug had been masking: `usage_events` has RLS enabled with zero
policies, and this container runs with only SUPABASE_ANON_KEY, which holds
an INSERT grant but does not bypass RLS. Every insert was attempted and
rejected. Fixed by routing through the SECURITY DEFINER RPC function
`usage_events_insert` (sql/agentbroker/003_usage_billing_security_definer_
rpc.sql) instead of a direct table insert — see log_usage_event()'s
docstring.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("smb_broker.usage_logger")

# ---------------------------------------------------------------------------
# Metering health — so a silently-failing background write can be OBSERVED.
# ---------------------------------------------------------------------------
# Strong references to every in-flight fire-and-forget task. Without this,
# nothing but the event loop's own (weak) bookkeeping keeps a scheduled task
# alive; discarded on completion via the done-callback below, so this never
# grows unbounded.
_pending_tasks: set[asyncio.Task] = set()

_stats: dict = {
    "scheduled": 0,
    "succeeded": 0,
    "failed": 0,
    "last_success_ts": None,
    "last_failure_ts": None,
    "last_failure_reason": None,
}


def get_usage_logger_health() -> dict:
    """A snapshot a health check (self_test, /health) can read to tell a
    silently-dead metering pipeline from a healthy one, instead of only
    finding out from a downstream row count days later."""
    return {**_stats, "pending": len(_pending_tasks)}


def _record_success() -> None:
    _stats["succeeded"] += 1
    _stats["last_success_ts"] = datetime.now(timezone.utc).isoformat()


def _record_failure(reason: str) -> None:
    logger.error("usage_log_failed reason=%s", reason)
    _stats["failed"] += 1
    _stats["last_failure_ts"] = datetime.now(timezone.utc).isoformat()
    _stats["last_failure_reason"] = reason


def _on_log_task_done(task: "asyncio.Task") -> None:
    """Done-callback for a scheduled log_usage_event task: releases the
    strong reference and makes an otherwise-invisible failure loud.

    log_usage_event() itself never raises (its own try/except guarantees
    that — see below), so task.exception() is expected to be None on every
    normal run. This callback exists for the cases that guarantee can't
    cover: the task being cancelled out from under us, or any other escape
    that would otherwise surface only as asyncio's own easy-to-miss
    "exception was never retrieved" warning.
    """
    _pending_tasks.discard(task)
    if task.cancelled():
        _record_failure("task_cancelled")
        return
    exc = task.exception()
    if exc is not None:
        logger.error("usage_log_task_failed err=%s", exc, exc_info=exc)
        _record_failure(f"unhandled_exception:{type(exc).__name__}")

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
    Fire-and-forget RPC insert into `usage_events`. Never raises.
    Designed to be called with asyncio.create_task() so it never delays the response.

    RLS-CREDENTIAL FIX (tm_requirements row 1181, 2026-09-28): this used to
    call storage/supabase_client.py's insert_row() directly against the
    `usage_events` table. That table has had RLS ENABLED with ZERO POLICIES
    since migrations/enable_rls.sql, and this container runs with ONLY
    SUPABASE_ANON_KEY (SUPABASE_SERVICE_KEY is deliberately never shipped
    here — see ops/vps/deploy_agentbroker_vps.py's NEVER_SHIP_TO_CONTAINER).
    `anon` holds a table-level INSERT grant but does not bypass RLS, so every
    insert was attempted and rejected — a grant without a policy is not
    permission. The fix is not a permissive anon policy on the table (the
    anon key is public; that would let anyone forge metering rows) — it is
    the narrow, parameter-scoped, SECURITY DEFINER RPC function
    `usage_events_insert` (sql/agentbroker/003_usage_billing_security_
    definer_rpc.sql), which `anon` may EXECUTE but which owns its own INSERT
    internally as a BYPASSRLS-owned function — the same architecture already
    proven in production for `operations` (001) and `anon_data_quota` (002).

    Every exit path updates `_stats` (see get_usage_logger_health()) so a
    caller that never inspects the return value still leaves a trail: a
    silent RPC failure (storage/supabase_client.py's rpc() raises, caught by
    the try/except below) used to be indistinguishable from success here. It
    no longer is.
    """
    try:
        args_hash = _hash8(str(sorted((arguments or {}).items()))) if arguments else None
        ip_hash = _hash8(ip) if ip else None
        ua = (user_agent or "")[:512]
        session_kind = classify_session_kind(method, tool_name, ua, key_id,
                                             principal_type=principal_type)
        clean_key_id = (key_id[:64] if key_id and key_id != "anonymous" else None)

        from storage.supabase_client import rpc
        result = await rpc("usage_events_insert", {
            "p_tool": tool_name or method,
            "p_args_hash": args_hash,
            "p_ip_hash": ip_hash,
            "p_user_agent": ua,
            "p_key_id": clean_key_id,
            "p_session_kind": session_kind,
            "p_method": method,
        })
        if result is None:
            # rpc() itself raises on any failure (see storage/
            # supabase_client.py) -- this branch guards a function that
            # somehow returned SQL NULL rather than a row, which
            # usage_events_insert's `returning * into v_row` makes
            # impossible in practice, but a None here must never be read as
            # success.
            _record_failure("rpc_returned_none")
        else:
            _record_success()
    except Exception as exc:  # noqa: BLE001
        logger.error("usage_log_failed method=%s tool=%s err=%s", method, tool_name, exc,
                     exc_info=exc)
        _record_failure(f"exception:{type(exc).__name__}")


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

    AUDIT-2026-09-28: this used to schedule the write with
    `asyncio.ensure_future(...)` and drop the returned Task immediately —
    the event loop's own bookkeeping only holds a WEAK reference to it (see
    module docstring), so nothing here kept it alive. The task is now held
    in `_pending_tasks` until its done-callback (`_on_log_task_done`) fires,
    and every scheduling attempt is counted in `_stats` whether or not it
    ultimately completes.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            task = loop.create_task(
                log_usage_event(method, tool_name, arguments, ip, user_agent,
                                key_id, principal_type=principal_type)
            )
            _stats["scheduled"] += 1
            _pending_tasks.add(task)
            task.add_done_callback(_on_log_task_done)
        else:
            _record_failure("no_running_event_loop")
    except Exception as exc:  # noqa: BLE001
        _record_failure(f"schedule_exception:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# OUTCOME LOGGING (2026-10-01, key-holder audit fix 1)
# ---------------------------------------------------------------------------
# fire_log_usage above is called only after a handler RETURNS, so every error path - a tool
# failure, a bad argument, an unknown tool, a crash, an HTTP 429 or 502 - wrote nothing, and the
# question "did the people holding keys get what they wanted?" had no answer for any period.
# These record every outcome, with the status code, latency, client name, the state of the key the
# caller presented, and the NAMES of the arguments (never their values), through the
# `usage_events_insert_v2` function that migrations/spine/009 creates.
#
# Same contract as fire_log_usage: fire-and-forget, never raises, never blocks a response, strong
# references held, every failure counted in _stats so get_usage_logger_health() sees it.

# "notification": a JSON-RPC message with no id (notifications/initialized...) or a client's response -
# accepted with 202, never answered, never an error. Needs migrations/spine/010, which teaches the
# database function to accept it; deploy 010 BEFORE the code that sends it.
OUTCOMES = ("ok", "tool_failure", "tool_error", "rpc_error", "exception", "http_error", "notification")

_V2_RPC = "usage_events_insert_v2"
_V1_RPC = "usage_events_insert"
# If the v2 function is missing (code deployed ahead of the migration, or a rollback of the
# database), fall back to the original 7-field insert for this long, then try v2 again.
_V2_RETRY_AFTER_S = 600.0
_v2_missing_until = 0.0


@dataclass
class UsageEvent:
    method: str
    tool_name: Optional[str] = None
    arguments: Optional[dict] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    key_id: Optional[str] = None
    principal_type: Optional[str] = None
    outcome: str = "ok"
    error_code: Optional[str] = None
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    key_state: Optional[str] = None
    arg_names: Optional[list] = None
    requested_name: Optional[str] = None
    detail: Optional[str] = None


def _safe_args_hash(arguments: Optional[dict]) -> Optional[str]:
    if not arguments:
        return None
    try:
        return _hash8(str(sorted((arguments or {}).items())))
    except Exception:  # noqa: BLE001 - mixed key types etc.: still hash SOMETHING stable
        return _hash8(repr(sorted(map(str, (arguments or {}).keys()))))


def _is_missing_function(exc: Exception) -> bool:
    text = str(exc)
    return "PGRST202" in text or "HTTP 404" in text


async def log_usage_outcome(event: UsageEvent) -> None:
    """Record one outcome. Never raises. See the block comment above."""
    global _v2_missing_until
    try:
        ip_hash = _hash8(event.ip) if event.ip else None
        ua = (event.user_agent or "")[:512]
        key_id = event.key_id
        session_kind = classify_session_kind(event.method, event.tool_name, ua, key_id,
                                             principal_type=event.principal_type)
        clean_key_id = (key_id[:64] if key_id and key_id != "anonymous" else None)
        base = {
            "p_tool": event.tool_name or event.method,
            "p_args_hash": _safe_args_hash(event.arguments),
            "p_ip_hash": ip_hash,
            "p_user_agent": ua,
            "p_key_id": clean_key_id,
            "p_session_kind": session_kind,
            "p_method": event.method,
        }
        from storage.supabase_client import rpc

        result = None
        if time.monotonic() >= _v2_missing_until:
            outcome = event.outcome if event.outcome in OUTCOMES else "ok"
            try:
                result = await rpc(_V2_RPC, {
                    **base,
                    "p_outcome": outcome,
                    "p_error_code": event.error_code,
                    "p_http_status": event.http_status,
                    "p_latency_ms": event.latency_ms,
                    "p_client_name": event.client_name,
                    "p_client_version": event.client_version,
                    "p_key_state": event.key_state,
                    "p_arg_names": event.arg_names,
                    "p_requested_name": event.requested_name,
                    "p_detail": event.detail,
                })
            except Exception as exc:  # noqa: BLE001
                if not _is_missing_function(exc):
                    raise
                _v2_missing_until = time.monotonic() + _V2_RETRY_AFTER_S
                logger.error(
                    "usage_log_v2_missing -- migrations/spine/009 is not applied; falling back "
                    "to the 7-field usage_events_insert for %ds (outcome columns are NOT being "
                    "recorded)", int(_V2_RETRY_AFTER_S))
        if result is None and time.monotonic() < _v2_missing_until:
            result = await rpc(_V1_RPC, base)
        if result is None:
            _record_failure("rpc_returned_none")
        else:
            _record_success()
    except Exception as exc:  # noqa: BLE001
        logger.error("usage_log_failed method=%s tool=%s err=%s", event.method, event.tool_name,
                     exc, exc_info=exc)
        _record_failure(f"exception:{type(exc).__name__}")


def fire_log_outcome(event: UsageEvent) -> None:
    """Schedule log_usage_outcome on the running loop. Safe from sync or async code; skipped
    (and counted) when there is no loop. Never raises."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            task = loop.create_task(log_usage_outcome(event))
            _stats["scheduled"] += 1
            _pending_tasks.add(task)
            task.add_done_callback(_on_log_task_done)
        else:
            _record_failure("no_running_event_loop")
    except Exception as exc:  # noqa: BLE001
        _record_failure(f"schedule_exception:{type(exc).__name__}")


class BurstThrottle:
    """At most one log per key per `window_s`, counting what was held back.

    A scanner at 23 requests a second would otherwise turn every HTTP 429 into a database write -
    the limiter protecting the service from the flood, and the logging amplifying it. `admit`
    returns (log_it, suppressed_since_last_log); the suppressed count goes into the row's `detail`
    so a flood is still visible as a flood.
    """

    def __init__(self, window_s: float = 30.0, max_keys: int = 2000) -> None:
        self.window_s = window_s
        self.max_keys = max_keys
        self._last: dict[str, float] = {}
        self._held: dict[str, int] = {}

    def admit(self, key: str, now: Optional[float] = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        last = self._last.get(key)
        if last is not None and now - last < self.window_s:
            self._held[key] = self._held.get(key, 0) + 1
            return False, 0
        if len(self._last) >= self.max_keys:
            cutoff = now - self.window_s
            for k in [k for k, t in self._last.items() if t < cutoff]:
                self._last.pop(k, None)
                self._held.pop(k, None)
            if len(self._last) >= self.max_keys:
                self._last.clear()
                self._held.clear()
        self._last[key] = now
        return True, self._held.pop(key, 0)


RATE_LIMIT_LOG_THROTTLE = BurstThrottle()
