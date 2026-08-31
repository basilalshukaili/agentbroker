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
import socket

import pytest

from core import number_pool as np


# ---------------------------------------------------------------------------
# I/O containment - the invariant is stated, not hoped for
# ---------------------------------------------------------------------------
# WHY THIS EXISTS (2026-08-31). The two adapter tests at the bottom of this file
# failed once inside a full-suite run and passed on every re-run - the exact
# shape of a test that is quietly talking to the network.
#
# They stubbed the compliance gate as "compliance.pre_check.pre_check". But
# channels/whatsapp/cloud_api.py binds that symbol AT IMPORT TIME
# (`from compliance.pre_check import pre_check`), so the stub rebound a name the
# adapter never reads: dead code, and the REAL gate ran on every send. The gate's
# last act is get_audit_log().record(), which fire-and-forgets a Supabase write
# onto whatever event loop is running - so anywhere SUPABASE_URL is configured, a
# test that believes it is offline was issuing a live HTTP POST mid-assertion.
#
# Two guards, because the two failure modes are opposite and only one of them
# involves a socket:
#
#   * no_outbound_io - anything aimed off-box raises. Catches whatever escapes
#     the httpx patch.
#   * _record_http   - records EVERY request, and the tests assert there is
#     exactly one. Catches the case that actually bit us: a stray call the httpx
#     patch happily absorbs, overwriting the single URL the assertion then reads.
#     A socket guard alone would have seen nothing at all here.
#
# Loopback is deliberately left alone: asyncio's Windows ProactorEventLoop builds
# its self-pipe from a real 127.0.0.1 socketpair, so banning sockets outright
# breaks asyncio.run itself and every test in this file fails for the wrong
# reason (measured, not assumed).
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class OutboundIOInTest(RuntimeError):
    """A test that claims to do no I/O reached for the network."""


def _hostname(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else value


@pytest.fixture(autouse=True)
def no_outbound_io(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _is_local(address):
        host = address[0] if isinstance(address, tuple) else address
        return _hostname(host) in _LOOPBACK

    def _connect(self, address, *a, **k):
        if not _is_local(address):
            raise OutboundIOInTest(f"outbound connect({address!r}) from a test")
        return real_connect(self, address, *a, **k)

    def _connect_ex(self, address, *a, **k):
        if not _is_local(address):
            raise OutboundIOInTest(f"outbound connect_ex({address!r}) from a test")
        return real_connect_ex(self, address, *a, **k)

    def _getaddrinfo(host, port, *a, **k):
        if _hostname(host) not in _LOOPBACK:
            raise OutboundIOInTest(f"outbound DNS for {_hostname(host)!r} from a test")
        return real_getaddrinfo(host, port, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


class _FakeResponse:
    status_code = 200
    content = b"{}"

    def json(self):
        return {"messages": [{"id": "wamid_x"}]}


def _record_http(monkeypatch) -> list[str]:
    """Patch httpx.AsyncClient; return the list of URLs anything POSTs to.

    A LIST, not a dict holding "the" URL. The flake this file suffered wrote a
    SECOND, unrelated POST (the compliance audit mirror, to Supabase) through
    this same mock - with a dict the last writer won and the assertion read back
    a URL the adapter never chose.
    """
    import httpx

    urls: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            urls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return urls


def _stub_compliance_gate(monkeypatch):
    """No-op the compliance gate FOR THE ADAPTER. The patch target is the point.

    Patching "compliance.pre_check.pre_check" rebinds the definition site, which
    every adapter has already copied into its own module namespace at import
    time - so the stub never fires. Patch the binding the caller actually reads,
    the way tests/unit/test_twilio_dual_auth.py documents. The gate itself is
    covered by tests/unit/test_compliance.py; in THIS file it is noise, and
    (until now) noise that made a network call.
    """
    monkeypatch.setattr("channels.whatsapp.cloud_api.pre_check", lambda **k: None)


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
    from channels.adapter_interface import ChannelRequest
    from channels.whatsapp.cloud_api import WhatsAppCloudAdapter

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "111")
    urls = _record_http(monkeypatch)
    _stub_compliance_gate(monkeypatch)

    req = ChannelRequest(
        recipient_id="+96894639405", channel="whatsapp",
        message_type="transactional", content="hi",
        metadata={"whatsapp_phone_id": "222", "whatsapp_from": "15551234567"},
    )
    resp = asyncio.run(WhatsAppCloudAdapter().send(req))
    assert resp.success
    # ONE send is ONE request. Anything else on this list is a side channel the
    # test was never told about - which is how the Supabase audit mirror used to
    # smuggle itself in here and overwrite the URL below.
    assert len(urls) == 1, f"one send, one request - got {urls}"
    assert "/222/messages" in urls[0], "must POST to the ALLOCATED phone id"


def test_adapter_defaults_when_no_allocation(monkeypatch):
    """No metadata -> today's behaviour, unchanged."""
    from channels.adapter_interface import ChannelRequest
    from channels.whatsapp.cloud_api import WhatsAppCloudAdapter

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "111")
    urls = _record_http(monkeypatch)
    _stub_compliance_gate(monkeypatch)

    req = ChannelRequest(recipient_id="+96894639405", channel="whatsapp",
                         message_type="transactional", content="hi")
    asyncio.run(WhatsAppCloudAdapter().send(req))
    assert len(urls) == 1, f"one send, one request - got {urls}"
    assert "/111/messages" in urls[0]


def test_webhook_replies_from_the_receiving_number(monkeypatch):
    """A clarifying question sent from a different number arrives as a stranger."""
    import agent_interface.whatsapp_webhook as wh
    _pool(monkeypatch, "15551234567:222")
    assert wh._phone_id_for("15551234567") == "222"
    assert wh._phone_id_for("15556677792") == "111"
    assert wh._phone_id_for("19999999999") == ""     # unknown -> caller defaults
