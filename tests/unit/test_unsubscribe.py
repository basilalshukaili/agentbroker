"""
One-click unsubscribe.

Both email adapters appended a CAN-SPAM opt-out link pointing at
`your-domain.example` / `smb-broker.example` - domains that do not exist - and
no unsubscribe route existed on ours either. Every commercial email we sent
carried a dead opt-out (found 2026-08-26).

These tests pin the properties that make the fix real rather than cosmetic:
the link is per-recipient and unforgeable, one click actually blocks future
sends through the SAME enforcement path as a WhatsApp STOP, and a bad link
still gives a human a way out.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_interface import unsubscribe as unsub


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(unsub.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_consent(monkeypatch):
    """Fresh consent store per test, and no database writes."""
    from compliance.consent_store import ConsentStore
    store = ConsentStore()
    import compliance.consent_store as cs
    monkeypatch.setattr(cs, "get_consent_store", lambda: store)

    async def _noop(*a, **k):
        return None
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _noop)
    return store


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def test_token_roundtrips():
    tok = unsub.make_token("ann@example.com", "email")
    assert unsub.parse_token(tok) == ("ann@example.com", "email")


def test_token_cannot_be_forged_or_edited():
    """The whole no-login design rests on this: editing the payload to name a
    different recipient must fail, or anyone could unsubscribe anyone."""
    tok = unsub.make_token("ann@example.com", "email")
    body, _, sig = tok.partition(".")
    import base64
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    tampered = payload.replace("ann@", "bob@")
    forged = base64.urlsafe_b64encode(tampered.encode()).decode().rstrip("=") + "." + sig
    assert unsub.parse_token(forged) is None


def test_garbage_tokens_are_rejected_not_crashed():
    for bad in ("", "nonsense", "a.b", "....", "x" * 500):
        assert unsub.parse_token(bad) is None


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr(unsub, "_TTL_S", -1)
    assert unsub.parse_token(unsub.make_token("ann@example.com")) is None


def test_link_uses_our_own_domain():
    """The defect was a link on a domain we do not own."""
    url = unsub.unsubscribe_url("ann@example.com")
    assert "hatchloop.dev" in url
    for dead in ("your-domain.example", "smb-broker.example", "workers.dev"):
        assert dead not in url


# ---------------------------------------------------------------------------
# The click actually blocks future sends
# ---------------------------------------------------------------------------

def test_one_click_blocks_future_sends(client, isolated_consent):
    """Not just a nice page - it must flip the gate every send path consults."""
    tok = unsub.make_token("ann@example.com", "email")
    assert isolated_consent.is_opted_out("ann@example.com", "email") is False

    r = client.get(f"/unsubscribe?t={tok}")
    assert r.status_code == 200
    assert "unsubscribed" in r.text.lower()
    assert isolated_consent.is_opted_out("ann@example.com", "email") is True


def test_unsubscribe_is_idempotent(client, isolated_consent):
    tok = unsub.make_token("ann@example.com", "email")
    for _ in range(3):
        assert client.get(f"/unsubscribe?t={tok}").status_code == 200
    assert isolated_consent.is_opted_out("ann@example.com", "email") is True


def test_rfc8058_one_click_post(client, isolated_consent):
    """Gmail/Yahoo POST here with no user interaction; a GET-only endpoint gets
    our mail filtered no matter what the law requires."""
    tok = unsub.make_token("ann@example.com", "email")
    r = client.post(f"/unsubscribe?t={tok}")
    assert r.status_code == 200
    assert r.json()["unsubscribed"] is True
    assert isolated_consent.is_opted_out("ann@example.com", "email") is True


def test_post_rejects_bad_token(client, isolated_consent):
    assert client.post("/unsubscribe?t=garbage").status_code == 400
    assert isolated_consent.is_opted_out("ann@example.com", "email") is False


def test_bad_link_still_offers_a_way_out(client):
    """A dead end here is the same failure we are fixing."""
    r = client.get("/unsubscribe?t=broken")
    assert r.status_code == 400
    assert unsub.SUPPORT_EMAIL in r.text
    assert "STOP" in r.text


def test_optout_is_durably_recorded(client, monkeypatch):
    """Memory-only opt-out is exactly the compliance leak fixed in August."""
    written = []

    async def _capture(table, row):
        written.append((table, row))
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _capture)

    client.get(f"/unsubscribe?t={unsub.make_token('ann@example.com')}")
    assert written, "opt-out must be written durably, not just held in memory"
    table, row = written[0]
    assert table == "consent_optouts"
    assert row["recipient_id"] == "ann@example.com"
    assert row["source"] == "unsubscribe_link"


def test_db_failure_still_enforces_in_memory(client, isolated_consent, monkeypatch):
    """Enforcement must not depend on the database being reachable."""
    async def _boom(*a, **k):
        raise RuntimeError("supabase down")
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _boom)

    r = client.get(f"/unsubscribe?t={unsub.make_token('ann@example.com')}")
    assert r.status_code == 200
    assert isolated_consent.is_opted_out("ann@example.com", "email") is True


# ---------------------------------------------------------------------------
# The adapters embed a working link
# ---------------------------------------------------------------------------

def test_no_adapter_ships_a_dead_domain():
    """Regression pin on the actual defect."""
    # Resolved from THIS FILE, not the working directory - see the note in
    # test_audit_durability.py. A dead-opt-out regression check that silently
    # depends on cwd is one that can stop checking without anyone noticing.
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for path in ("channels/sms_email/resend_email.py",
                 "channels/sms_email/sendgrid_email.py"):
        src = open(os.path.join(root, *path.split("/")), encoding="utf-8").read()
        for dead in ("your-domain.example/unsubscribe",
                     "smb-broker.example/unsubscribe"):
            assert dead not in src, f"{path} still ships {dead}"


def test_list_unsubscribe_headers_on_commercial_mail(monkeypatch):
    """RFC 8058 - Gmail/Yahoo filter bulk mail that lacks these."""
    from channels.adapter_interface import ChannelRequest
    from channels.sms_email.resend_email import ResendEmailAdapter

    monkeypatch.delenv("BUSINESS_UNSUBSCRIBE_URL", raising=False)
    a = ResendEmailAdapter()
    h = a._unsubscribe_headers(ChannelRequest(
        recipient_id="ann@example.com", channel="email",
        message_type="marketing", content="hi"))["headers"]
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert h["List-Unsubscribe"].startswith("<https://")
    assert "/unsubscribe?t=" in h["List-Unsubscribe"]


def test_no_unsubscribe_header_on_transactional_mail(monkeypatch):
    """A booking confirmation is not something to unsubscribe from; offering it
    there trains people to opt out of mail they asked for."""
    from channels.adapter_interface import ChannelRequest
    from channels.sms_email.resend_email import ResendEmailAdapter

    a = ResendEmailAdapter()
    assert a._unsubscribe_headers(ChannelRequest(
        recipient_id="ann@example.com", channel="email",
        message_type="transactional", content="your booking is confirmed")) == {}


def test_marketing_email_embeds_a_real_per_recipient_link(monkeypatch):
    import asyncio
    from channels.adapter_interface import ChannelRequest
    from channels.sms_email.resend_email import ResendEmailAdapter

    monkeypatch.setattr("compliance.pre_check.pre_check", lambda **k: None)
    monkeypatch.setenv("ALLOW_STUB_CHANNELS", "1")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("BUSINESS_UNSUBSCRIBE_URL", raising=False)

    captured = {}
    adapter = ResendEmailAdapter()
    real_send = adapter.send

    # COUNTRY IS NOW REQUIRED FOR MARKETING, and this test is the reason the
    # change is safe to make: it was sending a marketing email with no
    # jurisdiction at all, which the gate used to resolve to INTERNATIONAL
    # rules while labelling the decision "US" - two different answers to
    # "which law applies" inside one call. A real US marketing email is lawful
    # under CAN-SPAM without prior opt-in precisely BECAUSE it carries an
    # unsubscribe link, which is what this test checks, so stating US here
    # makes the test more faithful rather than less.
    req = ChannelRequest(recipient_id="ann@example.com", channel="email",
                         message_type="marketing", content="Hello there",
                         country_code="US")
    # Stub mode returns before the network call, but the footer is appended
    # first - assert on the body the adapter built.
    import channels.sms_email.resend_email as mod
    orig_fmt = mod._UNSUBSCRIBE_FOOTER

    class _Spy(str):
        def format(self, **kw):
            captured.update(kw)
            return orig_fmt.format(**kw)
    monkeypatch.setattr(mod, "_UNSUBSCRIBE_FOOTER", _Spy(orig_fmt))

    asyncio.run(real_send(req))
    link = captured.get("unsubscribe_url", "")
    assert "hatchloop.dev" in link and "/unsubscribe?t=" in link
    # and the token in it actually identifies THIS recipient
    tok = link.split("t=", 1)[1]
    assert unsub.parse_token(tok) == ("ann@example.com", "email")
