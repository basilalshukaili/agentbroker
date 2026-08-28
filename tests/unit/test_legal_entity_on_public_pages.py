"""
Every public page must name the entity that actually takes the money.

WHAT WAS WRONG (found 2026-08-28 by a legal-surface audit). `web/_partials.py`
hardcoded:

    LEGAL_ENTITY = "Agent Broker (sole proprietor: <founder's full legal name>,
                    Sultanate of Oman)"

and interpolated it into Terms section 7 (who owns the Service), section 9 (who
you indemnify and hold harmless), section 13 (the contracting party and notice
address), the Privacy "who we are" data-controller declaration, the Refund
policy, and the footnote on every page including /billing/checkout. All live.

Two things wrong at once, and the second is worse than the first:

  * It named a legal form holding NO commercial registration. Techmate - the
    registered company that actually receives the money - appeared nowhere in
    the entire agentbroker tree.
  * It put the founder PERSONALLY on the indemnification clause of a contract
    governed by Omani law in the courts of Muscat, while Techmate got no
    contractual protection at all.

Founder's ruling (2026-08-28): "we already registered techmate, we will treat
techmate as legal company and hatchloop as its one of the products." HatchLoop
and AgentBroker are product names. They are never a party to anything.

The footer also credited a payment company that was never onboarded - no
credentials for it exist anywhere and it is not a valid provider in
.env.example - on the same page whose body correctly named Polar.
"""
from __future__ import annotations

import sys
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


PAGES = ["render_home", "render_pricing", "render_terms",
         "render_privacy", "render_refund"]


def _render(name):
    from web import pages
    fn = getattr(pages, name)
    return fn(None) if name == "render_checkout" else fn()


@pytest.fixture(params=PAGES + ["render_checkout"])
def page(request):
    return request.param, _render(request.param)


# --------------------------------------------------------------------------
# The seller
# --------------------------------------------------------------------------

def test_every_public_page_names_the_registered_seller(page):
    name, html = page
    assert "Techmate" in html, f"{name} does not name the selling entity"
    assert "1661879" in html, f"{name} does not carry the commercial registration"


def test_no_page_names_an_unregistered_sole_proprietorship(page):
    """The specific defect: a legal form that holds no CR."""
    name, html = page
    assert "sole proprietor" not in html.lower(), (
        f"{name} names a sole proprietorship - Techmate is the registered seller")


def test_no_page_puts_the_founder_personally_on_the_contract(page):
    """His personal name belonged on none of this. A company indemnifies; a
    named individual is personally exposed."""
    name, html = page
    low = html.lower()
    for fragment in ("basil mubarak", "al shukaili", "alshukaili"):
        assert fragment not in low, (
            f"{name} names the founder personally on a public legal page")


def test_the_indemnity_clause_names_the_company(page):
    """Section 9 is the clause that decides who carries a claim."""
    name, html = page
    if "ndemnif" not in html:
        pytest.skip(f"{name} has no indemnification clause")
    i = html.find("ndemnif")
    window = html[i:i + 600]
    assert "Techmate" in window, (
        "the indemnification clause does not name Techmate - the protection "
        "runs to whoever is named here")


# --------------------------------------------------------------------------
# The payment rail
# --------------------------------------------------------------------------

def test_no_page_credits_a_payment_company_we_never_onboarded(page):
    name, html = page
    assert "Paddle" not in html, (
        f"{name} names Paddle as merchant of record; the rail is Polar and "
        f"Paddle was never onboarded")


def test_the_checkout_page_is_consistent_about_who_takes_the_money():
    """It said Polar in the body and a different company in the footer of the
    same page - a buyer could read either one first."""
    html = _render("render_checkout")
    assert "Polar" in html
    assert "Paddle" not in html


# --------------------------------------------------------------------------
# The product is not the company
# --------------------------------------------------------------------------

def test_no_page_claims_the_product_is_an_incorporated_company(page):
    name, html = page
    low = html.lower()
    for claim in ("hatchloop inc", "hatchloop llc", "hatchloop ltd",
                  "agentbroker inc", "agent broker llc",
                  "hatchloop is a company", "hatchloop, a company"):
        assert claim not in low, f"{name} claims {claim!r}"


def test_the_entity_is_overridable_without_a_code_change():
    """It was a bare hardcoded constant while SUPPORT_EMAIL and DOMAIN beside
    it were both env-overridable - so correcting the contracting party needed a
    deploy. A legal identity should not be the least configurable string."""
    from web import _partials
    import importlib
    os.environ["LEGAL_ENTITY"] = "Test Entity Ltd, CR 000"
    try:
        importlib.reload(_partials)
        assert _partials.LEGAL_ENTITY == "Test Entity Ltd, CR 000"
    finally:
        del os.environ["LEGAL_ENTITY"]
        importlib.reload(_partials)
    assert "Techmate" in _partials.LEGAL_ENTITY
