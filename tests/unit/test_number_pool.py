"""
Sender-number pool.

The pool's whole purpose is to make a (our_number, business_number) pair UNIQUE
so a bare "yes" is attributable. Its dangerous failure mode is subtle: if we
open the thread on number B but the message actually goes out from number A,
every reply correlates to the wrong pair — strictly worse than no pool. So the
send-from wiring is tested, not just the allocation arithmetic.
"""
from __future__ import annotations

import asyncio

import pytest

from core import number_pool as np


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("WHATSAPP_NUMBER_POOL", "WHATSAPP_PHONE_NUMBER", "WHATSAPP_PHONE_ID"):
        monkeypatch.delenv(k, raising=False)


def _pool(monkeypatch, spec, solo=("15556677792", "111")):
    if spec is not None:
        monkeypatch.setenv("WHATSAPP_NUMBER_POOL", spec)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER", solo[0])
    monkeypatch.setenv("WHATSAPP_PHONE_ID", solo[1])


def _threads(monkeypatch, counts: dict[str, int]):
    """counts: our_number -> number of live threads with the business."""
    async def _fake(our_number, business_number, limit=200):
        return [{"conversation_id": f"c{i}"} for i in range(counts.get(our_number, 0))]
    from core import conversations
    monkeypatch.setattr(conversations, "live_threads_for_pair", _fake)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_configured_number_is_always_a_member(monkeypatch):
    """A pool that excluded the number we actually send from breaks every send."""
    _pool(monkeypatch, "15551234567:222")
    numbers = [s.number for s in np.load_pool()]
    assert "15556677792" in numbers and "15551234567" in numbers


def test_no_duplicate_when_solo_is_listed(monkeypatch):
    _pool(monkeypatch, "15556677792:111,15551234567:222")
    assert len(np.load_pool()) == 2


def test_malformed_entry_is_skipped_not_fatal(monkeypatch):
    _pool(monkeypatch, "garbage,,15551234567:222,:999")
    numbers = [s.number for s in np.load_pool()]
    assert numbers == ["15556677792", "15551234567"]


def test_empty_config_yields_empty_pool(monkeypatch):
    assert np.load_pool() == []


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def test_prefers_a_number_with_no_live_thread(monkeypatch):
    """The whole point: avoid the collision rather than paper over it."""
    _pool(monkeypatch, "15551234567:222")
    _threads(monkeypatch, {"15556677792": 1, "15551234567": 0})
    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.sender.number == "15551234567"
    assert alloc.contested is False


def test_falls_back_to_fewest_when_all_contested(monkeypatch):
    _pool(monkeypatch, "15551234567:222,15559998888:333")
    _threads(monkeypatch, {"15556677792": 3, "15551234567": 5, "15559998888": 1})
    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.sender.number == "15559998888"
    assert alloc.contested is True          # still demand the reference
    assert alloc.live_threads == 1


def test_single_number_still_reports_contention(monkeypatch):
    """With no alternative the caller must STILL learn the thread is contested,
    or we would drop the reference line exactly when it is needed."""
    _pool(monkeypatch, None)
    _threads(monkeypatch, {"15556677792": 2})
    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.sender.number == "15556677792"
    assert alloc.contested is True


def test_single_number_uncontested_when_free(monkeypatch):
    _pool(monkeypatch, None)
    _threads(monkeypatch, {})
    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.contested is False


def test_unreadable_ledger_fails_to_CONTESTED_not_to_a_lie(monkeypatch):
    """We may not refuse to send, but we may not claim uncontested either."""
    _pool(monkeypatch, "15551234567:222")

    async def _boom(*a, **k):
        raise RuntimeError("db down")
    from core import conversations
    monkeypatch.setattr(conversations, "live_threads_for_pair", _boom)

    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.sender is not None          # still sends
    assert alloc.contested is True           # but honestly degraded


def test_allocation_is_deterministic(monkeypatch):
    """Same state must yield the same number — otherwise threads scatter across
    the pool and correlation gets harder, not easier."""
    _pool(monkeypatch, "15551234567:222,15559998888:333")
    _threads(monkeypatch, {"15556677792": 2, "15551234567": 2, "15559998888": 2})
    picks = {asyncio.run(np.allocate("96894639405")).sender.number for _ in range(5)}
    assert len(picks) == 1


def test_no_pool_configured_returns_no_sender(monkeypatch):
    alloc = asyncio.run(np.allocate("96894639405"))
    assert alloc.sender is None


# ---------------------------------------------------------------------------
# The dangerous one: thread opened on B, message sent from A
# ---------------------------------------------------------------------------

def test_adapter_sends_from_the_allocated_number(monkeypatch):
    """If the adapter ignored the allocation, every reply would correlate to the
    wrong pair — strictly worse than having no pool."""
    import httpx
    from channels.adapter_interface import ChannelRequest
    from channels.whatsapp.cloud_api import WhatsAppCloudAdapter

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "111")
    called = {}

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"messages": [{"id": "wamid_x"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            called["url"] = url
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("compliance.pre_check.pre_check", lambda **k: None)

    req = ChannelRequest(
        recipient_id="+96894639405", channel="whatsapp",
        message_type="transactional", content="hi",
        metadata={"whatsapp_phone_id": "222", "whatsapp_from": "15551234567"},
    )
    resp = asyncio.run(WhatsAppCloudAdapter().send(req))
    assert resp.success
    assert "/222/messages" in called["url"], "must POST to the ALLOCATED phone id"


def test_adapter_defaults_when_no_allocation(monkeypatch):
    """No metadata -> today's behaviour, unchanged."""
    import httpx
    from channels.adapter_interface import ChannelRequest
    from channels.whatsapp.cloud_api import WhatsAppCloudAdapter

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "111")
    called = {}

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"messages": [{"id": "wamid_x"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            called["url"] = url
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr("compliance.pre_check.pre_check", lambda **k: None)

    req = ChannelRequest(recipient_id="+96894639405", channel="whatsapp",
                         message_type="transactional", content="hi")
    asyncio.run(WhatsAppCloudAdapter().send(req))
    assert "/111/messages" in called["url"]


def test_webhook_replies_from_the_receiving_number(monkeypatch):
    """A clarifying question sent from a different number arrives as a stranger."""
    import agent_interface.whatsapp_webhook as wh
    _pool(monkeypatch, "15551234567:222")
    assert wh._phone_id_for("15551234567") == "222"
    assert wh._phone_id_for("15556677792") == "111"
    assert wh._phone_id_for("19999999999") == ""     # unknown -> caller defaults
