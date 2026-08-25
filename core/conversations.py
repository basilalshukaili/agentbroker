"""
Conversation threading — the multi-tenant correlation layer.

THE PROBLEM (founder, 2026-08-26): many end-users ask many businesses for
things through OUR shared number. A business replies "yes ok" with no context.
Which user? Which request? Guessing wrong routes someone's confirmation to a
stranger - the single worst trust failure this product can have.

THE 4-LAYER SOLUTION (in correlation priority order):
  1. WAMID   - Meta echoes `context.id` when a business taps reply-to. Exact.
  2. REF     - every outbound carries a short reference ("#4821"); businesses
               routinely echo it when typing freely. Exact.
  3. PAIR    - exactly ONE open conversation per (our_number, business_number)
               is allowed, so the pair itself identifies the thread. Inferred.
  4. AMBIGUOUS - if 2+ open threads could claim it, we NEVER guess: the caller
               asks the business one clarifying question, then escalates.

`correlate_inbound` returns a MatchResult carrying the method + confidence so
the caller can behave differently for exact vs inferred vs ambiguous.

All storage is Supabase, bounded and fail-open: correlation must never hang
inbound processing (same rule as the quota gate).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("smb_broker.conversations")

_SB_TIMEOUT_S = 2.5
_TABLE = "conversations"
_MSG_TABLE = "conversation_messages"

# Conversation lifetime: a thread nobody touches for this long is stale and no
# longer claims inbound replies (prevents a months-old thread hijacking a new one).
_DEFAULT_TTL_HOURS = 72

# State machine
OPEN = "open"
AWAITING_REPLY = "awaiting_reply"
CONFIRMED = "confirmed"
CLOSED = "closed"
_LIVE_STATES = (OPEN, AWAITING_REPLY)

# Reference token: 4 digits is enough to disambiguate concurrent threads with a
# single business while staying trivially typeable by a human on a phone.
_REF_RE = re.compile(r"#?\b(\d{4})\b")


@dataclass
class MatchResult:
    conversation: Optional[dict] = None
    method: str = "none"          # wamid | ref | pair | ambiguous | none
    confidence: str = "none"      # exact | inferred | none
    candidates: list[dict] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.conversation is not None

    @property
    def ambiguous(self) -> bool:
        return self.method == "ambiguous"


def new_ref_token() -> str:
    """A 4-digit reference an agent can quote and a human can type."""
    return f"{secrets.randbelow(9000) + 1000}"


def parse_ref(text: str) -> Optional[str]:
    """Extract a reference token from free-typed business text, if present."""
    if not text:
        return None
    m = _REF_RE.search(text)
    return m.group(1) if m else None


def reference_line(ref_token: str, end_user_label: str, on_behalf_of: str = "HatchLoop") -> str:
    """The identity+reference footer every outbound message carries.

    This is layer 2 AND the answer to "how does the business know who it is
    talking to" - the number is shared, but the identity travels in-message.
    """
    return (
        f"\n\n-- Request #{ref_token} for {end_user_label} "
        f"(via {on_behalf_of}). Reply with #{ref_token} to keep replies matched."
    )


async def _sb(coro, default=None):
    """Bounded, fail-open Supabase call — correlation never blocks dispatch."""
    try:
        return await asyncio.wait_for(coro, timeout=_SB_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 (includes TimeoutError)
        logger.warning("conversations_sb_failed: %s", exc)
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_live(row: dict) -> bool:
    if row.get("state") not in _LIVE_STATES:
        return False
    exp = row.get("expires_at")
    if not exp:
        return True
    try:
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00")) > _now()
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------

async def open_conversation(
    *,
    agent_id: Optional[str],
    end_user_ref: str,
    business_id: Optional[str],
    business_number: str,
    our_number: str,
    channel: str = "whatsapp",
    intent: Optional[str] = None,
    ttl_hours: int = _DEFAULT_TTL_HOURS,
) -> dict:
    """Open a thread. Returns the conversation row (always — fail-open).

    LAYER 3 GUARD: if a live thread already exists on this (our_number,
    business_number) pair, the caller MUST be told, because opening a second one
    makes replies ambiguous. We surface that as `pair_conflict` on the returned
    row so send paths can switch to another sender number from the pool.
    """
    from storage.supabase_client import insert_row

    existing = await find_live_by_pair(our_number, business_number)
    conv = {
        "conversation_id": f"conv_{secrets.token_hex(8)}",
        "ref_token": new_ref_token(),
        "agent_id": agent_id,
        "end_user_ref": end_user_ref,
        "business_id": business_id,
        "business_number": business_number,
        "our_number": our_number,
        "channel": channel,
        "state": OPEN,
        "intent": intent,
        "expires_at": (_now() + timedelta(hours=ttl_hours)).isoformat(),
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }
    await _sb(insert_row(_TABLE, conv))
    if existing:
        conv["pair_conflict"] = existing.get("conversation_id")
    return conv


async def record_outbound(conversation_id: str, wamid: Optional[str], body: str) -> None:
    """Bind the sent message's wamid to the thread (layer 1 substrate)."""
    from storage.supabase_client import insert_row, upsert_row

    await _sb(insert_row(_MSG_TABLE, {
        "conversation_id": conversation_id, "direction": "out",
        "wamid": wamid, "body": (body or "")[:4000],
    }))
    await _sb(upsert_row(_TABLE, {
        "conversation_id": conversation_id,
        "last_outbound_wamid": wamid,
        "state": AWAITING_REPLY,
        "updated_at": _now().isoformat(),
    }, on_conflict="conversation_id"))


