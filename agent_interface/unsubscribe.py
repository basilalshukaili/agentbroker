"""
One-click unsubscribe.

WHY THIS EXISTS. Both email adapters append a CAN-SPAM unsubscribe link to
marketing/follow-up/notification sends, and the URLs they used were
`https://your-domain.example/unsubscribe` and
`https://smb-broker.example/unsubscribe` - neither domain exists, and no
unsubscribe route existed on ours either (found 2026-08-26). Every commercial
email we sent carried a dead opt-out link.

That is a legal defect on its own, and a positioning one: the compliance gate
is the thing we sell. A product whose pitch is "your AI can message businesses
lawfully" cannot ship a broken unsubscribe.

DESIGN NOTES
  - The link must work with ONE CLICK and NO LOGIN, so the recipient is
    identified by a signed token, not by a session. The token carries
    (recipient_id, channel) and an expiry, HMAC-signed - it cannot be forged
    and it cannot be edited to unsubscribe someone else.
  - RFC 8058 one-click POST is supported as well as GET. Gmail and Yahoo
    require `List-Unsubscribe-Post` for bulk senders; a GET-only endpoint gets
    mail filtered regardless of what the law says.
  - Opt-out is recorded through the SAME machinery as a WhatsApp STOP: the
    durable `consent_optouts` table plus the in-memory enforcement set that
    `is_opted_out()` consults and that lifespan hydrates on boot. There is one
    opt-out path, not two.
  - Unsubscribing is IDEMPOTENT and never errors on a repeat click.
  - An invalid or expired token still shows a human a way out (reply STOP, or
    email us) rather than a bare 400 - a dead end here is the same failure we
    are fixing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("smb_broker.unsubscribe")

router = APIRouter(tags=["Compliance"])

# Same secret family as the key-verification tokens.
_DEV_SECRET = "dev-unsub-secret"
_SECRET = os.getenv(
    "UNSUBSCRIBE_SECRET",
    os.getenv("KEY_VERIFY_SECRET", os.getenv("JWT_SIGNING_SECRET", _DEV_SECRET)),
)

# THE SAME GUARD identity.py HAS, WHICH THIS FILE WAS MISSING.
#
# The fallback above is a literal in a PUBLIC repository. If none of the three
# environment variables is set in production, every opt-out token is forgeable
# by anyone who can read this line - which means a third party can unsubscribe
# our users, or craft a link that appears to come from us.
#
# identity.py guards JWT_SIGNING_SECRET exactly this way and
# billing/receipt_signer.py asserts on its key. This one had no guard at all,
# which is the kind of gap that only shows up when someone compares siblings.
if os.getenv("ENVIRONMENT") == "production" and _SECRET == _DEV_SECRET:
    logging.getLogger("smb_broker.unsubscribe").error(
        "SECURITY: no UNSUBSCRIBE_SECRET / KEY_VERIFY_SECRET / "
        "JWT_SIGNING_SECRET is set in production, so opt-out tokens are "
        "signed with the development default published in this repository "
        "and are forgeable by anyone. Set a strong secret and redeploy."
    )
# Long-lived on purpose: someone may unsubscribe from a year-old email, and
# "your opt-out link expired" is not an acceptable answer.
_TTL_S = 365 * 24 * 3600

SUPPORT_EMAIL = "hello@hatchloop.dev"


def make_token(recipient_id: str, channel: str = "email") -> str:
    """Signed, non-forgeable (recipient, channel) token for an opt-out link."""
    expires = int(time.time()) + _TTL_S
    payload = f"{recipient_id}|{channel}|{expires}"
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{b64}.{sig}"


def parse_token(token: str) -> Optional[tuple[str, str]]:
    """Return (recipient_id, channel) if the token is valid, else None."""
    try:
        b64, _, sig = (token or "").partition(".")
        if not b64 or not sig:
            return None
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        expected = hmac.new(_SECRET.encode(), payload.encode(),
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        recipient_id, channel, exp = payload.rsplit("|", 2)
        if time.time() > float(exp):
            return None
        return recipient_id, channel
    except Exception:  # noqa: BLE001
        return None


def unsubscribe_url(recipient_id: str, channel: str = "email") -> str:
    """The link to embed in an outbound message."""
    base = os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")
    return f"{base}/unsubscribe?t={make_token(recipient_id, channel)}"


async def _record_optout(recipient_id: str, channel: str, method: str) -> bool:
    """Register the opt-out in memory AND durably. Best-effort, never raises.

    Memory first: enforcement must take effect immediately even if the database
    write fails. The durable row is what survives a restart - the compliance
    leak we fixed in August was precisely a memory-only opt-out.
    """
    try:
        from compliance.consent_store import get_consent_store
        store = get_consent_store()
        store.mark_opted_out(recipient_id, channel)
        store.revoke_consent(recipient_id, channel, "marketing", method)
    except Exception as exc:  # noqa: BLE001
        logger.warning("unsub_memory_failed err=%s", exc)
    # THE RETURN VALUE USED TO BE DISCARDED, and insert_row cannot raise -
    # it returns None on failure - so the `except` here was dead and the
    # caller went on to render "You are unsubscribed. We will not send you any
    # further messages. This takes effect immediately." on the strength of a
    # write that may never have happened. The RFC 8058 one-click POST returned
    # {"ok": true, "unsubscribed": true} to Gmail and Yahoo the same way.
    #
    # In-memory suppression still works, so the promise holds until the next
    # redeploy - and then quietly stops. For an opt-out that is not an
    # operational detail; it is the difference between honouring a legal
    # request and only appearing to.
    #
    # core/handle_inbound.py already had this right ("opt_out_processed is
    # True ONLY if the durable write succeeds"). Same idiom, applied here.
    # NOT CONFIGURED IS NOT THE SAME AS DOWN.
    #
    # My first version returned 503 whenever the durable write did not happen,
    # which meant every unsubscribe in local dev and in the test suite - where
    # Supabase is deliberately absent - reported a service failure. In that
    # mode in-memory suppression IS the intended behaviour and the promise is
    # honest, which is the codebase's existing "durable is a bonus" pattern.
    #
    # A configured database that then FAILS is a real incident and must be
    # reported as one. The two look identical to the caller of insert_row,
    # which is how they came to be treated alike.
    from storage.supabase_client import _get_config
    _url, _key = _get_config()
    if not (_url and _key):
        return True

    try:
        from storage.supabase_client import insert_row_strict
        await insert_row_strict("consent_optouts", {
            "recipient_id": recipient_id,
            "channel": channel,
            "use_case": "marketing",
            "revocation_method": method,
            "source": "unsubscribe_link",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unsub_durable_failed recipient=%s channel=%s err=%s -- "
            "suppression is IN-MEMORY ONLY and will not survive a restart",
            recipient_id, channel, exc)
        return False


def _page(title: str, body: str, ok: bool = True) -> str:
    colour = "#27ae60" if ok else "#c0392b"
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:34rem;"
        "margin:4rem auto;padding:0 1.25rem;line-height:1.6;color:#18181b}"
        f"h1{{color:{colour};font-size:1.4rem}}"
        "a{color:#0d9488}code{background:#f4f4f5;padding:.1rem .3rem;border-radius:3px}"
        "</style></head><body>"
        f"<h1>{title}</h1>{body}</body></html>"
    )


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(t: str = Query("", description="signed opt-out token")):
    recipient = parse_token(t)
    if not recipient:
        # Never a dead end: a broken link is the very thing this route fixes.
        return HTMLResponse(_page(
            "This opt-out link is not valid",
            "<p>The link may be damaged or very old.</p>"
            f'<p>Email <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a> and we will '
            "remove you by hand, or reply <code>STOP</code> to any message from us.</p>",
            ok=False), status_code=400)

    recipient_id, channel = recipient
    durable = await _record_optout(recipient_id, channel, "unsubscribe_link")
    logger.info("unsubscribed recipient=%s channel=%s durable=%s",
                recipient_id[:4] + "***", channel, durable)
    if not durable:
        # SAY WHAT ACTUALLY HAPPENED. The suppression is real right now but
        # lives only in this process's memory, so a redeploy would silently
        # undo it. Promising "this takes effect immediately" and leaving it
        # there is how someone gets messaged again after opting out.
        return HTMLResponse(_page(
            "You are unsubscribed - please confirm by email",
            f"<p>We have stopped {channel} messages to you right now.</p>"
            "<p>We could not write this to our permanent record, so it may "
            "not survive a system restart. That is our fault, not yours.</p>"
            f'<p><strong>Please email <a href="mailto:{SUPPORT_EMAIL}">'
            f'{SUPPORT_EMAIL}</a></strong> so we can make it permanent, or '
            f'reply <code>STOP</code> to any message from us.</p>',
            ok=False), status_code=503)
    return HTMLResponse(_page(
        "You are unsubscribed",
        f"<p>We will not send you any further {channel} messages.</p>"
        "<p>This takes effect immediately and applies to every AI agent using "
        "HatchLoop - not just the one that contacted you.</p>"
        f'<p style="color:#71717a;font-size:.9rem">Removed in error? '
        f'<a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>'))


@router.post("/unsubscribe")
async def unsubscribe_post(request: Request, t: str = Query("")):
    """RFC 8058 one-click POST.

    Gmail and Yahoo require `List-Unsubscribe-Post` for bulk senders and will
    filter mail without it, so a GET-only endpoint is not enough regardless of
    what the law requires. The mail client POSTs here with no user interaction.
    """
    token = t
    if not token:
        try:
            form = await request.form()
            token = str(form.get("t") or "")
        except Exception:  # noqa: BLE001
            token = ""
    parsed = parse_token(token)
    if not parsed:
        return JSONResponse({"ok": False, "reason": "invalid_token"}, status_code=400)
    recipient_id, channel = parsed
    durable = await _record_optout(recipient_id, channel, "one_click_rfc8058")
    if not durable:
        # Gmail and Yahoo retry a 5xx. Returning 200 here told them the
        # opt-out was recorded when it was not, and there is no second chance
        # to correct that - the mail client never asks again.
        return JSONResponse(
            {"ok": False, "unsubscribed": False, "channel": channel,
             "reason": "not_durably_recorded", "retry": True},
            status_code=503)
    return JSONResponse({"ok": True, "unsubscribed": True, "channel": channel})
