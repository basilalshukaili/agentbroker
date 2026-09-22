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
import asyncio
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

# Events that mean "money came back, revoke access". Per Polar's webhook docs
# (https://polar.sh/docs/integrate/webhooks/events): `order.refunded` fires on
# the Order resource, `refund.created` fires on the Refund resource (belt and
# suspenders -- handle whichever Polar actually sends), `subscription.revoked`
# fires when a subscription's access is pulled (immediately, or at the end of
# a `subscription.canceled` period -- we only act on the terminal `.revoked`).
_REVOKE_EVENTS = {"order.refunded", "refund.created", "subscription.revoked"}

# Durable dedup/revocation ledger. Same Supabase project + REST wrapper
# (storage/supabase_client.py) that billing/durable_meter.py already writes
# `billing_events` to -- this is a second table in that project, not new
# infra. Expected columns: order_id, event_type, customer_id, status
# ("processed" | "revoked"), ts. Duplicate lookups remain best-effort so a
# failed lookup does not prevent an idempotent grant from being retried.
# Credits-disabled completion writes retain the legacy best-effort behavior;
# credits-enabled completion requires a confirmed durable write and raises
# on failure so the provider can retry.
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


async def _mark_processed(order_id: str, event_type: str, customer_id: str,
                          *, strict: bool = False) -> None:
    """Record completed fulfillment; credits mode requires a confirmed write.

    The legacy credits-disabled path retains its best-effort behavior. A paid
    grant can be replayed safely with its order-id key if this write fails.
    """
    if not order_id:
        if strict:
            raise PaidOrderFulfillmentError()
        return
    try:
        from storage.supabase_client import insert_row, insert_row_strict
        row = {
            "order_id": order_id,
            "event_type": event_type,
            "customer_id": customer_id,
            "status": "processed",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if strict:
            written = await insert_row_strict(_POLAR_EVENTS_TABLE, row)
            if not isinstance(written, dict) or any(
                written.get(key) != row[key]
                for key in ("order_id", "customer_id", "status")
            ):
                raise PaidOrderFulfillmentError()
        else:
            await insert_row(_POLAR_EVENTS_TABLE, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("polar_mark_processed_failed order_id=%s err=%s", order_id, exc)
        if strict:
            raise PaidOrderFulfillmentError() from None


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


async def _record_ungranted_order(order_id: str, account_id: str, credits: int,
                                  email: str, error: str) -> None:
    """A paid order whose credit delivery was not confirmed. Make it recoverable and loud.

    Three places, because the reason the grant failed is usually that one of
    them is the thing that is down:
      * the durable table, so a human or a sweeper can replay it;
      * the log, at ERROR;
      * Telegram, because a customer who paid and got nothing will not wait
        for someone to read a log.

    The order id is included everywhere: it is the idempotency key, so
    replaying the grant with it cannot double-credit.
    """
    try:
        # STRICT. insert_row is documented "never raises" - it returns None on
        # any failure - so the handler below was dead code, and the ERROR line
        # it guards could not fire. That matters more here than almost
        # anywhere: the usual reason a grant fails is that Supabase is down,
        # which is exactly when this recovery write fails too. A paid order
        # would vanish with no durable record AND no log saying so.
        from storage.supabase_client import insert_row_strict
        await insert_row_strict("ungranted_orders", {
            "order_id": order_id,
            "account_id": account_id,
            "credits": credits,
            "email": email,
            "error": error,
        })
    except Exception as exc:                    # noqa: BLE001
        logger.error(
            "ungranted_order_not_recorded order=%s account=%s credits=%d "
            "email=%s err=%s -- THIS ORDER IS NOW ONLY IN THIS LOG LINE AND "
            "THE TELEGRAM ALERT BELOW. Replay with idempotency_key=%s",
            order_id, account_id, credits, email, exc, order_id)
    try:
        from billing.telegram_revenue_alerts import send_telegram_alert
        await send_telegram_alert(
            f"PAID ORDER DID NOT DELIVER\n\n"
            f"order: {order_id}\n"
            f"account: {account_id}\n"
            f"credits owed: {credits}\n"
            f"error: {error[:160]}\n\n"
            f"The customer has been charged, but the credit grant was not "
            f"confirmed and key delivery is incomplete. "
            f"Replay with idempotency_key={order_id} - it cannot "
            f"double-credit.")
    except Exception:                           # noqa: BLE001
        pass


async def handle_polar_event(event: dict[str, Any]) -> None:
    """Dispatch a verified Polar event.

    Credits-enabled fulfillment fails retryably until grant, token mint and
    completion persistence succeed. The HTTP route propagates that failure
    as non-2xx so provider redelivery can finish the idempotent grant.
    """
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
    if event_type == "order.created" and status and status not in ("paid", "succeeded", "completed"):
        logger.info("polar_order_not_paid status=%s — skipping grant", status)
        return

    order_id = _extract_order_id(data)
    if order_id and await _already_processed(order_id):
        logger.info("polar_event_duplicate_skipped type=%s order_id=%s", event_type, order_id)
        return

    email = _extract_email(data)
    plan = _extract_plan(data)
    customer_id = _extract_customer_id(data)
    credits_enabled = os.getenv("CREDITS_ENABLED", "").lower() in ("1", "true", "yes")
    pkg_credits = 0

    if credits_enabled:
        from billing.packages import credits_for_product
        from billing.credits import grant as credit_grant

        product = data.get("product")
        if not order_id or not isinstance(product, dict):
            logger.error("polar_fulfillment_metadata_missing order_id=%s", order_id)
            raise PaidOrderFulfillmentError()
        try:
            metadata = product.get("metadata") or {}
            if not isinstance(metadata, dict):
                raise ValueError("invalid product metadata")
            pkg_credits = credits_for_product(
                product_name=product.get("name") or "",
                product_id=product.get("id") or "",
                product_metadata=metadata,
            )
            if type(pkg_credits) is not int or pkg_credits <= 0:
                raise ValueError("unknown credit package")
        except Exception:  # noqa: BLE001
            logger.error("polar_credit_package_unresolved order_id=%s", order_id)
            raise PaidOrderFulfillmentError() from None

        credit_account = f"sub_{customer_id}"
        last_error = None
        for attempt in range(3):
            try:
                result = await credit_grant(
                    account_id=credit_account, amount=pkg_credits, source="polar",
                    idempotency_key=order_id, order_id=order_id,
                )
                # Transport success is insufficient: the RPC must confirm the grant.
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise PaidOrderFulfillmentError()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("polar_credit_grant_attempt_failed attempt=%d order=%s err=%s",
                               attempt + 1, order_id, exc)
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        else:
            logger.error("POLAR_CREDIT_GRANT_UNRECOVERED account=%s credits=%d order_id=%s",
                         credit_account, pkg_credits, order_id)
            await _record_ungranted_order(
                order_id=order_id, account_id=credit_account, credits=pkg_credits,
                email=email or "", error=str(last_error)[:300],
            )
            raise PaidOrderFulfillmentError() from None

    token_value: str | None = None
    token_suffix = "????????????"
    expires_iso = "?"
    try:
        from agent_interface.identity import issue_subscription_token
        token_resp = issue_subscription_token(
            customer_id=customer_id, plan=plan, customer_email=email or "",
        )
        token_value = token_resp.token
        if credits_enabled and (not isinstance(token_value, str) or not token_value.strip()):
            raise PaidOrderFulfillmentError()
        token_suffix = token_value[-12:] if len(token_value) >= 12 else token_value
        expires_iso = datetime.fromtimestamp(token_resp.expires_at, tz=timezone.utc).isoformat(timespec="seconds")
        logger.info("polar_token_issued customer=%s plan=%s exp=%s", customer_id, plan, expires_iso)
        # Grant and mint must both complete before suppressing further delivery.
        await _mark_processed(order_id, event_type, customer_id, strict=credits_enabled)
    except Exception as exc:  # noqa: BLE001
        logger.exception("polar_token_or_completion_failed err=%s", exc)
        if credits_enabled:
            raise PaidOrderFulfillmentError() from None

    if credits_enabled and email and token_value:
        try:
            from billing.emails import send_welcome_email
            raw_amount = data.get("amount") or data.get("total_amount")
            amount_usd = float(raw_amount) / 100 if isinstance(raw_amount, (int, float)) else None
            asyncio.create_task(send_welcome_email(
                email=email, credits=pkg_credits, api_key=token_value,
                order_id=order_id, amount_usd=amount_usd,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("polar_welcome_email_failed customer=%s err=%s", customer_id, exc)

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
