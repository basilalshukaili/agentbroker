"""
Polar webhook → issue Agent-Identity token (the fiat rail).

Polar is the *human-developer prepay* rail that coexists with x402 (the
autonomous-agent crypto rail): a developer buys access via Polar's hosted
checkout (Polar is Merchant of Record — handles card + global tax + payout to
Oman), and on payment we mint a long-lived Agent-Identity token their agent then
sends as `X-Agent-Identity`. Reads stay free; with a valid token, writes are
pre-paid (skip the x402 402).

Signature verification follows the **Standard Webhooks** spec
(https://www.standardwebhooks.com) — which Polar implements. We verify it
ourselves (no SDK dependency, to avoid forcing an httpx upgrade) and prove the
implementation against the spec's published test vector in the unit test.

Headers (case-insensitive): `webhook-id`, `webhook-timestamp`, `webhook-signature`.
Secret is `whsec_<base64>`; the HMAC key is the base64-decoded bytes after the
prefix. Signed content is `{id}.{timestamp}.{body}`; expected signature is
`base64(HMAC_SHA256(key, signed_content))`. The signature header is a
space-separated list of `v1,<sig>` entries (key rotation) — we accept a match
against any. Returns 401 on bad signature so Polar surfaces the failure.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger("smb_broker.polar_webhook")

# Reject events whose timestamp is older/newer than this (replay protection).
_TIMESTAMP_TOLERANCE_S = 5 * 60


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header read. Accepts the svix-* aliases Polar may send."""
    # FastAPI/Starlette Headers are already case-insensitive, but accept a plain
    # dict too (for tests). Try the standard name and the svix-prefixed alias.
    for key in (name, f"svix-{name.split('-', 1)[1]}" if "-" in name else name):
        try:
            v = headers.get(key)  # type: ignore[union-attr]
        except Exception:
            v = None
        if v:
            return v
    return ""


def _key_bytes(secret: str) -> bytes:
    """Standard Webhooks secret → raw HMAC key. `whsec_<base64>` → decode base64."""
    s = secret.strip()
    if s.startswith("whsec_"):
        s = s[len("whsec_"):]
    try:
        return base64.b64decode(s)
    except Exception:
        # Some setups pass a raw (non-base64) secret; fall back to its bytes.
        return secret.encode("utf-8")


def verify_polar_signature(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    enforce_timestamp: bool = True,
) -> bool:
    """Verify a Standard Webhooks signature. Returns False on any problem
    (missing headers/secret, stale timestamp, no signature match)."""
    if not secret:
        return False
    msg_id = _header(headers, "webhook-id")
    ts = _header(headers, "webhook-timestamp")
    sig_header = _header(headers, "webhook-signature")
    if not (msg_id and ts and sig_header):
        return False

    if enforce_timestamp:
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        now = int(time.time())
        if abs(now - ts_int) > _TIMESTAMP_TOLERANCE_S:
            logger.warning("polar_webhook_timestamp_out_of_tolerance delta=%s", now - ts_int)
            return False

    try:
        body_str = body.decode("utf-8")
    except UnicodeDecodeError:
        return False

    signed_content = f"{msg_id}.{ts}.{body_str}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(_key_bytes(secret), signed_content, hashlib.sha256).digest()
    ).decode("ascii")

    # Header is space-separated "v1,<sig>" tokens; accept a constant-time match
    # against any (supports secret rotation).
    for token in sig_header.split(" "):
        _, _, sig = token.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return True
    return False


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------

# Polar product/price → our internal plan. Default "developer" so a paying
# customer never fails closed on an unmapped product.
def _extract_plan(data: dict[str, Any]) -> str:
    meta = data.get("metadata") or data.get("customer_metadata") or {}
    if isinstance(meta, dict):
        plan = meta.get("plan")
        if isinstance(plan, str) and plan.strip():
            return plan.strip().lower()
    # Fall back to product name heuristics.
    product = data.get("product") or {}
    name = (product.get("name") if isinstance(product, dict) else "") or ""
    name = name.lower()
    if "enterprise" in name:
        return "enterprise"
    if "business" in name or "pro" in name:
        return "business"
    return "developer"


