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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import billing.polar_webhook as pw  # noqa: E402


def test_a_failing_grant_is_retried_not_abandoned(monkeypatch):
    """Drive the REAL webhook path with a real order event.

    My first version of this test called a stub driver that did nothing, so
    the assertion passed against zero attempts - a test that passes for the
    wrong reason, which is the exact defect the rest of this file exists to
    prevent. It now sends an actual order.paid event through
    handle_polar_event.
    """
    attempts = {"n": 0}

    async def flaky(**kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("supabase timeout")
        return None

    async def noop(*a, **k):
        return None

    monkeypatch.setenv("CREDITS_ENABLED", "true")
    monkeypatch.setattr("billing.credits.grant", flaky, raising=False)
    monkeypatch.setattr("billing.packages.credits_for_product",
                        lambda *a, **k: 5000, raising=False)
    # Everything downstream of the grant is I/O we do not want in a unit test.
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_telegram_alert",
                        noop, raising=False)
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_api_key_email",
                        noop, raising=False)

    event = {
        "type": "order.paid",
        "data": {
            "id": "ord_retry_1",
            "status": "paid",
            "customer_id": "cus_retry_1",
            "customer": {"email": "buyer@example.com"},
            "product": {"id": "prod_x", "name": "Growth"},
            "amount": 4900,
        },
    }
    asyncio.run(pw.handle_polar_event(event))

    assert attempts["n"] >= 2, (
        f"the grant was tried {attempts['n']} time(s) - a transient failure "
        f"must be retried, not abandoned with the customer already keyed")


def test_an_unrecoverable_grant_is_recorded_and_escalated(monkeypatch):
    """The important one: after the retries, it must be recoverable and LOUD."""
    recorded, alerted = {}, {}

    async def always_fails(**kw):
        raise RuntimeError("supabase down")

    async def fake_insert(table, row):
        recorded["table"] = table
        recorded["row"] = row
        return {"ok": True}

    async def fake_alert(text):
        alerted["text"] = text
        return True

    # STRICT, matching the call site. _record_ungranted_order used the
    # lenient insert_row, which never raises - so its own except-handler,
    # and the ERROR log inside it, were unreachable. Patching the lenient
    # one here would now silently test nothing.
    monkeypatch.setattr("storage.supabase_client.insert_row_strict",
                        fake_insert)
    monkeypatch.setattr(
        "billing.telegram_revenue_alerts.send_telegram_alert", fake_alert)

    asyncio.run(pw._record_ungranted_order(
        order_id="ord_123", account_id="sub_cus_9", credits=5000,
        email="a@b.co", error="supabase down"))

    assert recorded.get("table") == "ungranted_orders", (
        "a paid order that did not deliver was not recorded anywhere durable")
    assert recorded["row"]["order_id"] == "ord_123"
    assert recorded["row"]["credits"] == 5000

    text = alerted.get("text", "")
    assert "ord_123" in text, "the alert does not name the order to replay"
    assert "idempotency_key" in text, (
        "the alert must say the replay is safe, or nobody will dare run it")


def test_a_failed_recovery_write_is_logged_and_still_escalates(monkeypatch,
                                                               caplog):
    """The handler that could not fire.

    _record_ungranted_order wrapped the lenient insert_row in
    `except Exception: logger.error(...)`. insert_row is documented "never
    raises" - it returns None on any failure - so that log line was dead code.
    It matters here more than almost anywhere: the usual reason a grant failed
    is that Supabase is down, which is exactly when this recovery write fails
    too. The paid order would vanish with no durable row AND nothing in the
    log saying so.
    """
    import logging
    alerted = {}

    async def _boom(table, row):
        raise RuntimeError("supabase down")

    async def fake_alert(text):
        alerted["text"] = text
        return True

    monkeypatch.setattr("storage.supabase_client.insert_row_strict", _boom)
    monkeypatch.setattr(
        "billing.telegram_revenue_alerts.send_telegram_alert", fake_alert)

    with caplog.at_level(logging.ERROR, logger="smb_broker.polar_webhook"):
        asyncio.run(pw._record_ungranted_order(
            order_id="ord_777", account_id="sub_cus_1", credits=1000,
            email="a@b.co", error="supabase down"))

    errors = [r.getMessage() for r in caplog.records
              if r.levelno >= logging.ERROR]
    assert any("ungranted_order_not_recorded" in m for m in errors), (
        "the durable write failed and nothing was logged at ERROR - this is "
        "the dead-handler bug, and a paid order just disappeared silently")
    assert any("ord_777" in m for m in errors), (
        "the log does not name the order, so it cannot be replayed from it")
    # And the customer-facing escalation must still happen.
    assert "ord_777" in alerted.get("text", "")
