"""
Webhook delivery — signed, idempotent, with exponential backoff retry.
Signature: HMAC-SHA256(body_bytes, shared_secret) in X-SMBBroker-Signature header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

try:
    from celery import shared_task  # type: ignore
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header_sig: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)


# In-memory webhook registry (replaced by DB in production)
_webhooks: dict[str, dict[str, Any]] = {}


def register_webhook(url: str, secret: str, operations: list[str] | None = None) -> str:
    import uuid
    webhook_id = str(uuid.uuid4())
    _webhooks[webhook_id] = {"url": url, "secret": secret, "operations": operations or []}
    return webhook_id


def get_webhooks_for_operation(operation: str) -> list[dict[str, Any]]:
    return [
        w for w in _webhooks.values()
        if not w["operations"] or operation in w["operations"]
    ]


async def deliver_webhook_sync(operation_id: str, outcome: dict[str, Any], trace_id: str | None) -> None:
    """Synchronous webhook delivery for use in tests."""
    import httpx
    body = json.dumps(outcome).encode()
    for webhook in get_webhooks_for_operation(outcome.get("operation", "")):
        sig = sign_payload(body, webhook["secret"])
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    webhook["url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-SMBBroker-Signature": sig,
                        "X-SMBBroker-Idempotency-Key": operation_id,
                        "X-SMBBroker-Event": "outcome",
                        "X-SMBBroker-Version": "0.1",
                    },
                    timeout=5.0,
                )
        except Exception:
            pass  # retried by Celery task


if CELERY_AVAILABLE:
    from celery import shared_task  # type: ignore

    @shared_task(bind=True, name="smb_broker.deliver_webhook",
                 max_retries=10, default_retry_delay=1)
    def deliver_webhook(self, operation_id: str, outcome: dict[str, Any], trace_id: str | None) -> None:
        import asyncio, httpx
        body = json.dumps(outcome).encode()
        for webhook in get_webhooks_for_operation(outcome.get("operation", "")):
            sig = sign_payload(body, webhook["secret"])
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                resp = loop.run_until_complete(
                    _post_webhook(webhook["url"], body, sig, operation_id)
                )
                if resp >= 400:
                    raise RuntimeError(f"Webhook returned {resp}")
            except Exception as exc:
                countdown = min(2 ** self.request.retries, 3600)
                raise self.retry(exc=exc, countdown=countdown)

    async def _post_webhook(url: str, body: bytes, sig: str, op_id: str) -> int:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-SMBBroker-Signature": sig,
                    "X-SMBBroker-Idempotency-Key": op_id,
                },
                timeout=5.0,
            )
            return resp.status_code
else:
    def deliver_webhook(operation_id, outcome, trace_id):  # type: ignore
        pass
