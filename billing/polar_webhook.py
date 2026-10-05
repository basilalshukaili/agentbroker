"""
Polar webhook → issue Agent-Identity token (the fiat rail).

Polar is the *human-developer prepay* rail that coexists with x402 (the
autonomous-agent crypto rail): a developer buys access via Polar's hosted
checkout (Polar is Merchant of Record — handles card + global tax + payout to
Oman), and on payment we mint a long-lived Agent-Identity token their agent then
sends as `X-Agent-Identity`. Reads stay free; with a valid token, writes are
pre-paid (skip the x402 402).

Signature verification supports Polar's legacy HMAC and Standard Webhooks
schemes without an SDK dependency. Polar secrets generated before 2026-09-08
use the UTF-8 bytes of the full secret (including whsec_); newer secrets use
the base64-decoded bytes after that prefix. Try both interpretations, as the
secret itself does not identify its generation date:
https://polar.sh/docs/integrate/webhooks/delivery

Headers (case-insensitive): webhook-id, webhook-timestamp, webhook-signature.
Signed content is {id}.{timestamp}.{body}; expected signature is
base64(HMAC_SHA256(key, signed_content)). The signature header is a
space-separated list of v1,<sig> entries (key rotation) -- we accept a match
against any supported entry. Returns 401 on bad signature.
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


def _key_candidates(secret: str) -> tuple[bytes, ...]:
    """Legacy Polar uses the full UTF-8 secret; Standard Webhooks decodes it."""
    keys = [secret.encode("utf-8")]
    s = secret.strip()
    if s.startswith("whsec_"):
        s = s[len("whsec_"):]
    try:
        decoded = base64.b64decode(s, validate=True)
    except ValueError:
        pass
    else:
        if decoded and decoded != keys[0]:
            keys.append(decoded)
    return tuple(keys)


def verify_polar_signature(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    enforce_timestamp: bool = True,
) -> bool:
    """Verify either supported Polar signature scheme. Returns False on any problem
    (missing headers/secret, stale timestamp, no signature match)."""
    if not secret or not secret.strip():
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
    expected_signatures = [
        base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode("ascii")
        for key in _key_candidates(secret)
    ]

    # Header is space-separated "v1,<sig>" tokens; compare only supported
    # versions, using constant-time comparison for each candidate key.
    for token in sig_header.split(" "):
        version, _, sig = token.partition(",")
        if version != "v1" or not sig or not sig.isascii():
            continue
        if any(hmac.compare_digest(sig, expected) for expected in expected_signatures):
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
    order = data.get("order")
    if isinstance(order, dict):
        nested_customer = order.get("customer")
        if isinstance(nested_customer, dict) and nested_customer.get("id"):
            return str(nested_customer["id"])
        if order.get("customer_id"):
            return str(order["customer_id"])
    return str(data.get("customer_id") or data.get("id") or "polar_customer")


def _extract_order_id(data: dict[str, Any]) -> str:
    """Best-effort order-id extraction across Polar's Order and Refund payload
    shapes. `order.paid`/`order.created`/`order.refunded` carry the Order
    object directly under `data` (so `data.id` IS the order id); `refund.
    created`/`refund.updated` carry a Refund object that references its
    order. Defensive like `_extract_plan`/`_extract_customer_id` above --
    never raises, returns "" if nothing usable is found (callers must treat
    that as "cannot dedupe" rather than a match)."""
    order = data.get("order")
    if isinstance(order, dict) and order.get("id"):
        return str(order["id"])
    for key in ("order_id", "id"):
        v = data.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


# Events that mean "money cleared, grant access".
_GRANT_EVENTS = {"order.paid", "order.created", "subscription.active", "subscription.created"}

# Refund events require the settlement/benefit gate below; subscription.revoked
# is terminal. A cancellation alone does not revoke an active paid period.
_REVOKE_EVENTS = {"order.refunded", "refund.created", "refund.updated", "subscription.revoked"}


def _paid_order(event_type: str, data: dict[str, Any]) -> bool:
    status = str(data.get("status") or "").lower()
    if data.get("paid") is False:
        return False
    if event_type == "order.created":
        return status in {"paid", "succeeded", "completed"}
    # Older signed order.paid payloads omit status. Explicit contradictory
    # status never authorizes fulfillment even when the event type says paid.
    return not status or status in {"paid", "succeeded", "completed"}


def _terminal_revocation(event_type: str, data: dict[str, Any]) -> bool:
    """Do not turn pending/partial refunds into permanent customer revocation.

    Official schemas: https://github.com/polarsource/polar/blob/main/server/polar/refund/schemas.py
    and https://github.com/polarsource/polar/blob/main/server/polar/order/schemas.py.
    Refund creation is not settlement; order.refunded includes partial refunds.
    """
    if event_type == "subscription.revoked":
        return True
    status = str(data.get("status") or "").lower()
    if event_type in {"refund.created", "refund.updated"}:
        if status in {"pending", "failed", "canceled"} or data.get("revoke_benefits") is False:
            return False
        if status != "succeeded" or data.get("revoke_benefits") is not True:
            raise PaidOrderFulfillmentError()
        return True
    if status == "refunded":
        return True
    amounts = [data.get(key) for key in ("total_amount", "refunded_amount", "refunded_tax_amount")]
    if all(type(value) is int and value >= 0 for value in amounts) and amounts[0] > 0:
        return amounts[1] + amounts[2] >= amounts[0]
    if status == "partially_refunded":
        return False
    raise PaidOrderFulfillmentError()

# Legacy ledger for unprovisioned compatibility only. The scoped fulfillment
# receipt replaces these best-effort reads/writes for all provisioned paid orders.
_POLAR_EVENTS_TABLE = "polar_order_events"


async def _already_processed(order_id: str) -> bool:
    """Durable idempotency check: has this order id already been granted?
    Returns False (i.e. "not a duplicate, proceed") on any lookup failure --
    an unreachable store must never block a real payment from being honored."""
    if not order_id:
        return False
    try:
        from storage.supabase_client import select_rows
        rows = await select_rows(
            _POLAR_EVENTS_TABLE, filters={"order_id": order_id, "status": "processed"},
        )
        return bool(rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug("polar_idempotency_check_failed order_id=%s err=%s", order_id, exc)
        return False


class PaidOrderFulfillmentError(RuntimeError):
    """Retryable fulfillment failure; never expose provider/storage details."""

    def __init__(self) -> None:
        super().__init__("Paid order fulfillment incomplete; retry later.")


async def _mark_processed(order_id: str, event_type: str, customer_id: str) -> None:
    """Legacy best-effort marker; the scoped path never calls this helper."""
    if not order_id:
        return
    try:
        from storage.supabase_client import insert_row
        row = {
            "order_id": order_id,
            "event_type": event_type,
            "customer_id": customer_id,
            "status": "processed",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await insert_row(_POLAR_EVENTS_TABLE, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("polar_mark_processed_failed order_id=%s err=%s", order_id, exc)


async def _handle_revoke_event(event_type: str, data: dict[str, Any]) -> None:
    """A refund/revocation event: revoke the customer's Agent-Identity
    token(s) immediately so a refunded order stops working right away
    instead of riding out its natural 90-day expiry."""
    order_id = _extract_order_id(data)
    customer_id = _extract_customer_id(data)
    logger.info(
        "polar_revoke_event_received type=%s order_id=%s customer=%s",
        event_type, order_id, customer_id,
    )
    try:
        from agent_interface.identity import revoke_customer
        _durable = await revoke_customer(
            customer_id=customer_id, order_id=order_id, reason=event_type)
        if not _durable:
            # The revocation holds in memory but did not persist, so it dies
            # at the next restart and the refunded customer gets their access
            # back. Escalate it the same way an undelivered paid order is
            # escalated - a refund that silently un-refunds is the same class
            # of money problem, pointing the other way.
            logger.error(
                "REVOCATION_NOT_DURABLE customer=%s order=%s -- access will "
                "return on the next restart", customer_id, order_id)
            try:
                from billing.telegram_revenue_alerts import send_telegram_alert
                await send_telegram_alert(
                    "REFUND DID NOT STICK" + chr(10) + chr(10)
                    + f"customer: {customer_id}" + chr(10)
                    + f"order: {order_id}" + chr(10) + chr(10)
                    + "The revocation is in memory only. After the next "
                      "restart this refunded customer regains paid access. "
                      "Re-run the revocation once the database is reachable.")
            except Exception:                   # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.exception("polar_revoke_failed customer=%s err=%s", customer_id, e)




async def _handle_legacy_event(event: dict[str, Any]) -> None:
    """Preserve unprovisioned identity delivery; never grants credits."""
    event_type = event.get("type") or event.get("event_type") or ""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    logger.info("polar_event_received type=%s", event_type)
    if event_type in _REVOKE_EVENTS:
        await _handle_revoke_event(event_type, data)
        return
    if event_type not in _GRANT_EVENTS:
        logger.info("polar_event_unhandled type=%s", event_type)
        return

    status = str(data.get("status") or "").lower()
    if event_type in {"order.created", "order.paid"} and not _paid_order(event_type, data):
        logger.info("polar_order_not_paid status=%s — skipping grant", status)
        return

    order_id = _extract_order_id(data)
    if order_id and await _already_processed(order_id):
        logger.info("polar_event_duplicate_skipped type=%s order_id=%s", event_type, order_id)
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
        expires_iso = datetime.fromtimestamp(token_resp.expires_at, tz=timezone.utc).isoformat(timespec="seconds")
        logger.info("polar_token_issued customer=%s plan=%s exp=%s", customer_id, plan, expires_iso)
        # Grant and mint must both complete before suppressing further delivery.
        await _mark_processed(order_id, event_type, customer_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("polar_token_or_completion_failed err=%s", exc)

    # Preserve the OAuth Connect account link introduced after the original repair.
    if email:
        try:
            from agent_interface.oauth.link import link_purchase
            await link_purchase(email, f"sub_{customer_id}", customer_id, plan)
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth_account_link_failed customer=%s err=%s", customer_id, type(exc).__name__)

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

def _explicit_customer_id(data: dict[str, Any]) -> str:
    customer = data.get("customer")
    value = customer.get("id") if isinstance(customer, dict) else None
    value = value or data.get("customer_id")
    order = data.get("order")
    if not value and isinstance(order, dict):
        customer = order.get("customer")
        value = (customer.get("id") if isinstance(customer, dict) else None) or order.get("customer_id")
    return value.strip() if isinstance(value, str) else ""


async def _scoped_refund(event_type: str, data: dict[str, Any]) -> None:
    from billing import polar_fulfillment as store
    from agent_interface.identity import remember_customer_revocation
    order_id, customer_id = _extract_order_id(data), _explicit_customer_id(data)
    if event_type in {"refund.created", "refund.updated"}:
        order = data.get("order")
        order_id = (order.get("id") if isinstance(order, dict) else None) or data.get("order_id") or ""
    if not order_id or not customer_id:
        raise PaidOrderFulfillmentError()
    try:
        await store.refund(order_id=order_id, customer_id=customer_id)
    except Exception:
        raise PaidOrderFulfillmentError() from None
    remember_customer_revocation(customer_id)


async def _handle_credit_event(event_type: str, data: dict[str, Any]) -> None:
    from billing import polar_fulfillment as store
    from billing.packages import credits_for_product
    from agent_interface.identity import issue_subscription_token
    from agent_interface.oauth.tokens import email_hash
    import uuid

    # Credit packages are fulfilled from paid Order events, never subscription ids.
    if event_type not in {"order.paid", "order.created"}:
        return
    if not _paid_order(event_type, data):
        return
    order_id, customer_id = _extract_order_id(data), _explicit_customer_id(data)
    email, plan = _extract_email(data), _extract_plan(data)
    product = data.get("product")
    if (not order_id or not customer_id or not email or "@" not in email
            or not isinstance(product, dict) or not isinstance(product.get("id"), str)
            or not product["id"].strip()):
        raise PaidOrderFulfillmentError()
    try:
        metadata = product.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError()
        credits = credits_for_product(product_name=product.get("name") or "",
                                      product_id=product["id"], product_metadata=metadata)
        if type(credits) is not int or credits <= 0:
            raise ValueError()
    except Exception:
        raise PaidOrderFulfillmentError() from None

    owner = str(uuid.uuid4())
    claim = None
    try:
        claim = await store.claim(order_id=order_id, customer_id=customer_id,
                                  account_id=f"sub_{customer_id}", product_id=product["id"],
                                  credits=credits, plan=plan, email_hash=email_hash(email), owner=owner)
        if claim["status"] == "refunded":
            return
        if claim["status"] not in {"claimed", "complete"}:
            raise PaidOrderFulfillmentError()
        token = issue_subscription_token(customer_id, plan, email,
                                         issued_at=claim["issued_at"], token_id=claim["token_id"],
                                         issuance_version=claim["issuance_version"])
        if not isinstance(token.token, str) or not token.token.strip():
            raise PaidOrderFulfillmentError()
        if claim["status"] == "claimed":
            completion = await store.complete(order_id=order_id, owner=owner, fence=claim["fence"])
            if completion["status"] == "refunded":
                return
            if (completion["status"] != "complete" or completion["issued_at"] != claim["issued_at"]
                    or completion["token_id"] != claim["token_id"]
                    or completion["issuance_version"] != claim["issuance_version"]
                    or completion.get("entitlements") != claim.get("entitlements")):
                raise PaidOrderFulfillmentError()
    except Exception:
        if claim and claim.get("status") == "claimed":
            try:
                await store.release(order_id=order_id, owner=owner, fence=claim["fence"])
            except Exception:
                pass  # A durable lease/tombstone governs the next delivery; never guess it was released.
        raise PaidOrderFulfillmentError() from None

    # Entitlement and OAuth linkage are committed before delivery. A failed send
    # remains retryable and replays use the SAME identity. Provider retries are
    # bounded; there is no durable mail queue or guaranteed email delivery.
    from billing.telegram_revenue_alerts import send_api_key_email
    from billing.emails import send_welcome_email
    try:
        if await send_welcome_email(email=email, credits=credits, api_key=token.token, order_id=order_id) is not True:
            raise PaidOrderFulfillmentError()
        if await send_api_key_email(email, plan, token.token, token.expires_at) is not True:
            raise PaidOrderFulfillmentError()
    except Exception:
        raise PaidOrderFulfillmentError() from None
    logger.info("polar_fulfillment_complete order=%s", order_id)


async def handle_polar_event(event: dict[str, Any]) -> None:
    """Acknowledge paid fulfillment only after its durable transaction completes."""
    from billing import switches
    event_type = event.get("type") or event.get("event_type") or ""
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    durable = bool(switches.credits_enabled() or os.getenv("POLAR_FULFILLMENT_KEY")
                   or os.getenv("SUPABASE_URL"))
    if event_type in _REVOKE_EVENTS:
        if not _terminal_revocation(event_type, data):
            return
        if durable:
            await _scoped_refund(event_type, data)
        else:
            await _handle_revoke_event(event_type, data)
        return
    # A signed historical paid order still needs fulfillment when offers and
    # metering are disabled. A configured backend must fail closed if its
    # private scoped credential is missing; legacy fallback is offline only.
    if durable:
        if event_type in _GRANT_EVENTS:
            await _handle_credit_event(event_type, data)
        return
    await _handle_legacy_event(event)
