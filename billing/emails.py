"""
billing/emails.py -- Transactional emails for the credits lifecycle.

WELCOME email: sent on Polar grant (slice 6). Contains the API key, MCP
quickstart config, a first-call example, and portal + invoice links.
LOW-BALANCE nudge: sent when balance drops below ~500cr and the account
has not been notified in the last 24 hours (deduped via low_balance_notified_at).

All sends are via Resend (from "HatchLoop <hello@hatchloop.dev>") and are
best-effort — never raises. Capability-first copy (capability, not protocol).
Light HTML: white background, emerald #059669 accent, dark code blocks.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("smb_broker.emails")

_FROM = "HatchLoop <hello@hatchloop.dev>"
_PORTAL_URL = "https://hatchloop.dev/portal"
_ACCENT = "#059669"
_ACCENT_FILL = "#34d399"
_CODE_BG = "#0f0f11"
_CODE_TEXT = "#d4d4d8"


# ---------------------------------------------------------------------------
# Shared HTML helpers
# ---------------------------------------------------------------------------

def _base(subject: str, body_html: str) -> str:
    """Wrap body_html in a clean light HTML email shell."""
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{subject}</title>"
        "<style>"
        "body{margin:0;padding:0;background:#f9fafb;font-family:system-ui,-apple-system,sans-serif;"
        "color:#18181b;-webkit-font-smoothing:antialiased;}"
        ".wrap{max-width:600px;margin:40px auto;background:#ffffff;border-radius:12px;"
        "border:1px solid #e4e4e7;overflow:hidden;}"
        ".header{background:#ffffff;padding:32px 40px 0;border-bottom:none;}"
        ".logo{display:flex;align-items:center;gap:10px;}"
        ".logo-mark{width:28px;height:28px;background:#34d399;border-radius:8px;display:inline-block;}"
        ".logo-text{font-size:16px;font-weight:700;color:#18181b;letter-spacing:-0.03em;}"
        ".body{padding:32px 40px;}"
        "h1{font-size:24px;font-weight:700;letter-spacing:-0.03em;color:#18181b;margin:0 0 8px;}"
        "p{font-size:15px;line-height:1.6;color:#52525b;margin:0 0 16px;}"
        "pre,code{font-family:\"Geist Mono\",\"Courier New\",monospace;}"
        ".code-block{background:#0f0f11;color:#d4d4d8;border-radius:8px;padding:16px 20px;"
        "font-size:13px;line-height:1.6;overflow-x:auto;margin:16px 0;white-space:pre-wrap;"
        "word-break:break-all;}"
        ".label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;"
        "color:#a1a1aa;margin:24px 0 8px;}"
        ".btn{display:inline-block;background:#34d399;color:#18181b;font-size:14px;font-weight:700;"
        "padding:11px 24px;border-radius:9999px;text-decoration:none;margin-right:8px;margin-bottom:8px;}"
        ".btn-outline{display:inline-block;background:#ffffff;color:#52525b;font-size:14px;font-weight:600;"
        "padding:11px 24px;border-radius:9999px;text-decoration:none;border:1px solid #d4d4d8;"
        "margin-right:8px;margin-bottom:8px;}"
        ".divider{border:none;border-top:1px solid #e4e4e7;margin:24px 0;}"
        ".footer{padding:24px 40px;background:#f9fafb;border-top:1px solid #e4e4e7;}"
        ".footer p{font-size:12px;color:#a1a1aa;margin:0;}"
        ".badge{display:inline-block;background:#f0fdf4;color:#059669;border:1px solid #bbf7d0;"
        "border-radius:9999px;font-size:11px;font-weight:600;padding:2px 10px;"
        "letter-spacing:0.05em;}"
        "</style></head><body>"
        "<div class=\"wrap\">"
        "<div class=\"header\">"
        "<div class=\"logo\">"
        "<div class=\"logo-mark\"></div>"
        "<span class=\"logo-text\">HatchLoop</span>"
        "</div></div>"
        "<div class=\"body\">"
        + body_html +
        "</div>"
        "<div class=\"footer\">"
        "<p>HatchLoop &mdash; AI infrastructure that does real work.<br>"
        "Questions? Reply to this email or visit <a href=\"https://hatchloop.dev\" "
        "style=\"color:#059669;\">hatchloop.dev</a>.</p>"
        "</div></div></body></html>"
    )


async def _send(to: str, subject: str, html: str) -> bool:
    """Send via Resend. Returns True on success. Never raises."""
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        logger.warning("emails._send skipped: RESEND_API_KEY not set (to=%s subject=%r)", to, subject)
        return False
    try:
        import httpx
        payload = {"from": _FROM, "to": [to], "subject": subject, "html": html}
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code in (200, 201):
            logger.info("email_sent to=%s subject=%r", to, subject)
            return True
        logger.warning(
            "email_send_failed to=%s status=%s body=%s",
            to, resp.status_code, resp.text[:300],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_send_exception to=%s err=%s", to, exc)
    return False


# ---------------------------------------------------------------------------
# MCP quickstart config block
# ---------------------------------------------------------------------------

_MCP_CONFIG = """{
  "mcpServers": {
    "agent-broker": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-fetch"],
      "env": {
        "SERVER_URL": "https://hatchloop.dev/mcp/agent-broker",
        "X-Agent-Identity": "YOUR_API_KEY_HERE"
      }
    }
  }
}"""

_FIRST_CALL_EXAMPLE = """// Free preview — no credits used
POST https://hatchloop.dev/mcp/agent-broker
{ "method": "tools/call", "params": { "name": "preview_cost",
  "arguments": { "operation": "capture_lead" } } }