def _extract_email(data: dict[str, Any]) -> str | None:
    customer = data.get("customer")
    if isinstance(customer, dict) and customer.get("email"):
        return str(customer["email"]).strip()
    for key in ("customer_email", "email", "user_email"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    user = data.get("user")
    if isinstance(user, dict) and user.get("email"):
        return str(user["email"]).strip()
    return None


def _extract_customer_id(data: dict[str, Any]) -> str:
    customer = data.get("customer")
    if isinstance(customer, dict) and customer.get("id"):
        return str(customer["id"])
    return str(data.get("customer_id") or data.get("id") or "polar_customer")


# Events that mean "money cleared, grant access".
_GRANT_EVENTS = {"order.paid", "order.created", "subscription.active", "subscription.created"}


async def handle_polar_event(event: dict[str, Any]) -> None:
    """Dispatch a verified Polar event. On a paid order/subscription, mint an
    Agent-Identity token, email it, and fire the Telegram revenue alert. Never
    raises — the caller always returns 200 after a valid signature."""
    event_type = event.get("type") or event.get("event_type") or ""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    logger.info("polar_event_received type=%s", event_type)

    if event_type not in _GRANT_EVENTS:
        logger.info("polar_event_unhandled type=%s", event_type)
        return

    # An order for a $0 / unpaid status shouldn't grant. Polar order.paid implies
    # paid; guard order.created on a paid flag if present.
    status = str(data.get("status") or "").lower()
    if event_type == "order.created" and status and status not in ("paid", "succeeded", "completed"):
        logger.info("polar_order_not_paid status=%s — skipping grant", status)
        return

    email = _extract_email(data)
    plan = _extract_plan(data)
    customer_id = _extract_customer_id(data)

    token_value: str | None = None
    token_suffix = "????????????"
    expires_iso = "?"
    try:
        from agent_interface.identity import issue_subscription_token
        token_resp = issue_subscription_token(
            customer_id=customer_id, plan=plan, customer_email=email or "",
        )
        token_value = token_resp.token
        token_suffix = token_value[-12:] if len(token_value) >= 12 else token_value
        expires_iso = datetime.fromtimestamp(
            token_resp.expires_at, tz=timezone.utc,
        ).isoformat(timespec="seconds")
        logger.info("polar_token_issued customer=%s plan=%s exp=%s", customer_id, plan, expires_iso)
    except Exception as e:  # noqa: BLE001
        logger.exception("polar_token_issue_failed err=%s", e)

    # Email the key (best-effort, reuses the Resend path).
    if token_value and email:
        try:
            from billing.telegram_revenue_alerts import send_api_key_email
            await send_api_key_email(email, plan, token_value, token_resp.expires_at)
        except Exception as e:  # noqa: BLE001
            logger.warning("polar_api_key_email_failed err=%s", e)

    # Telegram revenue alert (reuses the existing sender). Mask email, last-12 of token only.
    try:
        from billing.telegram_revenue_alerts import send_telegram_alert
        from compliance.log_redactor import mask_email
        amount = data.get("amount") or data.get("total_amount")
        currency = (data.get("currency") or "usd").upper()
        amount_str = f"{int(amount)/100:.2f} {currency}" if isinstance(amount, (int, float)) else "?"
        await send_telegram_alert("\n".join([
            "*Agent Broker* — a developer PAID via Polar (card / fiat)! 💳",
            f"Amount: *{amount_str}*",
            f"Plan: `{plan}`",
            f"Email: `{mask_email(email) if email else 'unknown'}`",
            f"Token suffix: `...{token_suffix}`  (expires {expires_iso})",
            "Their agent can now call paid tools pre-paid (X-Agent-Identity).",
        ]))
    except Exception as e:  # noqa: BLE001
        logger.warning("polar_telegram_alert_failed err=%s", e)
