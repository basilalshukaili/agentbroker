"""Polar webhook signature verification — proven against the official
Standard Webhooks test vector (https://www.standardwebhooks.com), which Polar
implements. This is payment-critical: a wrong verifier either rejects real
payments or accepts forged ones."""
import asyncio
from unittest.mock import AsyncMock, patch

from billing.polar_webhook import (
    verify_polar_signature, _extract_plan, _extract_email, _extract_order_id,
    handle_polar_event,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

# Official Standard Webhooks test vector.
_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
_BODY = b'{"test": 2432232314}'
_HEADERS = {
    "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
    "webhook-timestamp": "1614265330",
    "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
}


def test_valid_signature_passes():
    assert verify_polar_signature(_BODY, _HEADERS, _SECRET, enforce_timestamp=False) is True


def test_tampered_body_fails():
    assert verify_polar_signature(b'{"test": 9999}', _HEADERS, _SECRET, enforce_timestamp=False) is False


def test_wrong_secret_fails():
    assert verify_polar_signature(
        _BODY, _HEADERS, "whsec_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", enforce_timestamp=False
    ) is False


def test_missing_headers_fails():
    assert verify_polar_signature(_BODY, {}, _SECRET, enforce_timestamp=False) is False


def test_empty_secret_fails():
    assert verify_polar_signature(_BODY, _HEADERS, "", enforce_timestamp=False) is False


def test_stale_timestamp_rejected_when_enforced():
    # The vector's timestamp is from 2021 — enforcing freshness must reject it.
    assert verify_polar_signature(_BODY, _HEADERS, _SECRET, enforce_timestamp=True) is False


def test_plan_extraction_from_metadata():
    assert _extract_plan({"metadata": {"plan": "Business"}}) == "business"
    assert _extract_plan({"product": {"name": "Agent Broker Enterprise"}}) == "enterprise"
    assert _extract_plan({}) == "developer"  # safe default


def test_email_extraction():
    assert _extract_email({"customer": {"email": "dev@example.com"}}) == "dev@example.com"
    assert _extract_email({"customer_email": "x@y.com"}) == "x@y.com"
    assert _extract_email({}) is None


def test_order_id_extraction():
    # order.paid / order.created / order.refunded: data IS the Order object.
    assert _extract_order_id({"id": "order_abc123"}) == "order_abc123"
    # refund.created / refund.updated: data is a Refund object nesting its order.
    assert _extract_order_id({"id": "refund_xyz", "order": {"id": "order_abc123"}}) == "order_abc123"
    assert _extract_order_id({"id": "refund_xyz", "order_id": "order_abc123"}) == "order_abc123"
    # Nothing usable -> "" (caller must treat as "cannot dedupe", not a match).
    assert _extract_order_id({}) == ""


# ---------------------------------------------------------------------------
# handle_polar_event — idempotency guard (AUDIT-2026-08-16 finding #1: a
# re-delivered order.paid/order.created must not double-mint a token or
# double-send the welcome email).
# ---------------------------------------------------------------------------

def _fake_durable_store():
    """A tiny stateful fake of storage.supabase_client's insert_row/select_rows,
    backed by a plain list so two sequential handle_polar_event() calls see
    each other's writes -- exactly what a real Supabase table would provide."""
    rows: list[dict] = []

    async def fake_select_rows(table, filters=None, limit=1000):
        filters = filters or {}
        return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]

    async def fake_insert_row(table, row):
        rows.append(row)
        return row

    return rows, fake_select_rows, fake_insert_row


