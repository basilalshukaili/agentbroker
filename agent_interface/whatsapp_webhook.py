"""
WhatsApp Cloud API webhook — the ONLY way inbound WhatsApp messages reach us.

GET  /webhooks/whatsapp : Meta's verification handshake (hub.challenge echo).
POST /webhooks/whatsapp : message/status events. We:
  1. store every inbound message durably (Supabase whatsapp_inbound, best-effort),
  2. honor STOP/opt-out immediately in the compliance consent store
     (durable consent_optouts row + in-memory enforcement set),
  3. always return 200 fast (Meta retries on errors; processing is best-effort).

Verify token: WHATSAPP_VERIFY_TOKEN env (shared secret typed into the Meta app
dashboard when configuring the webhook URL).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger("smb_broker.whatsapp_webhook")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

_STOP_WORDS = {"stop", "unsubscribe", "cancel", "quit", "end", "revoke"}

# Every await on the intake path is bounded — Meta retries a slow webhook.
_IO_TIMEOUT_S = 2.5

# Unambiguous resolutions only. A thread that stays open forever makes future
# replies ambiguous, but guessing "resolved" from a vague reply would close a
# live request - so anything uncertain stays open.
_CONFIRM_WORDS = {"yes", "yep", "yes please", "confirmed", "confirm", "ok",
                  "okay", "sure", "booked", "done", "accepted", "agreed"}
_DECLINE_WORDS = {"no", "nope", "cannot", "can't", "cant", "unavailable",
                  "fully booked", "declined", "sorry no", "not available"}


def _resolution_state(text: str) -> Optional[str]:
    """CONFIRMED / CLOSED for a clear yes-or-no, else None (stay open)."""
    from core import conversations as _c
    t = (text or "").strip().lower().strip(".!,")
    # Strip a quoted reference so "#4821 yes" still resolves.
    for ref in _c.parse_refs(t):
        t = t.replace(f"#{ref}", "").replace(ref, "")
    t = t.strip().strip(".!,")
    if not t or len(t) > 40:      # long free text = judgement needed, stay open
        return None
    if t in _CONFIRM_WORDS:
        return _c.CONFIRMED
    if t in _DECLINE_WORDS:
        return _c.CLOSED
    return None


async def _ask(to: str, question: str) -> None:
    """Send a clarifying question back on the same channel (service window =
    free-form text is allowed and free). Best-effort; never raises.

    Checks opt-out FIRST: an outbound from the webhook path must respect the
    same consent gate as every other send (review 2026-08-26)."""
    try:
        from compliance.consent_store import get_consent_store
        if get_consent_store().is_opted_out(to, "whatsapp"):
            logger.info("wa_clarify_suppressed_optout to=%s", to)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        import httpx
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        phone_id = os.getenv("WHATSAPP_PHONE_ID", "")
        if not (token and phone_id):
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": to,
                      "type": "text", "text": {"body": question[:4000]}},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("wa_clarify_send_failed: %s", exc)


@router.get("/whatsapp")
async def verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if hub_mode == "subscribe" and expected and hub_token == expected:
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("verification failed", status_code=403)


def _verify_signature(raw: bytes, header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 over the RAW body.

    Without this the endpoint accepts anything: a forged POST could inject a
    fake "business reply" into a real end-user's conversation, or a fake STOP
    (adversarial review 2026-08-26, critical). The GET verify_token only guards
    the one-time handshake, not ongoing deliveries.
    """
    secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if not secret:
        return True                      # unconfigured: fail open (dev/test)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