async def record_inbound(conversation_id: str, wamid: Optional[str], body: str) -> None:
    from storage.supabase_client import insert_row, upsert_row

    await _sb(insert_row(_MSG_TABLE, {
        "conversation_id": conversation_id, "direction": "in",
        "wamid": wamid, "body": (body or "")[:4000],
    }))
    await _sb(upsert_row(_TABLE, {
        "conversation_id": conversation_id,
        "last_inbound_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
    }, on_conflict="conversation_id"))


async def set_state(conversation_id: str, state: str) -> None:
    from storage.supabase_client import upsert_row
    await _sb(upsert_row(_TABLE, {
        "conversation_id": conversation_id, "state": state,
        "updated_at": _now().isoformat(),
    }, on_conflict="conversation_id"))


# ---------------------------------------------------------------------------
# Lookup paths (the correlation layers)
# ---------------------------------------------------------------------------

async def find_by_wamid(wamid: str) -> Optional[dict]:
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(_TABLE, filters={"last_outbound_wamid": wamid}, limit=1), [])
    return (rows or [None])[0]


async def find_by_ref(ref_token: str, business_number: Optional[str] = None) -> Optional[dict]:
    from storage.supabase_client import select_rows
    filters: dict[str, Any] = {"ref_token": ref_token}
    if business_number:
        filters["business_number"] = business_number
    rows = await _sb(select_rows(_TABLE, filters=filters, limit=2), [])
    live = [r for r in (rows or []) if _is_live(r)]
    return live[0] if len(live) == 1 else None


async def find_live_by_pair(our_number: str, business_number: str) -> Optional[dict]:
    rows = await live_threads_for_pair(our_number, business_number)
    return rows[0] if len(rows) == 1 else None


async def live_threads_for_pair(our_number: str, business_number: str) -> list[dict]:
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(_TABLE, filters={
        "our_number": our_number, "business_number": business_number,
    }, limit=25), [])
    return [r for r in (rows or []) if _is_live(r)]


async def correlate_inbound(
    *,
    business_number: str,
    our_number: str,
    body: str = "",
    context_wamid: Optional[str] = None,
) -> MatchResult:
    """Run the 4-layer cascade. NEVER guesses between 2+ live candidates."""
    # Layer 1 — exact: the business tapped reply on a specific message.
    if context_wamid:
        conv = await find_by_wamid(context_wamid)
        if conv:
            return MatchResult(conversation=conv, method="wamid", confidence="exact")

    # Layer 2 — exact: the reference token appears in the text.
    ref = parse_ref(body)
    if ref:
        conv = await find_by_ref(ref, business_number)
        if conv:
            return MatchResult(conversation=conv, method="ref", confidence="exact")

    # Layers 3/4 — the number pair, or honest ambiguity.
    live = await live_threads_for_pair(our_number, business_number)
    if len(live) == 1:
        return MatchResult(conversation=live[0], method="pair", confidence="inferred")
    if len(live) > 1:
        return MatchResult(method="ambiguous", confidence="none", candidates=live)
    return MatchResult()


def clarifying_question(candidates: list[dict]) -> str:
    """Layer 4: ask, never guess."""
    refs = ", ".join(f"#{c.get('ref_token')}" for c in candidates[:5])
    return (
        "Sorry - we have more than one open request with you right now "
        f"({refs}). Which one is this about? Reply with the number, e.g. "
        f"\"#{candidates[0].get('ref_token')}\"."
    )
