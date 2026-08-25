"""WhatsApp channel adapter + webhook — honesty + enforcement tests."""
import asyncio

import pytest

from channels.adapter_interface import ChannelRequest
from channels.whatsapp.cloud_api import WhatsAppCloudAdapter


def _req(recipient="+96894639405", mtype="transactional", content="Your booking is confirmed."):
    return ChannelRequest(
        recipient_id=recipient, channel="whatsapp", message_type=mtype,
        content=content, country_code="OM",
    )


def test_not_configured_fails_honestly(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_ID", raising=False)
    a = WhatsAppCloudAdapter()
    assert a.is_available is False
    r = asyncio.run(a.send(_req()))
    assert r.success is False
    assert r.error_code == "not_configured"


def test_opted_out_recipient_is_blocked(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "x")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "y")
    from compliance.consent_store import get_consent_store
    from core.models import ComplianceViolationError
    get_consent_store().mark_opted_out("+96800001111", "whatsapp")
    a = WhatsAppCloudAdapter()
    with pytest.raises(ComplianceViolationError) as e:
        asyncio.run(a.send(_req(recipient="+96800001111")))
    assert e.value.rule == "recipient_opted_out"


def test_webhook_verify_handshake(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "sekrit")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_interface.whatsapp_webhook import router
    app = FastAPI(); app.include_router(router)
    c = TestClient(app)
    ok = c.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "sekrit", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = c.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_webhook_stop_registers_optout(monkeypatch):
    async def _no_insert(*a, **k):
        return None
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _no_insert)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_interface.whatsapp_webhook import router
    from compliance.consent_store import get_consent_store
    app = FastAPI(); app.include_router(router)
    c = TestClient(app)
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "96855550000", "profile": {"name": "T"}}],
        "messages": [{"id": "wamid.x", "from": "96855550000", "type": "text",
                      "text": {"body": "STOP"}}],
    }}]}]}
    r = c.post("/webhooks/whatsapp", json=payload)
    assert r.status_code == 200
    assert get_consent_store().is_opted_out("96855550000", "whatsapp") is True


def test_whatsapp_channel_cost_is_zero():
    """Founder 2026-08-26: costs us nothing -> free trial. Receipt cost must be 0."""
    from core.send_message import _CHANNEL_COSTS
    assert _CHANNEL_COSTS["whatsapp:cloud_api"] == 0.00


def test_metered_whatsapp_send_charges_zero_credits(monkeypatch):
    """A successful whatsapp-channel send through the credits rail commits 0."""
    import billing.credits as credits
    committed = {}

    async def fake_reserve(account_id, amount, hold_id, operation, operation_id=None):
        return {"ok": True, "balance_after": 1000}

    async def fake_commit(hold_id, actual):
        committed["actual"] = actual
        return {"balance_after": 1000 - actual}

    async def fake_release(hold_id, reason=""):
        return {"balance_after": 1000}

    monkeypatch.setattr(credits, "reserve", fake_reserve)
    monkeypatch.setattr(credits, "commit", fake_commit)
    monkeypatch.setattr(credits, "release", fake_release)

    async def dispatch():
        return {"status": "success", "reason_code": "delivered",
                "channel_used": "whatsapp:cloud_api",
                "cost": {"amount": 0.00, "currency": "USD", "basis": "per_message"}}

    receipt = asyncio.run(credits.run_metered_tool("send_message", "acct_x", dispatch))
    assert committed.get("actual") == 0          # zero credits committed
    assert receipt["credits"]["charged"] == 0    # receipt says charged 0