class TestPolarWebhookIdempotency:
    def test_duplicate_grant_event_is_noop(self):
        _rows, fake_select_rows, fake_insert_row = _fake_durable_store()
        event = {
            "type": "order.paid",
            "data": {
                "id": "order_dup_test_1",
                "customer": {"id": "cust_dup_1", "email": "buyer@example.com"},
                "amount": 900,
                "currency": "usd",
            },
        }

        with patch("storage.supabase_client.select_rows", side_effect=fake_select_rows), \
             patch("storage.supabase_client.insert_row", side_effect=fake_insert_row), \
             patch("agent_interface.identity.issue_subscription_token") as mock_issue, \
             patch("billing.telegram_revenue_alerts.send_api_key_email", new_callable=AsyncMock) as mock_email, \
             patch("billing.telegram_revenue_alerts.send_telegram_alert", new_callable=AsyncMock) as mock_alert:
            from agent_interface.identity import TokenResponse
            import time as _time
            mock_issue.return_value = TokenResponse(
                token="fake.tokenvalue1234", agent_id="sub_cust_dup_1",
                expires_at=_time.time() + 90 * 86400, issued_at=_time.time(),
            )

            run(handle_polar_event(event))
            run(handle_polar_event(event))  # simulated re-delivery of the same order

            assert mock_issue.call_count == 1, "token must be minted exactly once"
            assert mock_email.call_count == 1, "welcome email must be sent exactly once"
            assert mock_alert.call_count == 1, "revenue alert must fire exactly once"

    def test_different_orders_both_process(self):
        """Sanity check the guard isn't over-broad: two DIFFERENT orders must
        both grant."""
        _rows, fake_select_rows, fake_insert_row = _fake_durable_store()

        def make_event(order_id):
            return {
                "type": "order.paid",
                "data": {
                    "id": order_id,
                    "customer": {"id": f"cust_{order_id}", "email": "buyer@example.com"},
                },
            }

        with patch("storage.supabase_client.select_rows", side_effect=fake_select_rows), \
             patch("storage.supabase_client.insert_row", side_effect=fake_insert_row), \
             patch("agent_interface.identity.issue_subscription_token") as mock_issue, \
             patch("billing.telegram_revenue_alerts.send_api_key_email", new_callable=AsyncMock), \
             patch("billing.telegram_revenue_alerts.send_telegram_alert", new_callable=AsyncMock):
            from agent_interface.identity import TokenResponse
            import time as _time
            mock_issue.return_value = TokenResponse(
                token="fake.tokenvalue1234", agent_id="sub_x",
                expires_at=_time.time() + 90 * 86400, issued_at=_time.time(),
            )

            run(handle_polar_event(make_event("order_A")))
            run(handle_polar_event(make_event("order_B")))

            assert mock_issue.call_count == 2

    def test_duplicate_check_fails_open_when_store_unreachable(self):
        """If the durable store errors on lookup, we must NOT block a real
        grant -- fail open, exactly like every other Supabase read in this
        codebase (never let an optional durable layer break the primary
        path)."""
        async def broken_select_rows(table, filters=None, limit=1000):
            raise RuntimeError("supabase unreachable")

        with patch("storage.supabase_client.select_rows", side_effect=broken_select_rows), \
             patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None), \
             patch("agent_interface.identity.issue_subscription_token") as mock_issue, \
             patch("billing.telegram_revenue_alerts.send_api_key_email", new_callable=AsyncMock), \
             patch("billing.telegram_revenue_alerts.send_telegram_alert", new_callable=AsyncMock):
            from agent_interface.identity import TokenResponse
            import time as _time
            mock_issue.return_value = TokenResponse(
                token="fake.tokenvalue1234", agent_id="sub_x",
                expires_at=_time.time() + 90 * 86400, issued_at=_time.time(),
            )
            run(handle_polar_event({
                "type": "order.paid",
                "data": {"id": "order_store_down", "customer": {"id": "cust_x", "email": "b@e.com"}},
            }))
            assert mock_issue.call_count == 1


# ---------------------------------------------------------------------------
# handle_polar_event — refund/revocation (AUDIT-2026-08-16 finding: a
# refunded order's token kept working until natural 90-day expiry).
# ---------------------------------------------------------------------------

class TestPolarWebhookRefundRevokes:
    def test_order_refunded_revokes_customer(self):
        from agent_interface.identity import is_customer_revoked

        event = {
            "type": "order.refunded",
            "data": {
                "id": "order_refund_test_1",
                "customer": {"id": "cust_refund_test_1", "email": "buyer@example.com"},
            },
        }
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(handle_polar_event(event))

        assert is_customer_revoked("cust_refund_test_1") is True

    def test_refund_created_also_revokes(self):
        """Belt-and-suspenders: handle whichever of order.refunded /
        refund.created Polar actually sends."""
        from agent_interface.identity import is_customer_revoked

        event = {
            "type": "refund.created",
            "data": {
                "id": "refund_xyz_1",
                "order": {"id": "order_refund_test_2"},
                "customer": {"id": "cust_refund_test_2", "email": "buyer2@example.com"},
            },
        }
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(handle_polar_event(event))

        assert is_customer_revoked("cust_refund_test_2") is True

    def test_subscription_revoked_revokes(self):
        from agent_interface.identity import is_customer_revoked

        event = {
            "type": "subscription.revoked",
            "data": {"id": "sub_xyz_1", "customer": {"id": "cust_sub_revoked_1"}},
        }
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(handle_polar_event(event))

        assert is_customer_revoked("cust_sub_revoked_1") is True

    def test_unrelated_customer_not_revoked(self):
        from agent_interface.identity import is_customer_revoked

        event = {
            "type": "order.refunded",
            "data": {"id": "order_refund_test_3", "customer": {"id": "cust_refund_test_3"}},
        }
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(handle_polar_event(event))

        assert is_customer_revoked("cust_totally_unrelated") is False
