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
# ("processed" | "revoked"), ts. Like every other durable write in this
# codebase, both read and write are best-effort: if the table doesn't exist
# yet or Supabase is unreachable, checks fail OPEN (never block a real grant)
# and writes are logged-and-swallowed -- identical fallback behavior to
# today's code, just durable when the store is available.
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


async def _mark_processed(order_id: str, event_type: str, customer_id: str) -> None:
    """Durably record that `order_id` has been granted, so a re-delivered
    event (retry, or the duplicate-webhook-endpoint scenario) no-ops next
    time. Best-effort -- never raises, matches durable_meter's fire-and-forget
    write pattern."""
    if not order_id:
        return
    try:
        from storage.supabase_client import insert_row
        await insert_row(_POLAR_EVENTS_TABLE, {
            "order_id": order_id,
            "event_type": event_type,
            "customer_id": customer_id,
            "status": "processed",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
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
        await revoke_customer(customer_id=customer_id, order_id=order_id, reason=event_type)
    except Exception as e:  # noqa: BLE001
        logger.exception("polar_revoke_failed customer=%s err=%s", customer_id, e)


async def _record_ungranted_order(order_id: str, account_id: str, credits: int,
                                  email: str, error: str) -> None:
    """A paid order whose credits did not land. Make it recoverable and loud.

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
        from storage.supabase_client import insert_row
        await insert_row("ungranted_orders", {
            "order_id": order_id,
            "account_id": account_id,
            "credits": credits,
            "email": email,
            "error": error,
        })
    except Exception as exc:                    # noqa: BLE001
        logger.error("ungranted_order_not_recorded order=%s err=%s", order_id, exc)
    try:
        from billing.telegram_revenue_alerts import send_telegram_alert
        await send_telegram_alert(
            f"PAID ORDER DID NOT DELIVER\n\n"
            f"order: {order_id}\n"
            f"account: {account_id}\n"
            f"credits owed: {credits}\n"
            f"error: {error[:160]}\n\n"
            f"The customer has been charged and has an API key with NO "
            f"credits. Replay with idempotency_key={order_id} - it cannot "
            f"double-credit.")
    except Exception:                           # noqa: BLE001
        pass


async def handle_polar_event(event: dict[str, Any]) -> None:
    """Dispatch a verified Polar event. On a paid order/subscription, mint an
    Agent-Identity token, email it, and fire the Telegram revenue alert. Never
    raises — the caller always returns 200 after a valid signature."""
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

    # An order for a $0 / unpaid status shouldn't grant. Polar order.paid implies
    # paid; guard order.created on a paid flag if present.
    status = str(data.get("status") or "").lower()
    if event_type == "order.created" and status and status not in ("paid", "succeeded", "completed"):
        logger.info("polar_order_not_paid status=%s — skipping grant", status)
        return

    # Idempotency guard: re-delivery of an already-granted order (Polar retry,
    # or the two-webhook-endpoints-pointing-at-us scenario) must no-op rather
    # than double-mint a token and double-send the welcome email. Keyed on
    # order id (not the webhook delivery id) because that's what's actually
    # invariant across duplicate deliveries/endpoints for the same purchase.
    order_id = _extract_order_id(data)
    if order_id and await _already_processed(order_id):
        logger.info(
            "polar_event_duplicate_skipped type=%s order_id=%s — already processed, no-op",
            event_type, order_id,
        )
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
        # Mark processed only on a *successful* mint -- a failed issuance must
        # stay eligible for the next retry/redelivery to actually grant access.
        await _mark_processed(order_id, event_type, customer_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("polar_token_issue_failed err=%s", e)

    # --- SLICE 4: Credit grant on purchase ---
    # Maps the purchased Polar product -> credits and grants them to the
    # sub_{customer_id} credit account. Idempotent: keyed on order_id so a
    # re-delivered webhook never double-grants. Reuses the same
    # polar_order_events idempotency already checked above.
    # The account_id convention matches resolve_account (agent_id from JWT):
    # issue_subscription_token sets agent_id = f"sub_{customer_id}".
    try:
        import os as _os_c
        if _os_c.getenv("CREDITS_ENABLED", "").lower() in ("1", "true", "yes"):
            from billing.packages import credits_for_product
            from billing.credits import grant as _credit_grant

            product_obj = data.get("product") or {}
            product_name = (product_obj.get("name") if isinstance(product_obj, dict) else "") or ""
            product_id = (product_obj.get("id") if isinstance(product_obj, dict) else "") or ""
            product_meta = (product_obj.get("metadata") if isinstance(product_obj, dict) else None) or {}

            pkg_credits = credits_for_product(
                product_name=product_name,
                product_id=product_id,
                product_metadata=product_meta,
            )
            if pkg_credits > 0 and order_id:
                credit_account = f"sub_{customer_id}"
                # A FAILED GRANT USED TO BE A LOG LINE AND NOTHING ELSE.
                #
                # The outer `except` caught it, execution carried on to email
                # the customer their API key and fire the revenue alert, and
                # the route returned 200 - which this webhook does on purpose
                # so Polar does not retry. Net effect: the customer paid, got
                # a key, got a welcome email, and had ZERO credits for ever.
                # Nothing retried it and nothing surfaced it.
                #
                # The grant is idempotent on order_id, so retrying is safe.
                # After the retries, an unfixed grant is escalated rather than
                # logged: a paid order that did not deliver is not an
                # operational detail, it is somebody's money.
                _granted, _last_err = False, None
                for _attempt in range(3):
                    try:
                        await _credit_grant(
                            account_id=credit_account,
                            amount=pkg_credits,
                            source="polar",
                            idempotency_key=order_id,
                            order_id=order_id,
                        )
                        _granted = True
                        break
                    except Exception as _ge:    # noqa: BLE001
                        _last_err = _ge
                        logger.warning(
                            "polar_credit_grant_attempt_failed attempt=%d "
                            "order=%s err=%s", _attempt + 1, order_id, _ge)
                        if _attempt < 2:
                            await asyncio.sleep(1.5 * (_attempt + 1))

                if _granted:
                    logger.info(
                        "polar_credit_grant_applied account=%s credits=%d order_id=%s",
                        credit_account, pkg_credits, order_id,
                    )
                else:
                    logger.error(
                        "POLAR_CREDIT_GRANT_UNRECOVERED account=%s credits=%d "
                        "order_id=%s err=%s -- CUSTOMER PAID AND HAS NO CREDITS",
                        credit_account, pkg_credits, order_id, _last_err)
                    await _record_ungranted_order(
                        order_id=order_id, account_id=credit_account,
                        credits=pkg_credits, email=email or "",
                        error=str(_last_err)[:300])
            elif pkg_credits <= 0:
                logger.warning(
                    "polar_credit_grant_skipped: no credits resolved for "
                    "product_name=%r product_id=%r order_id=%s",
                    product_name, product_id, order_id,
                )

            # SLICE 6: Send WELCOME email with credits, API key, and quickstart.
            # Only sent when we have an email and a token was successfully issued.
            if email and token_value and pkg_credits > 0:
                try:
                    from billing.emails import send_welcome_email as _welcome
                    # Derive USD amount from the order's amount field (cents -> USD).
                    raw_amount = data.get("amount") or data.get("total_amount")
                    amount_usd_val: float | None = None
                    if isinstance(raw_amount, (int, float)):
                        amount_usd_val = float(raw_amount) / 100
                    import asyncio as _asyncio
                    _asyncio.create_task(_welcome(
                        email=email,
                        credits=pkg_credits,
                        api_key=token_value,
                        order_id=order_id or None,
                        amount_usd=amount_usd_val,
                    ))
                except Exception as _we:  # noqa: BLE001
                    logger.warning("polar_welcome_email_failed customer=%s err=%s", customer_id, _we)

    except Exception as e:  # noqa: BLE001
        logger.warning("polar_credit_grant_failed customer=%s order=%s err=%s", customer_id, order_id, e)
    # --- END SLICE 4 ---

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
