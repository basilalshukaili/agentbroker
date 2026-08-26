"""
Demand queue — the dispatch half of demand shaping.

demand_shaping.py decides that a request must WAIT and renders a digest.
Nothing called the renderer, so the digest was dead code: over-budget requests
returned an honest "queued" receipt and were then FORGOTTEN. "Queued" that
nobody stores is just a nicer word for dropped (audit 2026-08-26).

This module closes that loop:

  enqueue()          persist an over-budget request so "queued" is TRUE
  dispatch_digest()  N pending requests -> ONE numbered WhatsApp message
  resolve_reply()    "1 YES 2 NO" -> mark those exact requests accepted/declined

THE 24h WINDOW IS THE HARD CONSTRAINT. WhatsApp only permits free-form
business-initiated messages inside 24h of the business's last inbound message.
Outside it, delivery requires a Meta-APPROVED TEMPLATE, which we do not have.
So a digest dispatches ONLY inside a live window (where it costs us nothing and
is permitted); otherwise the requests STAY QUEUED and we say so plainly. We do
not fabricate a dispatch, and we do not burn money on an SMS fallback the agent
did not ask for.

That constraint is also why the webhook is the natural trigger: a business's
inbound message is the exact moment a window OPENS.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("smb_broker.demand_queue")

_SB_TIMEOUT_S = 2.5
_TABLE = "pending_requests"
_DIGEST_TABLE = "demand_digests"

QUEUED = "queued"
DISPATCHED = "dispatched"
ACCEPTED = "accepted"
DECLINED = "declined"
EXPIRED = "expired"

# A queued request older than this is stale: the end-user has moved on, and
# surfacing it to the business would be worse than dropping it.
_QUEUE_TTL_HOURS = 48

# WhatsApp free-form service window.
_SERVICE_WINDOW_HOURS = 24

# Don't interrupt a business for a single request — that is the flood behaviour
# the digest exists to replace. Two is the smallest number that is a digest.
MIN_DIGEST_SIZE = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _sb(coro, default=None):
    """Bounded + swallowing. Shaping must never break a send (fail-open)."""
    try:
        return await asyncio.wait_for(coro, timeout=_SB_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("demand_queue_sb_failed err=%s", exc)
        return default


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

async def enqueue(
    *,
    business_id: str,
    business_number: str,
    agent_id: Optional[str],
    end_user_ref: Optional[str],
    intent: Optional[str],
    idempotency_key: Optional[str] = None,
) -> Optional[dict]:
    """Persist an over-budget request. Returns the row, or None if not stored.

    `idempotency_key` stops an agent's honest retry (which our own receipt
    invites via retry_after_ms) from stacking duplicate entries for the same
    end-user — the business would see the same request twice in one digest.
    """
    from core.conversations import norm_number, new_ref_token
    from storage.supabase_client import insert_row

    business_number = norm_number(business_number)
    key = idempotency_key or f"{business_id}:{agent_id}:{end_user_ref}:{intent}"

    existing = await _sb(_select(filters={
        "business_id": business_id, "idem_key": key, "state": QUEUED}), [])
    if existing:
        return existing[0]

    row = {
        "request_id": f"pq_{secrets.token_hex(8)}",
        "idem_key": key,
        "business_id": business_id,
        "business_number": business_number,
        "agent_id": agent_id,
        "end_user_ref": end_user_ref,
        "intent": intent,
        "ref_token": new_ref_token(),
        "state": QUEUED,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=_QUEUE_TTL_HOURS)).isoformat(),
    }
    await _sb(insert_row(_TABLE, row))
    return row


async def _select(filters: dict, limit: int = 200, order: str = "created_at.asc") -> list[dict]:
    from storage.supabase_client import select_rows
    return await select_rows(_TABLE, filters=filters, limit=limit, order=order) or []


async def pending_for(business_id: str) -> list[dict]:
    """Queued, unexpired requests for this business, OLDEST FIRST.

    Oldest-first is fairness, not style: the person who has waited longest is
    the one whose request should reach the business next.
    """
    rows = await _sb(_select({"business_id": business_id, "state": QUEUED}), []) or []
    now = _now()
    fresh = []
    for r in rows:
        exp = _parse_ts(r.get("expires_at"))
        if exp and exp <= now:
            continue
        fresh.append(r)
    return fresh


async def expire_stale(business_id: str) -> int:
    """Mark aged-out queued requests EXPIRED. Returns how many."""
    from storage.supabase_client import upsert_row
    rows = await _sb(_select({"business_id": business_id, "state": QUEUED}), []) or []
    now, n = _now(), 0
    for r in rows:
        exp = _parse_ts(r.get("expires_at"))
        if exp and exp <= now:
            await _sb(upsert_row(_TABLE, {
                "request_id": r["request_id"], "state": EXPIRED,
                "updated_at": now.isoformat(),
            }, on_conflict="request_id"))
            n += 1
    return n


# ---------------------------------------------------------------------------
# The 24h service window
# ---------------------------------------------------------------------------

async def service_window_open(our_number: str, business_number: str) -> bool:
    """True if this business messaged us within the last 24h.

    Checked against the conversation ledger's last_inbound_at, which the webhook
    stamps on every real inbound. If we cannot read it we return False — the
    fail-open rule protects DELIVERY of things the agent asked for, but a digest
    is OUR initiative, and guessing wrong here means an undeliverable send and a
    policy strike. Fail CLOSED on our own outreach.
    """
    from core.conversations import norm_number
    from storage.supabase_client import select_rows

    our_number = norm_number(our_number)
    business_number = norm_number(business_number)
    rows = await _sb(select_rows(
        "conversations",
        filters={"our_number": our_number, "business_number": business_number},
        limit=25, order="updated_at.desc"), []) or []
    cutoff = _now() - timedelta(hours=_SERVICE_WINDOW_HOURS)
    for r in rows:
        ts = _parse_ts(r.get("last_inbound_at"))
        if ts and ts > cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_digest(
    *,
    business_id: str,
    business_number: str,
    our_number: str,
    business_name: str = "there",
    force_window: Optional[bool] = None,
) -> dict:
    """N pending requests -> ONE numbered message. Honest about not sending.

    Returns {"dispatched": bool, "reason": str, ...}. Every non-dispatch path
    names WHY, because a silent no-op here is indistinguishable from a bug.
    """
    from core.demand_shaping import build_digest, digest_slice

    await expire_stale(business_id)
    pending = await pending_for(business_id)
    if len(pending) < MIN_DIGEST_SIZE:
        return {"dispatched": False, "reason": "below_digest_threshold",
                "pending": len(pending)}

    # Never message a number that told us to stop. Checked before the window so
    # an opt-out can never be overridden by a freshly opened window.
    try:
        from compliance.consent_store import get_consent_store
        if get_consent_store().is_opted_out(business_number, "whatsapp"):
            return {"dispatched": False, "reason": "business_opted_out",
                    "pending": len(pending)}
    except Exception as exc:  # noqa: BLE001
        # An unreadable opt-out store is NOT permission to send. Everywhere else
        # in demand shaping we fail open; here we fail closed, because the cost
        # of guessing wrong is messaging someone who said stop.
        logger.warning("optout_check_failed number=%s err=%s", business_number[-4:], exc)
        return {"dispatched": False, "reason": "optout_check_unavailable",
                "pending": len(pending)}

    window = force_window
    if window is None:
        window = await service_window_open(our_number, business_number)
    if not window:
        # Requests STAY queued. This is the honest state, not a failure: an
        # approved Meta template would unlock business-initiated delivery here.
        return {"dispatched": False, "reason": "outside_24h_service_window",
                "pending": len(pending),
                "how_to_resolve": ("WhatsApp permits free-form business-initiated "
                                   "messages only within 24h of the business's last "
                                   "message. Requests remain queued until the business "
                                   "contacts us or an approved template is available.")}

    shown = digest_slice(pending)
    body = build_digest(business_name, pending)
    if not body:
        return {"dispatched": False, "reason": "empty_digest", "pending": len(pending)}

    sent = await _send_whatsapp(business_number, body)
    if not sent.get("ok"):
        # Requests stay QUEUED — a failed send must never mark them dispatched,
        # or they vanish from every future digest without the business ever
        # having seen them.
        return {"dispatched": False, "reason": "send_failed",
                "detail": sent.get("error", "")[:200], "pending": len(pending)}

    digest_id = f"dg_{secrets.token_hex(8)}"
    # Persist the EXACT ordered slice that was rendered. The reply parser must
    # score "2 YES" against what the business actually SAW, not against whatever
    # the queue looks like when the reply lands (which may have grown).
    await _record_digest(digest_id, business_id, business_number, our_number, shown,
                         sent.get("wamid"))
    await _mark(shown, DISPATCHED, digest_id=digest_id)
    return {"dispatched": True, "digest_id": digest_id, "count": len(shown),
            "wamid": sent.get("wamid"), "remaining_queued": len(pending) - len(shown)}


async def dispatch_for_number(our_number: str, business_number: str,
                              business_name: str = "there") -> list[dict]:
    """Dispatch digests for whatever business owns this number.

    The webhook knows a phone number, not a business_id — and an inbound from
    that number is the exact moment its 24h window opens, which is the only
    moment we are permitted to send. force_window=True because the caller has
    just observed the inbound that opens it (reading it back from the ledger
    would race the write that records it).
    """
    from core.conversations import norm_number
    business_number = norm_number(business_number)
    rows = await _sb(_select({"business_number": business_number, "state": QUEUED}),
                     []) or []
    out = []
    for bid in dict.fromkeys(r.get("business_id") for r in rows if r.get("business_id")):
        res = await dispatch_digest(
            business_id=bid, business_number=business_number,
            our_number=our_number, business_name=business_name,
            force_window=True)
        res["business_id"] = bid
        out.append(res)
    return out


async def sweep_open_windows(max_businesses: int = 50) -> dict:
    """Flush queued digests for businesses whose 24h window is ALREADY open.

    dispatch_for_number only fires on a fresh inbound. But a window opened two
    hours ago is still open, and requests that queue up afterwards would sit
    untouched until it closed — we would miss the one period in which we are
    permitted to deliver them. This sweep is what makes the window check real
    (before it, service_window_open had no production caller at all).
    """
    rows = await _sb(_select({"state": QUEUED}, limit=1000), []) or []
    seen: dict[tuple[str, str], str] = {}
    for r in rows:
        bid, bnum = r.get("business_id"), r.get("business_number")
        if bid and bnum:
            seen.setdefault((bid, bnum), r.get("our_number") or "")

    dispatched = skipped = 0
    for (bid, bnum), _ in list(seen.items())[:max_businesses]:
        our = await _our_number_for(bnum)
        if not our:
            skipped += 1
            continue
        # force_window omitted on purpose: here we genuinely must ASK.
        res = await dispatch_digest(business_id=bid, business_number=bnum,
                                    our_number=our,
                                    business_name=await _name_for(bid) or "there")
        if res.get("dispatched"):
            dispatched += 1
        else:
            skipped += 1
    return {"businesses": len(seen), "dispatched": dispatched, "skipped": skipped}


async def _our_number_for(business_number: str) -> Optional[str]:
    """Which of our sender numbers last spoke to this business."""
    from core.conversations import norm_number
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(
        "conversations", filters={"business_number": norm_number(business_number)},
        limit=1, order="updated_at.desc"), []) or []
    return rows[0].get("our_number") if rows else None


async def _name_for(business_id: str) -> Optional[str]:
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows("businesses", filters={"business_id": business_id},
                                 limit=1), []) or []
    return (rows[0].get("name") if rows else None)


async def _send_whatsapp(to_number: str, body: str) -> dict:
    """Send the digest through the real adapter — compliance gate included.

    Goes through ChannelRequest/ChannelResponse rather than posting to Graph
    directly, so the digest cannot bypass pre_check. A path that skips the gate
    is exactly how a "safe" system starts sending unlawful messages.
    """
    from channels.adapter_interface import ChannelRequest
    from channels.whatsapp.cloud_api import WhatsAppCloudAdapter
    try:
        adapter = WhatsAppCloudAdapter()
        if not adapter.is_available:
            return {"ok": False, "error": "whatsapp_not_configured"}
        req = ChannelRequest(
            recipient_id=to_number,
            channel="whatsapp",
            # A digest is a service message to a business about requests it
            # already has — transactional, never marketing.
            message_type="transactional",
            content=body,
            agent_id="hatchloop:demand_digest",
        )
        resp = await asyncio.wait_for(adapter.send(req), timeout=15.0)
        if getattr(resp, "success", False):
            return {"ok": True, "wamid": resp.provider_message_id}
        return {"ok": False,
                "error": f"{resp.error_code}: {resp.error_message or ''}".strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _record_digest(digest_id: str, business_id: str, business_number: str,
                         our_number: str, shown: list[dict], wamid: Optional[str]) -> None:
    from storage.supabase_client import insert_row
    await _sb(insert_row(_DIGEST_TABLE, {
        "digest_id": digest_id,
        "business_id": business_id,
        "business_number": business_number,
        "our_number": our_number,
        "wamid": wamid,
        # Ordered: index i in this list is what the business saw as item i+1.
        "request_ids": [r["request_id"] for r in shown],
        "state": "awaiting_reply",
        "created_at": _now().isoformat(),
    }))


async def _mark(rows: list[dict], state: str, digest_id: Optional[str] = None) -> None:
    from storage.supabase_client import upsert_row
    for r in rows:
        patch = {"request_id": r["request_id"], "state": state,
                 "updated_at": _now().isoformat()}
        if digest_id:
            patch["digest_id"] = digest_id
        await _sb(upsert_row(_TABLE, patch, on_conflict="request_id"))


# ---------------------------------------------------------------------------
# Reply resolution
# ---------------------------------------------------------------------------

async def latest_digest(our_number: str, business_number: str) -> Optional[dict]:
    from core.conversations import norm_number
    from storage.supabase_client import select_rows
    rows = await _sb(select_rows(
        _DIGEST_TABLE,
        filters={"our_number": norm_number(our_number),
                 "business_number": norm_number(business_number),
                 "state": "awaiting_reply"},
        limit=1, order="created_at.desc"), []) or []
    return rows[0] if rows else None


async def resolve_reply(our_number: str, business_number: str, text: str) -> dict:
    """Score a business's free-typed reply against the digest it actually saw.

    Returns {"matched": n, "results": [{request_id, accepted}], ...}.
    A reply that matches nothing returns matched=0 — the caller then treats it
    as an ordinary conversational message, never as a silent accept.
    """
    from core.demand_shaping import parse_digest_reply
    from storage.supabase_client import upsert_row

    digest = await latest_digest(our_number, business_number)
    if not digest:
        return {"matched": 0, "reason": "no_open_digest"}

    ids = list(digest.get("request_ids") or [])
    if not ids:
        return {"matched": 0, "reason": "digest_has_no_items"}

    rows = await _sb(_select({"digest_id": digest["digest_id"]}), []) or []
    by_id = {r.get("request_id"): r for r in rows}
    # Rebuild the ordering the business saw. A missing row becomes a hole rather
    # than a shift — renumbering would silently reassign "2 YES" to someone else.
    ordered = [by_id.get(rid) or {"request_id": rid, "_missing": True} for rid in ids]

    pairs = parse_digest_reply(text, ordered)
    results = []
    for req, accepted in pairs:
        if req.get("_missing"):
            continue
        state = ACCEPTED if accepted else DECLINED
        await _sb(upsert_row(_TABLE, {
            "request_id": req["request_id"], "state": state,
            "updated_at": _now().isoformat(),
        }, on_conflict="request_id"))
        results.append({"request_id": req["request_id"], "accepted": accepted,
                        "end_user_ref": req.get("end_user_ref"),
                        "agent_id": req.get("agent_id")})

    if results and len(results) >= len(ids):
        await _sb(upsert_row(_DIGEST_TABLE, {
            "digest_id": digest["digest_id"], "state": "resolved",
            "updated_at": _now().isoformat(),
        }, on_conflict="digest_id"))

    return {"matched": len(results), "digest_id": digest["digest_id"],
            "results": results}
