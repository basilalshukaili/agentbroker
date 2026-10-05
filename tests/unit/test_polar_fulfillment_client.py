"""The paid store uses its scoped key exclusively and refuses uncertain transport."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from billing import polar_fulfillment as store
from billing import polar_webhook as webhook

PAYLOAD = dict(order_id="offline-order", customer_id="offline-customer", account_id="sub_offline-customer",
               product_id="offline-product", credits=1000, plan="developer", email_hash="a" * 64, owner="b" * 32)
GOOD = dict(ok=True, status="claimed", issued_at=1791000000.0, token_id="a" * 32, fence=1, issuance_version=1,
            entitlements=dict(operations=["*"], budget_cap_usd=500, verticals=["*"], ttl_seconds=7776000))


def install(monkeypatch, status=200, response=None):
    seen = []
    monkeypatch.setenv("SUPABASE_URL", "https://spine.example.invalid")
    monkeypatch.setenv("POLAR_FULFILLMENT_KEY", "synthetic-scoped-key")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "synthetic-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "synthetic-service-key")
    def handle(request):
        seen.append(request)
        return httpx.Response(status, json=GOOD if response is None else response)
    client = httpx.AsyncClient
    monkeypatch.setattr(store.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    return seen


def test_claim_only_uses_scoped_authority_and_minimal_payload(monkeypatch):
    seen = install(monkeypatch)
    assert asyncio.run(store.claim(**PAYLOAD)) == GOOD
    request = seen[0]
    assert request.url.path == "/rest/v1/rpc/polar_fulfillment_claim"
    assert request.headers["authorization"] == "Bearer synthetic-scoped-key"
    assert request.headers["apikey"] == "synthetic-scoped-key"
    assert json.loads(request.content) == {"p_" + k: v for k, v in PAYLOAD.items()}
    assert b"synthetic-service-key" not in request.content


def test_missing_scoped_key_never_falls_back_or_connects(monkeypatch):
    install(monkeypatch)
    monkeypatch.delenv("POLAR_FULFILLMENT_KEY")
    client = Mock(side_effect=AssertionError("must not open transport"))
    monkeypatch.setattr(store.httpx, "AsyncClient", client)
    with pytest.raises(store.FulfillmentUnavailable):
        asyncio.run(store.claim(**PAYLOAD))
    client.assert_not_called()


@pytest.mark.parametrize("key", [None, ""])
@pytest.mark.parametrize("kind", ["order.paid", "order.refunded"])
def test_configured_backend_with_offers_off_never_falls_back_when_key_missing(monkeypatch, key, kind):
    install(monkeypatch)
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    if key is None:
        monkeypatch.delenv("POLAR_FULFILLMENT_KEY")
    else:
        monkeypatch.setenv("POLAR_FULFILLMENT_KEY", key)
    client = Mock(side_effect=AssertionError("must not open transport"))
    monkeypatch.setattr(store.httpx, "AsyncClient", client)
    legacy, marker, welcome, key_email, legacy_refund = (AsyncMock() for _ in range(5))
    monkeypatch.setattr(webhook, "_handle_legacy_event", legacy)
    monkeypatch.setattr(webhook, "_mark_processed", marker)
    monkeypatch.setattr(webhook, "_handle_revoke_event", legacy_refund)
    monkeypatch.setattr("billing.emails.send_welcome_email", welcome)
    monkeypatch.setattr("billing.telegram_revenue_alerts.send_api_key_email", key_email)
    event = {"type":kind,"data":{"id":"offline-order",
        "status":"refunded" if kind == "order.refunded" else "paid",
        "customer":{"id":"offline-customer","email":"buyer@example.invalid"},
        "product":{"id":"offline-product","name":"Growth"}}}
    with pytest.raises(webhook.PaidOrderFulfillmentError):
        asyncio.run(webhook.handle_polar_event(event))
    for action in (legacy, marker, welcome, key_email, legacy_refund):
        action.assert_not_awaited()
    client.assert_not_called()


@pytest.mark.parametrize("status,response", [(401, {}), (500, {"secret": "synthetic-private-detail"}),
                    (200, []), (200, {"ok": False}), (200, {"ok": 1}),
                    (200, {**GOOD, "status": "unknown"}), (200, {**GOOD, "issued_at": True}),
                    (200, {**GOOD, "token_id": "bad"}), (200, {**GOOD, "fence": 0})])
def test_uncertain_response_is_generic_retryable_failure(monkeypatch, status, response):
    install(monkeypatch, status, response)
    with pytest.raises(store.FulfillmentUnavailable) as caught:
        asyncio.run(store.claim(**PAYLOAD))
    assert str(caught.value) == "Paid order fulfillment store unavailable; retry later."


@pytest.mark.parametrize("action,status", [("complete", "complete"), ("complete", "refunded"),
                        ("release", "ready"), ("refund", "refunded")])
def test_each_action_uses_its_scoped_function(monkeypatch, action, status):
    response = {**GOOD, "status": status}
    seen = install(monkeypatch, response=response)
    kwargs = dict(order_id="offline-order", owner="b" * 32, fence=1)
    if action == "refund":
        kwargs = dict(order_id="offline-order", customer_id="offline-customer")
    asyncio.run(getattr(store, action)(**kwargs))
    assert seen[0].url.path.endswith("polar_fulfillment_" + action)
