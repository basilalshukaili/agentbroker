"""The sign-in link email.

It reuses the service's existing mail path - Resend, from `hello@hatchloop.dev`, the same sender the
email-verified free key and the portal already use - but reports the outcome with THREE answers instead of
two, because the caller must say different things for each:

  sent         Resend accepted the message for delivery.
  rejected     Resend refused THIS ADDRESS (HTTP 422 validation_error: a reserved or malformed address). The
               person typed something that cannot receive mail; tell them so and let them correct it.
  unavailable  We could not hand the message to a mail provider (no API key configured, provider down,
               network). Nothing was sent, nobody should be told to check their inbox.

The existing free-key path folds the second into the third and answers 503 `onboarding_unavailable`. That is
what the 2026-10-01 release smoke test saw: Resend answered `422 Invalid to field ... domains like
example.com`, so a test that used example.com looked like an outage. (Diagnosed 2026-10-03 from the live
container's log, addresses redacted: RESEND_API_KEY is set; the one failure was that test address.)

The message names the app that is asking and the host it will return to, because the link authorises that
app. It never contains a credential - only a single-use link that does nothing until the person presses a
button on the page it opens.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Callable, Optional

log = logging.getLogger("smb_broker.oauth.email")

SENT, REJECTED, UNAVAILABLE = "sent", "rejected", "unavailable"

# Replaceable in tests (an httpx.AsyncClient factory); production uses the default.
_client_factory: Optional[Callable] = None


def _client():
    import httpx
    if _client_factory is not None:
        return _client_factory()
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


def compose(link: str, app_label: str, return_host: str, minutes: int = 15) -> tuple:
    """(subject, html, text). Every dynamic value is escaped; the link is the only URL."""
    app = html.escape(app_label)
    host = html.escape(return_host)
    esc_link = html.escape(link, quote=True)
    subject = "Confirm sign-in to HatchLoop AgentBroker"
    body_html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f9fafb;font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#18181b;">'
        '<div style="max-width:560px;margin:32px auto;background:#fff;border:1px solid #e4e4e7;border-radius:12px;padding:32px;">'
        '<h1 style="font-size:20px;margin:0 0 12px;">Confirm sign-in</h1>'
        f'<p style="font-size:15px;line-height:1.6;color:#3f3f46;margin:0 0 8px;"><strong>{app}</strong> '
        'is asking to use AgentBroker as you.</p>'
        f'<p style="font-size:13px;line-height:1.5;color:#71717a;margin:0 0 20px;">It will be sent back to {host}.</p>'
        f'<p style="margin:0 0 20px;"><a href="{esc_link}" style="display:inline-block;background:#34d399;color:#18181b;'
        'font-weight:700;font-size:15px;padding:12px 28px;border-radius:9999px;text-decoration:none;">Review and confirm</a></p>'
        f'<p style="font-size:13px;line-height:1.5;color:#52525b;margin:0 0 8px;">The link works once and expires in {minutes} minutes. '
        'Opening it shows what you are approving; nothing happens until you press the button on that page.</p>'
        '<p style="font-size:13px;line-height:1.5;color:#52525b;margin:0;">If you did not start this, ignore this email - '
        'no one can sign in without pressing the button.</p>'
        '</div></body></html>')
    body_text = (
        f"{app_label} is asking to use HatchLoop AgentBroker as you.\n"
        f"It will be sent back to {return_host}.\n\n"
        f"Review and confirm (works once, expires in {minutes} minutes):\n{link}\n\n"
        "Opening the link shows what you are approving; nothing happens until you press the button on that page.\n"
        "If you did not start this, ignore this email.\n")
    return subject, body_html, body_text


async def send_signin_link(to_email: str, link: str, app_label: str, return_host: str) -> str:
    """Send the link. Returns SENT, REJECTED or UNAVAILABLE. Never raises, never logs the address or link."""
    key = os.getenv("RESEND_API_KEY", "")
    if not key:
        log.warning("oauth_signin_email_unavailable reason=RESEND_API_KEY_unset")
        return UNAVAILABLE
    subject, body_html, body_text = compose(link, app_label, return_host)
    payload = {"from": "HatchLoop <hello@hatchloop.dev>", "to": [to_email], "subject": subject,
               "html": body_html, "text": body_text}
    try:
        async with _client() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth_signin_email_unavailable reason=transport:%s", type(exc).__name__)
        return UNAVAILABLE
    if resp.status_code in (200, 201):
        return SENT
    if resp.status_code == 422:
        log.info("oauth_signin_email_rejected status=422")
        return REJECTED
    log.warning("oauth_signin_email_unavailable reason=http_%s", resp.status_code)
    return UNAVAILABLE
