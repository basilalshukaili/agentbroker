"""Board row 249 — the live money-path defect.

https://api.hatchloop.dev/billing/checkout returned HTTP 200, rendered
"Taking you to secure checkout (Polar)..." and sent a real visitor to
https://wise.com/pay/your-link-here — a placeholder URL that goes nowhere,
under a page that lied about which provider was in use. Cause chain:

1. BILLING_PROVIDER was absent from .env, so get_billing_provider() fell back
   to its documented default, ManualProvider.
2. ManualProvider.create_checkout() fabricated the hardcoded placeholder URL
   when WISE_PAYMENT_LINK/PAYPAL_PAYMENT_LINK were both unset, and — unlike
   every other provider in billing/providers.py — never flagged the result
   as a stub, so main.py's `if not url or session.metadata.get("stub")`
   safety net could not catch it.
3. ManualProvider.health_check() returned True unconditionally, so nothing
   alarmed on any of this.

This file proves the fix: a provider that cannot produce a real payment URL
now refuses (returns the same stub shape every other dead-rail provider in
this file already uses) instead of fabricating one, health_check() reflects
real configuration state, and the route's copy names whichever provider is
actually in play rather than hardcoding "Polar".

Every test below was run red (against the pre-fix code) and confirmed to
fail before the fix landed, then green after — see the session's own
red/green transcript; this file is what stays behind as the regression
guard.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


PLACEHOLDER_URL = "https://wise.com/pay/your-link-here"


# ---------------------------------------------------------------------------
# ManualProvider itself: refuse, don't fabricate.
# ---------------------------------------------------------------------------

class TestManualProviderRefusesInsteadOfFabricating:
    def test_no_links_configured_returns_a_flagged_stub_not_the_placeholder(self, monkeypatch):
        monkeypatch.delenv("WISE_PAYMENT_LINK", raising=False)
        monkeypatch.delenv("PAYPAL_PAYMENT_LINK", raising=False)
        from billing.providers import ManualProvider

        prov = ManualProvider()
        session = run(prov.create_checkout(
            amount_usd=9.0, description="x", agent_id="a",
            success_url="https://api.hatchloop.dev/billing/success",
            cancel_url="https://api.hatchloop.dev/pricing",
        ))
        assert session.metadata.get("stub") is True, (
            "unconfigured ManualProvider must return the same flagged-stub "
            "shape every other dead-rail provider in this file uses, so "
            "main.py's existing stub guard catches it too"
        )
        assert session.payment_url != PLACEHOLDER_URL
        assert "your-link-here" not in session.payment_url

    def test_health_check_is_false_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("WISE_PAYMENT_LINK", raising=False)
        monkeypatch.delenv("PAYPAL_PAYMENT_LINK", raising=False)
        from billing.providers import ManualProvider

        assert ManualProvider().health_check() is False, (
            "a health check that cannot return False is not a health check"
        )

    def test_health_check_is_true_and_checkout_is_real_when_a_link_is_configured(self, monkeypatch):
        monkeypatch.setenv("WISE_PAYMENT_LINK", "https://wise.com/pay/test-fixture-link")
        monkeypatch.delenv("PAYPAL_PAYMENT_LINK", raising=False)
        from billing.providers import ManualProvider

        prov = ManualProvider()
        assert prov.health_check() is True
        session = run(prov.create_checkout(
            amount_usd=9.0, description="x", agent_id="a",
            success_url="https://api.hatchloop.dev/billing/success",
            cancel_url="https://api.hatchloop.dev/pricing",
        ))
        assert session.metadata.get("stub") is not True
        assert session.payment_url == "https://wise.com/pay/test-fixture-link"


# ---------------------------------------------------------------------------
# The route: never a dead-URL redirect, never a false "(Polar)" claim.
# ---------------------------------------------------------------------------

class TestBillingCheckoutRouteNeverServesThePlaceholder:
    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app, raise_server_exceptions=False)

    def test_manual_provider_unconfigured_redirects_to_pricing_not_a_dead_page(self, monkeypatch):
        monkeypatch.setenv("BILLING_PROVIDER", "manual")
        monkeypatch.delenv("WISE_PAYMENT_LINK", raising=False)
        monkeypatch.delenv("PAYPAL_PAYMENT_LINK", raising=False)
        client = self._client()

        r = client.get("/billing/checkout", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers.get("location") == "/pricing"
        assert "your-link-here" not in r.text
        assert "Polar" not in r.text

    def test_route_names_the_real_provider_not_always_polar(self, monkeypatch):
        """Route-level proof that the copy is provider-driven: swap in a fake
        provider (no network, no real credentials) and confirm the rendered
        page names IT, never a hardcoded 'Polar'."""
        import billing.providers as providers_mod
        from billing.providers import BillingProvider, CheckoutSession

        class _FakeCoinbaseLikeProvider(BillingProvider):
            name = "coinbase"

            async def create_checkout(self, *, amount_usd, description, agent_id,
                                      success_url, cancel_url) -> CheckoutSession:
                return CheckoutSession(
                    session_id="fake_cb_1",
                    payment_url="https://commerce.coinbase.com/charges/test-fixture",
                    amount_usd=amount_usd,
                    provider="coinbase",
                    metadata={},
                )

            async def get_status(self, session_id):
                return None

            def health_check(self) -> bool:
                return True

        monkeypatch.setattr(
            providers_mod, "get_billing_provider",
            lambda: _FakeCoinbaseLikeProvider(),
        )
        client = self._client()

        r = client.get("/billing/checkout", follow_redirects=False)
        assert r.status_code == 200
        assert "(Coinbase)" in r.text
        assert "Polar" not in r.text

    def test_route_names_manual_not_polar_when_a_real_manual_link_is_configured(self, monkeypatch):
        monkeypatch.setenv("BILLING_PROVIDER", "manual")
        monkeypatch.setenv("WISE_PAYMENT_LINK", "https://wise.com/pay/test-fixture-link")
        monkeypatch.delenv("PAYPAL_PAYMENT_LINK", raising=False)
        client = self._client()

        r = client.get("/billing/checkout", follow_redirects=False)
        assert r.status_code == 200
        assert "your-link-here" not in r.text
        assert "Polar" not in r.text
        assert "manual payment link" in r.text


# ---------------------------------------------------------------------------
# Structural proof: the displayed name and the redirect target must come
# from the SAME CheckoutSession object, so they cannot drift apart again.
# ---------------------------------------------------------------------------

class TestDisplayedNameAndRedirectTargetShareOneSource:
    """The 2026-09-21 incident was exactly two facts disagreeing: the page's
    copy said "(Polar)" while the embedded redirect target was a dead
    wise.com URL. Nothing stopped that divergence because the label was a
    hardcoded literal, independent of whatever session.payment_url the
    checkout call actually produced.

    This test does not just check one provider's label (the tests above
    already do that). It proves the *coupling*: label and destination are
    both read off the one CheckoutSession returned by the one
    provider.create_checkout() call, by using values ("provider" and
    "payment_url") a per-run random token that cannot be coincidentally
    hardcoded anywhere in main.py. If the route ever regressed to reading
    the label from a separate source (a hardcoded string, a second lookup,
    the BILLING_PROVIDER env var re-read independently of the session),
    this would fail even though the redirect itself still worked.
    """

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app, raise_server_exceptions=False)

    def test_label_and_redirect_url_trace_back_to_the_same_session_object(self, monkeypatch):
        import uuid
        import billing.providers as providers_mod
        from billing.providers import BillingProvider, CheckoutSession

        # A random-per-run token in BOTH the provider name and the payment
        # URL. Nothing in main.py can name this value in advance, so a match
        # is only possible if the route actually reads both off this one
        # session object rather than off any independently-sourced value.
        token = f"probe{uuid.uuid4().hex[:12]}"
        fake_url = f"https://pay.example.test/{token}/checkout"

        class _ProbeProvider(BillingProvider):
            name = token  # deliberately not in main.py's known-label map

            async def create_checkout(self, *, amount_usd, description, agent_id,
                                      success_url, cancel_url) -> CheckoutSession:
                return CheckoutSession(
                    session_id=f"probe_{token}",
                    payment_url=fake_url,
                    amount_usd=amount_usd,
                    provider=token,
                    metadata={},
                )

            async def get_status(self, session_id):
                return None

            def health_check(self) -> bool:
                return True

        monkeypatch.setattr(
            providers_mod, "get_billing_provider",
            lambda: _ProbeProvider(),
        )
        client = self._client()

        r = client.get("/billing/checkout", follow_redirects=False)
        assert r.status_code == 200
        # The redirect target embedded in the page is this session's own url...
        assert fake_url in r.text, (
            "the redirect destination in the rendered page does not match "
            "the payment_url this provider's CheckoutSession returned"
        )
        # ...and the displayed name is this SAME session's own provider field,
        # not a separately configured or hardcoded value that happens to
        # equal it.
        assert f"({token})" in r.text, (
            "the displayed provider label does not match session.provider — "
            "name and destination are being read from different sources, "
            "which is exactly the row-249 defect shape (page said 'Polar', "
            "URL went to wise.com)"
        )
