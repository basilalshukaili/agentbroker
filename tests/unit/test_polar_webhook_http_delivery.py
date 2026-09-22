"""Verified but incomplete fulfillment must not acknowledge delivery to Polar."""
import asyncio
import base64
import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from billing import polar_webhook
from main import app


@pytest.mark.parametrize("complete", [True, False])
def test_http_acknowledgement_requires_completed_fulfillment(monkeypatch, complete):
    secret = "whsec_" + base64.b64encode(b"synthetic-webhook-test-secret-32!").decode()
    body = b'{"type":"order.paid","data":{"id":"offline-http-order"}}'
    timestamp = str(int(time.time()))
    message_id = "offline-http-message"
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(hmac.new(
        b"synthetic-webhook-test-secret-32!", signed, hashlib.sha256
    ).digest()).decode()
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", secret)
    handler = AsyncMock(side_effect=None if complete else
                        polar_webhook.PaidOrderFulfillmentError())
    monkeypatch.setattr(polar_webhook, "handle_polar_event", handler)

    async def request():
        # ASGI transport invokes the real route in-process without opening a socket
        # or starting the application's production lifespan tasks.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.invalid") as client:
            return await client.post("/webhooks/polar", content=body, headers={
                "webhook-id": message_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": "v1," + signature,
            })

    response = asyncio.run(request())
    handler.assert_awaited_once_with({
        "type": "order.paid", "data": {"id": "offline-http-order"},
    })
    assert response.status_code == (200 if complete else 500)
    if complete:
        assert response.json() == {"ok": True}
    else:
        assert "credit" not in response.text.lower()
