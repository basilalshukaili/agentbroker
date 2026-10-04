"""The portal's top-up route sells credit packages only while the credits gate delivers them.

THE DEFECT (third review of feat/x402-honesty-20261004, finding P1). hatchloop.dev/pricing links every
package to /portal?package=starter|growth|scale, and the portal's "Complete your purchase" button POSTs
/portal-api/topup, which the site proxies to agent_interface/portal.py. That route minted a Polar checkout
for a credit package WITHOUT looking at CREDITS_ENABLED. With credits off (the running container),
billing/polar_webhook.py skips the grant and is idempotent on the order id, so a buyer who paid got a
receipt and no credits: the one public claim that can take a customer's money.

/billing/checkout already refuses while credits are off (second pass); this is the same rule on the second
door. billing.switches.credits_enabled() is the expression the webhook's grant uses, so "we sell it" and
"we deliver it" are one test. The page's own copy (hatchloop.dev/pricing, a separate Next.js site) is a
founder decision and is policed by scripts/live_verify_release.py. Nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def portal(monkeypatch):
    """The portal module with the session, the account lookup and Polar replaced by recorders."""
    import agent_interface.portal as P
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    created = []

    def session(_cookie):
        return "buyer@example.com"

    async def no_account(_email):
        return None

    async def checkout(product_id, account_id, email, credits):
        created.append((product_id, credits))
        return "https://polar.example/checkout/abc"

    monkeypatch.setattr(P, "_require_session", session)
    monkeypatch.setattr(P, "_get_account", no_account)
    monkeypatch.setattr(P, "_create_polar_checkout", checkout)
    monkeypatch.setattr(P, "product_id_for_package", lambda pkg: (f"prod_{pkg}", 1000))
    P.created = created
    return P


def _topup(P, package="starter"):
    resp = _run(P.portal_topup(P.TopupRequest(package=package), hl_portal="cookie"))
    import json
    return json.loads(resp.body)


def test_credits_off_the_portal_mints_no_checkout(portal):
    body = _topup(portal)
    assert body["ok"] is False and body["reason"] == "credits_not_enabled", body
    assert portal.created == [], "a Polar checkout was created for a package that would never be credited"
    assert "checkout_url" not in body


@pytest.mark.parametrize("package", ["starter", "growth", "scale"])
def test_every_package_is_refused_while_credits_are_off(portal, package):
    assert _topup(portal, package)["ok"] is False
    assert portal.created == []


def test_credits_on_the_portal_still_sells(portal, monkeypatch):
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    body = _topup(portal)
    assert body == {"ok": True, "checkout_url": "https://polar.example/checkout/abc"}
    assert portal.created == [("prod_starter", 1000)]


def test_the_refusal_comes_before_the_package_lookup(portal, monkeypatch):
    """No Polar product id is read, and nothing about the account is looked up, for a package that cannot be sold."""
    looked = []
    monkeypatch.setattr(portal, "product_id_for_package", lambda pkg: looked.append(pkg) or ("p", 1))
    _topup(portal)
    assert looked == []


def test_an_unknown_package_is_still_a_400_in_both_states(portal, monkeypatch):
    from fastapi import HTTPException
    for credits in (False, True):
        monkeypatch.setenv("CREDITS_ENABLED", "true") if credits else monkeypatch.delenv("CREDITS_ENABLED", raising=False)
        with pytest.raises(HTTPException) as e:
            _topup(portal, "platinum")
        assert e.value.status_code == 400


def test_the_session_is_still_required_before_anything(portal, monkeypatch):
    from fastapi import HTTPException

    def refuse(_cookie):
        raise HTTPException(status_code=401, detail="no session")
    monkeypatch.setattr(portal, "_require_session", refuse)
    with pytest.raises(HTTPException) as e:
        _topup(portal)
    assert e.value.status_code == 401
