"""/checkout and /billing/checkout sell only what a gate will deliver.

THE DEFECT (review of feat/x402-honesty-20261004, finding F2). With CREDITS_ENABLED off, which is the live
state, /checkout still said "Credits, bought by card through Polar", listed a package table and linked
/billing/checkout, which minted a real Polar checkout session. A buyer who paid got a key but NO credits:
billing/polar_webhook.py skips the grant while the credits gate is off, and the grant is idempotent on the
order id, so a purchase made while the gate is off is never credited unless somebody replays it by hand.
With x402 on and credits off the page still said "Two rails" (one rail).

The card half of the page and the Polar session are now functions of billing.switches.credits_enabled(), the
expression the webhook's grant uses; "Two rails" needs both rails. Nothing here touches the network.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RECEIVER = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _every_switch_off(monkeypatch):
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: False)


def _set(monkeypatch, *, credits=False, x402=False):
    if credits:
        monkeypatch.setenv("CREDITS_ENABLED", "true")
    from billing import x402_gate
    import config
    monkeypatch.setattr(x402_gate, "enabled", lambda: x402)
    if x402:
        monkeypatch.setattr(config, "X402_RECEIVER_ADDRESS", RECEIVER)


def _client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def _checkout():
    r = _client().get("/checkout")
    assert r.status_code == 200
    return r.text


CARD_HALF = ("Credit packages", "Pay with card via Polar", "/billing/checkout", "Credits, bought by card",
             "One balance covers every HatchLoop server", "14-day refund")


# ------------------------------------------------------------------------ the page

def test_credits_off_the_page_does_not_sell_credits(monkeypatch):
    """THE FINDING. Every switch off: no package table, no pay link, no 'bought by card'."""
    text = _checkout()
    for phrase in CARD_HALF:
        assert phrase not in text, f"/checkout still says {phrase!r} while credits are off"
    assert "x402" not in text.lower() and "Two rails" not in text
    # it says what is true instead, in words
    assert "No payment rail is switched on" in text
    assert "not on sale" in text
    # and the price schedule is still there for the buyer who wants to know what it will cost
    assert "capture_lead" in text


def test_credits_on_x402_off_the_card_half_is_back_and_it_is_one_rail(monkeypatch):
    _set(monkeypatch, credits=True)
    text = _checkout()
    for phrase in CARD_HALF:
        assert phrase in text, f"{phrase!r} missing with credits on"
    assert "Two rails" not in text and "x402" not in text.lower()
    assert "not on sale" not in text and "No payment rail is switched on" not in text


def test_x402_on_credits_off_it_is_one_rail_and_it_is_x402(monkeypatch):
    """The second state the review found: 'Two rails' with one rail."""
    _set(monkeypatch, x402=True)
    text = _checkout()
    assert "Two rails" not in text
    assert "x402" in text and "USDC on Base" in text
    for phrase in CARD_HALF:
        assert phrase not in text, f"{phrase!r} shown while credits are off"
    assert "No payment rail is switched on" not in text


def test_both_rails_on_it_is_two_rails(monkeypatch):
    _set(monkeypatch, credits=True, x402=True)
    text = _checkout()
    assert "Two rails" in text and "x402" in text
    for phrase in CARD_HALF:
        assert phrase in text, phrase


def test_the_meta_description_follows_the_switches(monkeypatch):
    import re

    def meta():
        m = re.search(r'<meta name="description" content="([^"]*)"', _checkout())
        return m.group(1) if m else ""

    assert "Credits, bought by card" not in meta()
    _set(monkeypatch, credits=True)
    assert "Credits, bought by card" in meta()


# ------------------------------------------------------------------------ the pay link

@pytest.fixture
def provider(monkeypatch):
    """A stand-in billing provider that records whether a real checkout session would have been minted."""
    calls = []

    class Provider:
        async def create_checkout(self, **kw):
            calls.append(kw)
            return SimpleNamespace(payment_url="https://polar.example/pay/abc", metadata={}, provider="polar")

    import billing.providers as providers
    monkeypatch.setattr(providers, "get_billing_provider", lambda *a, **k: Provider())
    return calls


def test_credits_off_the_pay_link_mints_no_checkout_session(provider):
    r = _client().get("/billing/checkout", follow_redirects=False)
    assert provider == [], "a Polar session was minted while credits are off: the buyer pays for nothing"
    assert r.status_code == 303 and r.headers["location"] == "/checkout"


def test_credits_on_the_pay_link_still_works(monkeypatch, provider):
    _set(monkeypatch, credits=True)
    r = _client().get("/billing/checkout", follow_redirects=False)
    assert len(provider) == 1
    assert r.status_code == 200 and "https://polar.example/pay/abc" in r.text


def test_the_gate_and_the_page_share_one_expression(monkeypatch):
    """The webhook grants credits through switches.credits_enabled(); the page and the link must use the
    same expression, so 'the page sells credits' and 'a purchase is credited' can never differ."""
    import inspect
    import main
    import billing.polar_webhook as pw
    from web import pages
    assert "credits_enabled()" in inspect.getsource(pw.handle_polar_event)
    assert "credits_enabled()" in inspect.getsource(pages.render_checkout)
    assert "credits_enabled()" in inspect.getsource(main.billing_checkout)
