"""Credits are bought on the website; the assistant must be able to spend them.

ChatGPT's app rules forbid selling credits inside the conversation, so the money step happens on
hatchloop.dev (Polar checkout) and the assistant only ever holds an access token. The seam between the two is
the Polar order webhook: it is the one place that sees the buyer's email next to the customer id the credits
are granted to (`sub_<customer id>`). It records that link; the sign-in reads it when it mints a token.

Pinned here: a paid order writes the link; a redelivered order and a second purchase change nothing; a
failure to write the link can NEVER fail or even delay a paid order; and the whole loop - sign in free, buy,
refresh - ends with the assistant holding the paid identity, while an order with no email links nothing.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

import main
from agent_interface.identity import validate_token
from agent_interface.oauth import limits, tokens
from agent_interface.oauth.link import link_purchase
from agent_interface.oauth.store import MemoryStore, StoreUnavailable, set_store
from billing.polar_webhook import handle_polar_event
from tests.oauth_support import install_mailbox, pkce, poll_secret_of, rid_of, magic_of, code_of


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _store():
    store = MemoryStore()
    set_store(store)
    limits.LIMITS.reset()
    yield store
    set_store(None)


def _order(order_id="order_link_1", customer="cus_LINK1", email="Buyer@Example.org"):
    return {"type": "order.paid", "data": {"id": order_id, "customer": {"id": customer, "email": email}}}


_OUTBOUND = ("RESEND_API_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY", "TELEGRAM_BOT_TOKEN",
             "TELEGRAM_CHAT_ID", "POLAR_ACCESS_TOKEN", "POLAR_API_KEY", "CREDITS_ENABLED")


@pytest.fixture(autouse=True)
def _nothing_leaves_the_machine(monkeypatch):
    """The real webhook handler runs, with every outbound credential blank: its mail, Telegram and database
    paths take their 'not configured' branches, so a paid-order test can neither send nor spend anything."""
    for name in _OUTBOUND:
        monkeypatch.delenv(name, raising=False)


def _deliver(event):
    """Run the production handler once. Returns nothing; raises if the handler itself raises."""
    run(handle_polar_event(event))
    return 1


def test_a_paid_order_links_the_buyers_email_to_the_account_the_credits_go_to(_store):
    assert _deliver(_order()) == 1
    link = _store.links[tokens.email_hash("buyer@example.org")]            # lower-cased: the case the buyer typed is irrelevant
    assert link["account_id"] == "sub_cus_LINK1" and link["customer_id"] == "cus_LINK1"
    assert link["plan"] == "developer"
    assert "buyer@example.org" not in str(_store.links)                    # only the digest is stored


def test_a_redelivered_order_and_a_second_purchase_never_change_an_existing_link(_store):
    _deliver(_order())
    _deliver(_order())                                                      # Polar retried the webhook
    _deliver(_order(order_id="order_link_2", customer="cus_SOMEONE_ELSE"))  # same email, different customer id
    assert len(_store.links) == 1
    assert _store.links[tokens.email_hash("buyer@example.org")]["account_id"] == "sub_cus_LINK1"


def test_an_order_without_an_email_links_nothing(_store):
    assert _deliver({"type": "order.paid", "data": {"id": "order_noemail", "customer": {"id": "cus_X"}}}) == 1
    assert _store.links == {}


def test_a_database_that_is_down_never_fails_a_paid_order(_store, caplog, monkeypatch):
    async def down(*a, **k):
        raise StoreUnavailable("spine down")
    monkeypatch.setattr(_store, "account_link", down)
    with caplog.at_level(logging.DEBUG):
        assert _deliver(_order(order_id="order_down", customer="cus_DOWN")) == 1      # the order still completes
    assert not any("buyer@example.org" in r.getMessage().lower() for r in caplog.records)


def test_link_purchase_refuses_anything_that_is_not_a_subscription_account(_store):
    for account in ("free_abcdef", "admin", "", "sub"):
        assert run(link_purchase("a@example.org", account, "c", "developer")) is False
    assert run(link_purchase("not-an-email", "sub_x", "c", "developer")) is False
    assert run(link_purchase(None, "sub_x", "c", "developer")) is False
    assert run(link_purchase("a@example.org", "sub_x", "c", "developer")) is True
    assert _store.links.keys() == {tokens.email_hash("a@example.org")}


def test_the_whole_loop_sign_in_free_buy_on_the_website_refresh_and_hold_the_paid_identity(_store, monkeypatch):
    box = install_mailbox(monkeypatch)
    c = TestClient(main.app, base_url="https://api.hatchloop.dev", follow_redirects=False)
    reg = c.post("/oauth/register", json={"redirect_uris": ["https://assistant.example.org/cb"]}).json()["client_id"]
    verifier, challenge = pkce()
    page = c.get("/oauth/authorize", params={"response_type": "code", "client_id": reg, "redirect_uri": "https://assistant.example.org/cb",
                                             "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
    c.post("/oauth/authorize/email", data={"rid": rid_of(page.text), "poll_secret": poll_secret_of(page.text),
                                           "email": "Buyer@Example.org"})
    done = c.post("/oauth/verify", data={"t": magic_of(box.last_link), "decision": "approve"})
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code_of(done.headers["location"]),
                                       "redirect_uri": "https://assistant.example.org/cb", "client_id": reg,
                                       "code_verifier": verifier}).json()
    free = validate_token(tok["access_token"]).identity
    assert free.agent_id.startswith("free_") and free.scope.budget_cap == 0.0

    _deliver(_order(email="buyer@example.org"))                            # ... the person buys credits on hatchloop.dev

    nxt = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": reg}).json()
    paid = validate_token(nxt["access_token"]).identity
    assert paid.agent_id == "sub_cus_LINK1" and paid.principal.id == "cus_LINK1" and paid.scope.budget_cap > 0
    # and the credit path recognises it as a funded account, not a free key
    from billing.credits import is_free_key
    assert is_free_key(free.agent_id) and not is_free_key(paid.agent_id)
