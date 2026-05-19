"""
Paddle webhook → Telegram revenue alerts.

Complements the Cloudflare edge worker's polling-based alerts (see
edge/src/alerts.ts) with a *push* path: every time Paddle confirms real
money has moved (or failed to), the origin handler posts a Telegram
message so Basil hears about it within seconds of the event.

Design rules (mirror the edge worker for consistency):
  - If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing, silently no-op.
  - No cooldown: revenue events are rare; every one is high-signal.
  - All event types log to stdout for audit, even if Telegram is silent.
  - Signature verification is constant-time (hmac.compare_digest).

Paddle's webhook signature format:
  Paddle-Signature: ts=<unix_ts>;h1=<hex_hmac_sha256>
  HMAC payload: f"{ts}:{raw_body}" signed with the webhook secret.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from compliance.log_redactor import mask_email

logger = logging.getLogger("smb_broker.paddle_webhook")

_TELEGRAM_TIMEOUT_S = 6.0
_RESEND_TIMEOUT_S = 10.0

# Public MCP URL the customer points their agent at. The web-checkout page
# hands the same URL out; if you swap origins/CDNs, update both.
_MCP_URL_DEFAULT = "https://smb-broker.onrender.com/mcp"


def verify_paddle_signature(header: str, body: bytes, secret: str) -> bool:
    """
    Verify a Paddle webhook signature.

    Paddle sends `Paddle-Signature: ts=<unix_ts>;h1=<hex_hmac>`.
    The HMAC-SHA256 input is `f"{ts}:{raw_body_bytes_decoded}"` keyed
    by the webhook secret from Paddle's vendor dashboard.

    Returns False on any malformed input rather than raising — the
    caller treats a False return as 401.
    """
    if not header or not secret:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(";") if "=" in p)
    except ValueError:
        return False
    ts = parts.get("ts")
    h1 = parts.get("h1")
    if not ts or not h1:
        return False
    try:
        signed_payload = f"{ts}:{body.decode('utf-8')}".encode("utf-8")
    except UnicodeDecodeError:
        return False
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


async def send_telegram_alert(text: str) -> bool:
    """
    POST `text` to Telegram. Silent no-op if either env var is missing.

    Returns True on HTTP 200 from Telegram, False otherwise (including
    when env vars are missing). Never raises.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("telegram_alert_skipped reason=missing_env_vars")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_TELEGRAM_TIMEOUT_S) as client:
            r = await client.post(url, json=payload)
        if r.status_code != 200:
            logger.warning("telegram_alert_failed status=%s body=%s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001 — never let alert failure bubble
        logger.warning("telegram_alert_exception err=%s", e)
        return False


def _extract_customer_email(event: dict[str, Any]) -> str | None:
    """
    Pull the customer email out of a Paddle event.

    Paddle isn't consistent: depending on the event type the address lives
    at any of these paths. We check the common locations defensively and
    return the first non-empty hit. Returns None when nothing is found.
    """
    data = event.get("data") or {}
    candidates = [
        data.get("customer", {}).get("email") if isinstance(data.get("customer"), dict) else None,
        data.get("customer_email"),
        (data.get("billing_details") or {}).get("email") if isinstance(data.get("billing_details"), dict) else None,
        data.get("email"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _extract_plan(event: dict[str, Any]) -> str:
    """
    Pull the plan name out of `event.data.custom_data.plan`. Default to
    "developer" — the cheapest tier — so a misconfigured Paddle product
    never accidentally grants an enterprise scope.
    """
    data = event.get("data") or {}
    custom = data.get("custom_data") or {}
    if isinstance(custom, dict):
        plan = custom.get("plan")
        if isinstance(plan, str) and plan.strip():
            return plan.strip().lower()
    return "developer"


async def send_api_key_email(
    email: str,
    plan: str,
    token: str,
    expires_at: float,
) -> bool:
    """
    Email the freshly-minted Agent-Identity token to the customer.

    Best-effort: returns True on success, False on any failure (missing
    env var, Resend error, network blip). Never raises. The caller logs
    on False and falls through to the Telegram alert so we still hear
    about the activation even if the email send fails.
    """
    if not email:
        logger.warning("api_key_email_skipped reason=no_email plan=%s", plan)
        return False

    expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="seconds")
    mcp_url = os.getenv("MCP_PUBLIC_URL", _MCP_URL_DEFAULT)
    plan_display = plan.capitalize() if plan else "Developer"
    subject = f"Your Agent Broker API key — {plan_display} plan"
    body = (
        f"Welcome to Agent Broker ({plan_display} plan).\n"
        "\n"
        "Your API key (Agent-Identity token):\n"
        f"{token}\n"
        "\n"
        f"Expires: {expires_iso}\n"
        f"MCP endpoint: {mcp_url}\n"
        "\n"
        "Send the token on every request as the X-Agent-Identity header,\n"
        "e.g. curl -H 'X-Agent-Identity: <token>' <endpoint>.\n"
        "\n"
        "Reply to this email if anything looks off."
    )

    # Try the in-repo adapter first — it handles compliance, CAN-SPAM
    # footers, etc. The adapter is intended for outbound business messaging,
    # so we only fall through to it when it's importable; this email is
    # transactional and the bare Resend POST below is sufficient.
    try:
        from channels.sms_email.resend_email import ResendEmailAdapter  # type: ignore
        from channels.adapter_interface import ChannelRequest  # type: ignore

        adapter = ResendEmailAdapter()
        # Empty API key → adapter returns a stub success; we'd rather know
        # it failed, so check the env var ourselves.
        if not os.getenv("RESEND_API_KEY", "").strip():
            logger.warning("api_key_email_skipped reason=no_resend_key plan=%s", plan)
            return False
        resp = await adapter.send(ChannelRequest(  # type: ignore[call-arg]
            recipient_id=email,
            channel="email",
            message_type="transactional",
            content=body,
            subject=subject,
            country_code=None,
            state_code=None,
            agent_id="smb-broker-provisioning",
            trace_id=None,
        ))
        if getattr(resp, "success", False):
            logger.info("api_key_email_sent via=adapter email=%s plan=%s", mask_email(email), plan)
            return True
        logger.warning(
            "api_key_email_adapter_failed err=%s plan=%s",
            getattr(resp, "error_message", "unknown"), plan,
        )
        # Fall through to direct httpx attempt — adapter may have failed
        # for a non-Resend reason (e.g. compliance pre-check rejecting the
        # body); the raw POST below skips that step.
    except Exception as e:  # noqa: BLE001 — adapter import/init is best-effort
        logger.info("api_key_email_adapter_unavailable err=%s falling_back_to_httpx", e)

    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        logger.warning("api_key_email_skipped reason=no_resend_key plan=%s", plan)
        return False
    from_email = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
    try:
        async with httpx.AsyncClient(timeout=_RESEND_TIMEOUT_S) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_email,
                    "to": [email],
                    "subject": subject,
                    "text": body,
                },
            )
        if 200 <= r.status_code < 300:
            logger.info("api_key_email_sent via=httpx email=%s plan=%s", mask_email(email), plan)
            return True
        logger.warning(
            "api_key_email_failed status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return False
    except Exception as e:  # noqa: BLE001 — never let email failure bubble
        logger.warning("api_key_email_exception err=%s", e)
        return False


def _extract_amount(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Pull (amount, currency) out of a Paddle transaction.completed payload.

    Paddle nests the figure under `data.details.totals.total` (minor units,
    string) with `data.currency_code`. Falls back to legacy fields if the
    canonical path is absent. Returns (None, None) when nothing usable
    is found.
    """
    data = event.get("data") or {}
    currency = data.get("currency_code") or data.get("currency")
    # Canonical: data.details.totals.total (string, minor units)
    details = data.get("details") or {}
    totals = details.get("totals") or {}
    raw = totals.get("total") or totals.get("grand_total")
    if raw is None:
        # Fallbacks for older event shapes
        raw = data.get("amount") or data.get("total")
    if raw is None:
        return None, currency
    try:
        # Paddle returns minor units as a string ("1999" = $19.99)
        minor = int(str(raw))
        major = f"{minor / 100:.2f}"
    except (TypeError, ValueError):
        major = str(raw)
    return major, currency


async def handle_paddle_event(event: dict[str, Any]) -> None:
    """
    Dispatch a verified Paddle event to the appropriate Telegram alert.

    Unknown event types are logged and ignored (silent 200 upstream).
    Never raises — caller always returns 200 to Paddle after this.
    """
    event_type = event.get("event_type")
    if not event_type:
        logger.info("paddle_event_missing_type payload_keys=%s", list(event.keys()))
        return

    event_id = event.get("event_id") or "?"
    logger.info("paddle_event_received type=%s id=%s", event_type, event_id)

    if event_type == "subscription.activated":
        data = event.get("data") or {}
        sub_id = data.get("id", "?")
        customer_id = data.get("customer_id", "?")
        plan = _extract_plan(event)
        customer_email = _extract_customer_email(event)

        # 1. Mint the API key. This is the critical path — if it fails the
        #    customer has paid but has no way to use the service, so we let
        #    the exception surface to logs but never raise out of the
        #    handler (caller treats handler failure as "still return 200").
        token_value: str | None = None
        token_suffix = "????????????"
        expires_at_iso = "?"
        try:
            from agent_interface.identity import issue_subscription_token
            token_resp = issue_subscription_token(
                customer_id=str(customer_id),
                plan=plan,
                customer_email=customer_email or "",
            )
            token_value = token_resp.token
            token_suffix = token_value[-12:] if len(token_value) >= 12 else token_value
            expires_at_iso = datetime.fromtimestamp(
                token_resp.expires_at, tz=timezone.utc,
            ).isoformat(timespec="seconds")
            logger.info(
                "subscription_token_issued customer=%s plan=%s exp=%s",
                customer_id, plan, expires_at_iso,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("subscription_token_issue_failed err=%s", e)

        # 2. Email the customer their key. Best-effort.
        if token_value and customer_email:
            try:
                await send_api_key_email(
                    customer_email, plan, token_value, token_resp.expires_at,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("api_key_email_unexpected_error err=%s", e)
        elif token_value:
            logger.warning(
                "api_key_email_skipped reason=no_customer_email customer=%s plan=%s",
                customer_id, plan,
            )

        # 3. Telegram audit alert — last-12 of token only, masked email,
        #    never the full key.
        text = (
            "*Agent Broker* — first-revenue / new subscription!\n"
            f"Subscription: `{sub_id}`\n"
            f"Customer: `{customer_id}`\n"
            f"Plan: `{plan}`\n"
            f"Email: `{mask_email(customer_email) if customer_email else 'unknown'}`\n"
            f"Token suffix: `...{token_suffix}`\n"
            f"Token expires: `{expires_at_iso}`\n"
            f"Event: `{event_id}`"
        )
        await send_telegram_alert(text)
        return

    if event_type == "transaction.completed":
        amount, currency = _extract_amount(event)
        data = event.get("data") or {}
        txn_id = data.get("id", "?")
        amount_line = f"{amount} {currency}" if amount and currency else (amount or currency or "?")
        text = (
            "*Agent Broker* — payment cleared!\n"
            f"Amount: *{amount_line}*\n"
            f"Transaction: `{txn_id}`\n"
            f"Event: `{event_id}`"
        )
        await send_telegram_alert(text)
        return

    if event_type == "subscription.canceled":
        data = event.get("data") or {}
        sub_id = data.get("id", "?")
        customer_id = data.get("customer_id", "?")
        # Audit-log line a future revocation list can replay. We don't
        # maintain one today because tokens expire in 90 days and the
        # customer keeps using until then by design (no DB needed).
        logger.info(
            "subscription_canceled_for_revocation_list customer=%s subscription=%s event=%s",
            customer_id, sub_id, event_id,
        )
        text = (
            "*Agent Broker* — subscription canceled.\n"
            f"Subscription: `{sub_id}`\n"
            f"Customer: `{customer_id}`\n"
            f"Event: `{event_id}`"
        )
        await send_telegram_alert(text)
        return

    if event_type == "transaction.payment_failed":
        amount, currency = _extract_amount(event)
        data = event.get("data") or {}
        txn_id = data.get("id", "?")
        amount_line = f"{amount} {currency}" if amount and currency else (amount or currency or "?")
        text = (
            "*Agent Broker* — payment FAILED, investigate.\n"
            f"Amount: *{amount_line}*\n"
            f"Transaction: `{txn_id}`\n"
            f"Event: `{event_id}`"
        )
        await send_telegram_alert(text)
        return

    logger.info("paddle_event_unhandled type=%s id=%s", event_type, event_id)
