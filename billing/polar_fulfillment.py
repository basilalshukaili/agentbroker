"""Strict, narrowly authorized durable Polar fulfillment RPCs.

The scoped JWT may execute only these four functions. Never fall back to the
public anon credential or the broad service-role key. Tokens and emails are
not sent to this store; it keeps immutable issuance fields and an email hash.
"""
from __future__ import annotations

import math
import os
import uuid

import httpx


class FulfillmentUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Paid order fulfillment store unavailable; retry later.")


async def _rpc(action: str, payload: dict) -> dict:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("POLAR_FULFILLMENT_KEY", "").strip()
    if not url or not key:
        raise FulfillmentUnavailable()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(f"{url}/rest/v1/rpc/polar_fulfillment_{action}",
                                         headers={"apikey": key, "Authorization": f"Bearer {key}"},
                                         json=payload)
        if response.status_code != 200:
            raise FulfillmentUnavailable()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise FulfillmentUnavailable()
        return result
    except Exception:
        # No provider/database payload, credentials or customer identifiers in errors.
        raise FulfillmentUnavailable() from None


def _issuance(result: dict) -> dict:
    status = result.get("status")
    if status not in {"claimed", "complete", "in_progress", "refunded", "conflict"}:
        raise FulfillmentUnavailable()
    if status in {"claimed", "complete"}:
        timestamp, token_id = result.get("issued_at"), result.get("token_id")
        try:
            if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or timestamp <= 0 or not isinstance(token_id, str)):
                raise ValueError()
            uuid.UUID(token_id)
            if type(result.get("issuance_version")) is not int or result["issuance_version"] != 1:
                raise ValueError()
            if status == "claimed" and (type(result.get("fence")) is not int or result["fence"] < 1):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise FulfillmentUnavailable() from None
    return result


async def claim(*, order_id: str, customer_id: str, account_id: str,
                product_id: str, credits: int, plan: str, email_hash: str, owner: str) -> dict:
    result = _issuance(await _rpc("claim", {
        "p_order_id": order_id, "p_customer_id": customer_id, "p_account_id": account_id,
        "p_product_id": product_id, "p_credits": credits, "p_plan": plan,
        "p_email_hash": email_hash, "p_owner": owner,
    }))
    if result["status"] in {"claimed", "complete"}:
        from agent_interface.identity import _FULFILLMENT_V1_SCOPES
        scopes = _FULFILLMENT_V1_SCOPES.get(plan)
        if not scopes or result.get("entitlements") != {
            "operations": scopes[0], "budget_cap_usd": scopes[1],
            "verticals": scopes[2], "ttl_seconds": scopes[3],
        }:
            raise FulfillmentUnavailable()
    return result


async def complete(*, order_id: str, owner: str, fence: int) -> dict:
    result = _issuance(await _rpc("complete", {
        "p_order_id": order_id, "p_owner": owner, "p_fence": fence,
    }))
    if result["status"] not in {"complete", "refunded"}:
        raise FulfillmentUnavailable()
    return result


async def release(*, order_id: str, owner: str, fence: int) -> None:
    result = await _rpc("release", {"p_order_id": order_id, "p_owner": owner, "p_fence": fence})
    if result.get("status") not in {"ready", "complete", "refunded", "in_progress"}:
        raise FulfillmentUnavailable()


async def refund(*, order_id: str, customer_id: str) -> None:
    result = await _rpc("refund", {"p_order_id": order_id, "p_customer_id": customer_id})
    if result.get("status") != "refunded":
        raise FulfillmentUnavailable()
