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

# Reference token: 4 digits, typeable by a human on a phone.
#
# The '#' sigil is MANDATORY (adversarial review 2026-08-26, critical): with an
# optional sigil, ANY bare 4-digit run parsed as a reference - "come at 1430",
# "1000 OMR", "2026-08-27" - and layer 2 returned confidence="exact", silently
# overriding the never-guess ambiguity guard and misrouting one user's
# confirmation onto another user's thread. reference_line() explicitly instructs
# businesses to include the '#', so requiring it loses nothing: a bare number now
# correctly falls through to layers 3/4 (pair match, or ask).
_REF_RE = re.compile(r"#\s*(\d{4})\b")


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


def parse_refs(text: str) -> list[str]:
    """ALL sigiled reference tokens in the text (order preserved, deduped).

    Returns every match, not just the first: a business writing "we can do 2500
    for #4821" must not have a price shadow the real reference.
    """
    if not text:
        return []
    seen, out = set(), []
    for m in _REF_RE.finditer(text):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def parse_ref(text: str) -> Optional[str]:
    """First sigiled reference token, or None. (parse_refs is preferred.)"""
    refs = parse_refs(text)
    return refs[0] if refs else None


def reference_line(ref_token: str, end_user_label: str, on_behalf_of: str = "HatchLoop",
                   contested: bool = False) -> str:
    """The identity+reference footer every outbound message carries.

    This is layer 2 AND the answer to "how does the business know who it is
    talking to" - the number is shared, but the identity travels in-message.

    `contested` = another live thread already exists with this business on this
    number, so a bare "yes" CANNOT be attributed (it goes to layer 4 and we have
    to interrupt them with a clarifying question). We therefore ask for the
    reference more firmly. Acting on pair_conflict this way was missed until two
    independent model reviews both flagged it (2026-08-26).
    """
    if contested:
        return (
            f"\n\n-- Request #{ref_token} for {end_user_label} (via {on_behalf_of}). "
            f"You have more than one open request with us, so please START YOUR REPLY "
            f"WITH #{ref_token} - otherwise we cannot tell which request you mean."
        )
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


def norm_number(n: Optional[str]) -> str:
    """Digits only.

    The send path supplies E.164 ("+96890000001") while Meta's webhook supplies
    bare digits ("96890000001"); storing one and looking up the other silently
    matched NOTHING, so every reply fell through to 'unknown'. Normalise at
    every boundary (found by an end-to-end test, 2026-08-26).
    """
    return "".join(c for c in (n or "") if c.isdigit())


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

    business_number = norm_number(business_number)
    our_number = norm_number(our_number)
    existing = await find_live_by_pair(our_number, business_number)
    conv = {
        "conversation_id": f"conv_{secrets.token_hex(8)}",
        "ref_token": new_ref_token(),
        "agent_id": agent_id,
        "end_user_ref": end_user_ref,
        "business_id": business_id,
        "business_number": norm_number(business_number),
        "our_number": norm_number(our_number),
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

async def get_conversation(conversation_id: str) -> Optional[dict]:
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(_TABLE, filters={"conversation_id": conversation_id},
                                 limit=1), [])
    return (rows or [None])[0]


async def messages_for(conversation_id: str, limit: int = 200) -> list[dict]:
    """Every message in a thread, oldest first (the agent-readable transcript)."""
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(_MSG_TABLE, filters={"conversation_id": conversation_id},
                                 limit=limit, order="created_at.asc"), [])
    return rows or []


async def find_by_wamid(wamid: str) -> Optional[dict]:
    """Resolve a replied-to message id through the MESSAGE LEDGER.

    The conversation row only keeps the LAST outbound wamid, so resolving against
    it lost every earlier message: a business replying to an older message in the
    thread fell through to weaker layers (review 2026-08-26). The ledger has every
    outbound wamid, so we resolve there - and still require the thread to be live,
    or a closed thread would out-rank the live one (layer 1 runs first).
    """
    if not wamid:
        return None
    from storage.supabase_client import select_rows
    msgs = await _sb(select_rows(_MSG_TABLE, filters={"wamid": wamid, "direction": "out"},
                                 limit=2), [])
    ids = {m.get("conversation_id") for m in (msgs or []) if m.get("conversation_id")}
    if len(ids) != 1:
        return None                      # unknown, or ambiguous -> weaker layers
    conv = await get_conversation(ids.pop())
    return conv if conv and _is_live(conv) else None