// → { "cost": { "amount": 0.05 } }   ← 5 credits

// Paid call — 5 credits debited on success
POST https://hatchloop.dev/mcp/agent-broker
{ "method": "tools/call", "params": { "name": "capture_lead",
  "arguments": { "smb_id": "...", "lead": { ... } } } }
// → { "status": "success", "credits": { "charged": 5, "balance": 995 } }"""


# ---------------------------------------------------------------------------
# WELCOME email (sent on Polar grant)
# ---------------------------------------------------------------------------

async def send_welcome_email(
    email: str,
    credits: int,
    api_key: str,
    order_id: Optional[str] = None,
    amount_usd: Optional[float] = None,
) -> bool:
    """Send the WELCOME email with credits balance, API key, quickstart, and portal link.

    Capability-first: leads with what the AI can now do, not the protocol name.
    Key is shown in a dark code block ("copy & store, not shown again").
    Returns True on successful Resend delivery.
    """
    amount_str = f"${amount_usd:.2f}" if amount_usd else f"${credits / 100:.2f}"
    subject = f"Your HatchLoop credits are live — {credits:,} credits"

    body = (
        f"<h1>Your AI is ready to work.</h1>"
        f"<p>You now have <strong>{credits:,} credits</strong> ({amount_str}) loaded on AgentBroker. "
        f"Credits never expire and reads are always free.</p>"
        f"<p style=\"color:#059669;font-weight:600;\">Your AI can now: verify any company, screen "
        f"sanctions lists, check what&rsquo;s legal to ship, capture leads, and schedule appointments "
        f"&mdash; all without human intervention.</p>"
        f"<hr class=\"divider\">"
        f"<div class=\"label\">Your API key &mdash; copy &amp; store, not shown again</div>"
        f"<div class=\"code-block\">{api_key}</div>"
        f"<p style=\"font-size:13px;color:#a1a1aa;\">Send this as the "
        f"<code style=\"background:#f4f4f5;padding:1px 5px;border-radius:3px;\">X-Agent-Identity</code> "
        f"header on every call to AgentBroker.</p>"
        f"<hr class=\"divider\">"
        f"<div class=\"label\">MCP quickstart &mdash; add to your Claude / Cursor config</div>"
        f"<div class=\"code-block\">{_MCP_CONFIG.replace('YOUR_API_KEY_HERE', api_key[:20] + '...')}</div>"
        f"<hr class=\"divider\">"
        f"<div class=\"label\">Your first calls</div>"
        f"<div class=\"code-block\">{_FIRST_CALL_EXAMPLE}</div>"
        f"<p style=\"font-size:13px;color:#a1a1aa;\">preview_cost is always free. "
        f"Paid calls only deduct credits on success &mdash; no charge for failures.</p>"
        f"<hr class=\"divider\">"
        f"<div style=\"margin-top:8px;\">"
        f"<a class=\"btn\" href=\"{_PORTAL_URL}\">Open your portal</a>"
        f"<a class=\"btn-outline\" href=\"https://hatchloop.dev/docs\">View the docs</a>"
        f"</div>"
    )

    return await _send(email, subject, _base(subject, body))


# ---------------------------------------------------------------------------
# LOW-BALANCE nudge
# ---------------------------------------------------------------------------

async def send_low_balance_email(email: str, balance: int) -> bool:
    """Send the low-balance nudge email. Dedup enforced by caller (24h window).

    Capability-first: reassures the user that reads are free and only
    paid actions consume credits. Provides a direct top-up link.
    """
    subject = f"Your HatchLoop balance is low — {balance:,} credits remaining"
    top_up_url = f"{_PORTAL_URL}#topup"

    body = (
        f"<h1>Running low on credits.</h1>"
        f"<p>Your AgentBroker balance is <strong>{balance:,} credits</strong>. "
        f"That&rsquo;s enough for roughly {balance // 5} lead captures "
        f"or {balance // 2} messages.</p>"
        f"<p><span class=\"badge\">Reads stay free</span>&nbsp; "
        f"Company lookups, sanctions screens, and compliance checks never cost credits. "
        f"Only actions like capturing leads or sending messages draw from your balance.</p>"
        f"<p>Top up any time &mdash; packages start at $9 for 1,000 credits.</p>"
        f"<div style=\"margin-top:24px;\">"
        f"<a class=\"btn\" href=\"{top_up_url}\">Top up your balance</a>"
        f"<a class=\"btn-outline\" href=\"{_PORTAL_URL}\">View your portal</a>"
        f"</div>"
        f"<hr class=\"divider\">"
        f"<p style=\"font-size:13px;color:#a1a1aa;\">"
        f"You&rsquo;re receiving this because your AgentBroker balance dropped below 500 credits. "
        f"We&rsquo;ll remind you again in 24 hours if the balance stays low.</p>"
    )

    return await _send(email, subject, _base(subject, body))
