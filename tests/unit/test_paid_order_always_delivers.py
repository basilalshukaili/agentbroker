"""A customer who pays must never end up with an API key and no credits.

THE BUG THIS LOCKS OUT. `handle_polar_event` granted credits inside a broad
try/except. When the grant threw, the handler logged a warning and CARRIED ON:
it issued the API key, emailed the customer a welcome, fired the revenue
alert, and the route returned 200 - which this webhook does deliberately so
Polar does not retry.

So the customer was charged, received a key that worked, had zero credits, and
nothing anywhere retried or surfaced it. Every individual step "succeeded".

The grant is idempotent on order_id, so retrying is always safe. What must
never happen again is a failure that ends in silence.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import billing.polar_webhook as pw  # noqa: E402


def test_a_failing_grant_is_retried_not_abandoned(monkeypatch):
    """A transaction outage returns retryably; provider redelivery reaches it again."""
    receipt = dict(ok=True,status="claimed",issued_at=1791000000.0,token_id="a"*32,
                   fence=1,issuance_version=1,entitlements={})
    monkeypatch.setenv("CREDITS_ENABLED","true")
    monkeypatch.setattr("billing.polar_fulfillment.claim",AsyncMock(return_value=receipt))
    complete = AsyncMock(side_effect=[RuntimeError("synthetic store outage"),
                                     {**receipt,"status":"complete"}])
    monkeypatch.setattr("billing.polar_fulfillment.complete",complete)
    monkeypatch.setattr("billing.polar_fulfillment.release",AsyncMock())
    monkeypatch.setattr("agent_interface.identity.issue_subscription_token",
                        Mock(return_value=SimpleNamespace(token="synthetic.token",expires_at=1900000000)))
    welcome, key_email = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr("billing.emails.send_welcome_email",welcome)
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_api_key_email",key_email)
    event = {"type":"order.paid","data":{"id":"offline-order","customer":{"id":"offline-customer",
             "email":"buyer@example.invalid"},"product":{"id":"offline-product","name":"Growth"}}}
    with pytest.raises(pw.PaidOrderFulfillmentError):
        asyncio.run(pw.handle_polar_event(event))
    welcome.assert_not_awaited()
    key_email.assert_not_awaited()
    asyncio.run(pw.handle_polar_event(event))
    assert complete.await_count == 2
    welcome.assert_awaited_once()
    key_email.assert_awaited_once()