async def _bounded(coro, what: str, timeout: float = _IO_TIMEOUT_S):
    """Every await on the intake path is bounded: the webhook must 200 fast or
    Meta retries and (worse) our reply latency compounds."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 (includes TimeoutError)
        logger.warning("wa_%s_failed: %s", what, exc)
        return None


async def _already_handled(wa_message_id: str) -> bool:
    """Meta delivers at-least-once; without a dedupe a retry double-records the
    reply and re-sends the clarifying question."""
    if not wa_message_id:
        return False
    from storage.supabase_client import select_rows
    rows = await _bounded(
        select_rows("whatsapp_inbound", filters={"wa_message_id": wa_message_id}, limit=1),
        "dedupe_check")
    return bool(rows)


async def _handle_message(msg: dict, contacts: dict, our_number: str) -> bool:
    """Process ONE inbound message. Isolated so a malformed sibling cannot abort
    the batch. Returns True if it was recorded."""
    sender = msg.get("from") or ""
    mtype = msg.get("type") or ""
    wa_id = msg.get("id") or ""
    text = ""
    if mtype == "text":
        text = ((msg.get("text") or {}).get("body") or "")
    elif mtype == "button":
        text = ((msg.get("button") or {}).get("text") or "")
    context_wamid = (msg.get("context") or {}).get("id")

    if await _already_handled(wa_id):
        logger.info("wa_duplicate_skipped id=%s", wa_id)
        return False

    from storage.supabase_client import insert_row
    await _bounded(insert_row("whatsapp_inbound", {
        "wa_message_id": wa_id,
        "sender": sender,
        "sender_name": contacts.get(sender),
        "msg_type": mtype,
        "body": text[:4000],
        "received_at": datetime.now(timezone.utc).isoformat(),
    }), "inbound_store")

    # STOP: honor it and STOP PROCESSING. A stop is not a reply to a booking,
    # and continuing would let us message a number in the same request in which
    # it opted out (review 2026-08-26).
    if text.strip().lower() in _STOP_WORDS:
        try:
            from compliance.consent_store import get_consent_store
            get_consent_store().mark_opted_out(sender, "whatsapp")
            get_consent_store().revoke_consent(
                sender, "whatsapp", "marketing", "keyword_STOP")
        except Exception as exc:  # noqa: BLE001
            logger.warning("wa_optout_memory_failed: %s", exc)
        await _bounded(insert_row("consent_optouts", {
            "recipient_id": sender, "channel": "whatsapp", "use_case": "marketing",
            "revocation_method": "keyword_STOP", "source": "whatsapp_webhook",
        }), "optout_store")
        logger.info("wa_optout from=%s", sender)
        return True

    # Correlate to the RIGHT conversation. Never guess.
    try:
        from core import conversations as _conv
        match = await asyncio.wait_for(
            _conv.correlate_inbound(business_number=sender, our_number=our_number,
                                    body=text, context_wamid=context_wamid),
            timeout=_IO_TIMEOUT_S * 2)
        if match.matched:
            cid = match.conversation["conversation_id"]
            await _bounded(_conv.record_inbound(cid, wa_id, text), "record_inbound")
            # Close the loop: a clear yes/no RESOLVES the thread. Without this
            # every thread stayed live until its TTL, so a business with repeat
            # custom accumulated live threads and more replies fell into the
            # ambiguous branch than necessary (wiring audit, 2026-08-26).
            # Conservative: only unambiguous signals transition; anything else
            # stays open for a human/agent to judge.
            new_state = _resolution_state(text)
            if new_state:
                await _bounded(_conv.set_state(cid, new_state), "set_state")
                logger.info("wa_thread_resolved conv=%s state=%s", cid, new_state)
            logger.info("wa_correlated conv=%s method=%s confidence=%s",
                        cid, match.method, match.confidence)
        elif match.ambiguous:
            logger.info("wa_ambiguous candidates=%d from=%s",
                        len(match.candidates), sender)
            await _ask(sender, _conv.clarifying_question(match.candidates))
    except Exception as exc:  # noqa: BLE001 — never break intake
        logger.warning("wa_correlate_failed: %s", exc)

    logger.info("wa_inbound from=%s type=%s len=%d", sender, mtype, len(text))
    return True


@router.post("/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    if not _verify_signature(raw, request.headers.get("x-hub-signature-256", "")):
        logger.warning("wa_bad_signature len=%d", len(raw))
        return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=403)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})

    stored = 0
    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                            for c in (value.get("contacts") or [])}
                our_number = ((value.get("metadata") or {})
                              .get("display_phone_number") or "").replace("+", "")
                for msg in (value.get("messages") or []):
                    try:
                        if not isinstance(msg, dict):
                            logger.warning("wa_message_not_an_object: %.60r", msg)
                            continue
                        if await _handle_message(msg, contacts, our_number):
                            stored += 1
                    except Exception as exc:  # noqa: BLE001 — isolate siblings
                        # The handler itself must never raise: extract the id
                        # defensively or one bad sibling aborts the batch.
                        _mid = msg.get("id") if isinstance(msg, dict) else "?"
                        logger.warning("wa_message_failed id=%s err=%s", _mid, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("wa_webhook_parse_failed: %s", exc)

    return JSONResponse({"ok": True, "received": stored})
