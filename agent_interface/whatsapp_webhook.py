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

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

logger = logging.getLogger("smb_broker.whatsapp_webhook")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

_STOP_WORDS = {"stop", "unsubscribe", "cancel", "quit", "end", "revoke"}


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


@router.post("/whatsapp")
async def receive(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})

    stored = 0
    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                contacts = {c.get("wa_id"): c.get("profile", {}).get("name")
                            for c in value.get("contacts", []) or []}
                for msg in value.get("messages", []) or []:
                    sender = msg.get("from", "")
                    mtype = msg.get("type", "")
                    text = ""
                    if mtype == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    elif mtype == "button":
                        text = (msg.get("button") or {}).get("text", "")
                    row = {
                        "wa_message_id": msg.get("id"),
                        "sender": sender,
                        "sender_name": contacts.get(sender),
                        "msg_type": mtype,
                        "body": text[:4000],
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    }
                    # 1. durable store (best-effort)
                    try:
                        from storage.supabase_client import insert_row
                        await insert_row("whatsapp_inbound", row)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("wa_inbound_store_failed: %s", exc)
                    # 2. STOP handling — same enforcement path as SMS STOP
                    if text.strip().lower() in _STOP_WORDS:
                        try:
                            from compliance.consent_store import get_consent_store
                            get_consent_store().mark_opted_out(sender, "whatsapp")
                            get_consent_store().revoke_consent(
                                sender, "whatsapp", "marketing", "keyword_STOP")
                            from storage.supabase_client import insert_row as _ir
                            await _ir("consent_optouts", {
                                "recipient_id": sender, "channel": "whatsapp",
                                "use_case": "marketing",
                                "revocation_method": "keyword_STOP",
                                "source": "whatsapp_webhook",
                            })
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("wa_optout_failed: %s", exc)
                    stored += 1
                    logger.info("wa_inbound from=%s type=%s len=%d",
                                sender, mtype, len(text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("wa_webhook_parse_failed: %s", exc)

    return JSONResponse({"ok": True, "received": stored})