async def find_by_ref(ref_token: str, business_number: Optional[str]) -> Optional[dict]:
    """Exact-match a reference token WITHIN one business.

    business_number is MANDATORY: ref tokens are only 4 digits and are reused
    across businesses, so an unscoped lookup could hand a reply to a completely
    different business's thread (review 2026-08-26, critical).
    """
    if not ref_token or not business_number:
        return None
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(_TABLE, filters={
        "ref_token": ref_token, "business_number": norm_number(business_number),
    }, limit=25), [])
    rows = rows or []
    if len(rows) >= 25:
        return None                      # truncated view -> untrustworthy
    live = [r for r in rows if _is_live(r)]
    return live[0] if len(live) == 1 else None


async def find_live_by_pair(our_number: str, business_number: str) -> Optional[dict]:
    rows = await live_threads_for_pair(our_number, business_number)
    return rows[0] if len(rows) == 1 else None


# Sentinel row: signals "the view was truncated, so we cannot prove uniqueness".
# Carried as an extra candidate so correlate_inbound takes the ambiguous branch
# instead of trusting a partial result.
_TRUNCATED = {"conversation_id": "__truncated__", "ref_token": "?"}


async def live_threads_for_pair(our_number: str, business_number: str,
                                limit: int = 200) -> list[dict]:
    """Live threads on a number pair.

    Filters liveness IN THE QUERY (per state) rather than truncating first and
    filtering after: dead rows accumulate forever on a repeat business, and an
    unordered LIMIT window silently hid the second live thread - which defeated
    the ambiguity guard entirely (review 2026-08-26, critical). If a window comes
    back full we cannot prove uniqueness, so we append a sentinel that forces the
    ambiguous (ask, never guess) branch.
    """
    from storage.supabase_client import select_rows
    our_number, business_number = norm_number(our_number), norm_number(business_number)
    out: list[dict] = []
    truncated = False
    for state in _LIVE_STATES:
        rows = await _sb(select_rows(_TABLE, filters={
            "our_number": our_number, "business_number": business_number,
            "state": state,
        }, limit=limit), [])
        rows = rows or []
        if len(rows) >= limit:
            truncated = True
        out.extend(r for r in rows if _is_live(r))
    if truncated and out:
        out.append(dict(_TRUNCATED))
    return out


async def correlate_inbound(
    *,
    business_number: str,
    our_number: str,
    body: str = "",
    context_wamid: Optional[str] = None,
) -> MatchResult:
    """Run the 4-layer cascade. NEVER guesses between 2+ live candidates."""
    business_number = norm_number(business_number)
    our_number = norm_number(our_number)
    # Without a business identity nothing can be scoped safely -> no match.
    if not business_number:
        return MatchResult()

    # Layer 1 — exact: the business tapped reply on a specific message.
    if context_wamid:
        conv = await find_by_wamid(context_wamid)
        if conv:
            return MatchResult(conversation=conv, method="wamid", confidence="exact")

    # Layer 2 — exact: a sigiled reference appears in the text. Every quoted
    # reference is resolved; if the message quotes two different live threads we
    # must NOT pick one, so that also goes to the ambiguous branch.
    resolved: list[dict] = []
    for ref in parse_refs(body):
        conv = await find_by_ref(ref, business_number)
        if conv and all(c["conversation_id"] != conv["conversation_id"] for c in resolved):
            resolved.append(conv)
    if len(resolved) == 1:
        return MatchResult(conversation=resolved[0], method="ref", confidence="exact")
    if len(resolved) > 1:
        return MatchResult(method="ambiguous", confidence="none", candidates=resolved)

    # Layers 3/4 — the number pair, or honest ambiguity.
    live = await live_threads_for_pair(our_number, business_number)
    if len(live) == 1:
        return MatchResult(conversation=live[0], method="pair", confidence="inferred")
    if len(live) > 1:
        return MatchResult(method="ambiguous", confidence="none", candidates=live)
    return MatchResult()


def clarifying_question(candidates: list[dict]) -> str:
    """Layer 4: ask, never guess."""
    real = [c for c in candidates if c.get("conversation_id") != "__truncated__"]
    if not real:
        return ("Sorry - we could not match your reply to a specific request. "
                "Please reply quoting the request number, e.g. \"#1234\".")
    refs = ", ".join(f"#{c.get('ref_token')}" for c in real[:5])
    return (
        "Sorry - we have more than one open request with you right now "
        f"({refs}). Which one is this about? Reply with the number, e.g. "
        f"\"#{real[0].get('ref_token')}\"."
    )
