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


# Only Terms Section 9 carries the indemnification clause; the marketing and
# checkout pages never did and are not expected to. That absence used to be a
# `pytest.skip` on every one of them, which is indistinguishable in a test
# report from "this could not be checked" - the exact shape of test the
# founder's incident review was worried about. It is checked here instead:
# absence on these five pages is asserted as an EXPECTED, VERIFIED fact, not
# waved through, so a future footer change that quietly duplicates the clause
# onto e.g. checkout is caught rather than skipped past.
#
# POLICY, made explicit (2026-09-22 review): absence on every page outside
# this set IS the requirement, not merely "nothing to check yet". A previous
# version of this test only inferred absence from which `if`/`elif` branch
# python took, then `return`ed with no assert statement in that branch - so
# a stray, but correctly-attributed, copy of Section 9 duplicated onto e.g.
# checkout would pass silently: has_clause would be True, the elif would be
# skipped, and execution would fall through into the content check below,
# which only verifies WHO is named, never WHETHER the clause belongs on this
# page at all. That made the comment above (assured you the duplicate "is
# caught") false. Fixed by asserting the absence directly.
PAGES_EXPECTED_TO_CARRY_THE_CLAUSE = {"render_terms"}


def test_the_indemnity_clause_names_the_company(page):
    """Section 9 is the clause that decides who carries a claim - on every
    page, distinguish three outcomes, each with its own real assertion
    rather than collapsing two of them into an unchecked early return:

      1. no clause here, and none is expected - `assert not has_clause`,
         so a stray duplicate is a hard failure, not a silent pass;
      2. a clause is present and correctly names Techmate - a pass;
      3. a clause is present and names the founder personally instead - the
         exact 2026-08-28 defect this file exists to catch, and it must FAIL
         LOUDLY on ANY page, not only the one page we expect to carry it.
    """
    name, html = page
    has_clause = "ndemnif" in html

    if name in PAGES_EXPECTED_TO_CARRY_THE_CLAUSE:
        assert has_clause, (
            f"{name} is expected to carry the indemnification clause (Terms "
            f"Section 9) and does not - the protection this file guards has "
            f"disappeared from the one page supposed to carry it")
    else:
        assert not has_clause, (
            f"{name} unexpectedly carries an indemnification clause - only "
            f"{sorted(PAGES_EXPECTED_TO_CARRY_THE_CLAUSE)} is expected to, "
            f"and a stray duplicate elsewhere is exactly as dangerous as a "
            f"broken expected one even when correctly attributed, so its "
            f"mere presence here is itself the failure")
        return

    # Reached only for a page that is expected to, and does, carry the
    # clause - now check who it names.
    i = html.find("ndemnif")
    window = html[i:i + 600]
    assert "Techmate" in window, (
        f"{name}'s indemnification clause does not name Techmate - the "
        f"protection runs to whoever is named here")
    low_window = window.lower()
    for fragment in ("basil mubarak", "al shukaili", "alshukaili"):
        assert fragment not in low_window, (
            f"{name}'s indemnification clause names the founder personally "
            f"instead of the company - exactly the 2026-08-28 defect this "
            f"file exists to catch")


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


# --------------------------------------------------------------------------
# The Arabic legal name (found 2026-09-22: the wrong Arabic form was LIVE on
# /terms, /privacy, and /refund while `web/_partials.py` already read
# correctly in the working tree - the fix had never been committed or
# deployed. "Techmate" in LEGAL_ENTITY, above, is the Latin word: it was
# never wrong and passes identically whichever Arabic form ships alongside
# it, so it cannot detect this defect. These checks look at the actual
# Arabic substrings instead, in both the constant AND the rendered output of
# every public page, since a source fix that never reaches production is
# exactly the failure mode this file needs to catch.
# --------------------------------------------------------------------------

# Definite form - "al-Rafiq al-Taqni" ("the Companion, the Technical one").
# The founder's standing rule: TechMate's Arabic name is always this definite
# form, never a transliteration and never the indefinite form below.
CORRECT_ARABIC_NAME = "الرفيق التقني"
# Indefinite/mis-declined form ("Rafiq al-Taqniyah") that was live on the
# contract pages on 2026-09-22. A plain grep on the live HTML found zero
# occurrences of this because the page served it as decimal HTML numeric
# character references, not literal UTF-8 - invisible to a literal search.
WRONG_ARABIC_NAME = "رفيق التقنية"


def test_legal_entity_constant_uses_the_correct_arabic_form():
    """Narrowest check: the source constant itself, independent of any page
    render or env override."""
    from web import _partials
    assert CORRECT_ARABIC_NAME in _partials.LEGAL_ENTITY, (
        "LEGAL_ENTITY default is missing the correct definite Arabic form "
        "of the company name")
    assert WRONG_ARABIC_NAME not in _partials.LEGAL_ENTITY, (
        "LEGAL_ENTITY default contains the wrong (indefinite) Arabic form "
        "of the company name")


def test_every_public_page_carries_the_correct_arabic_name_only(page):
    """The exact 2026-09-22 defect, checked on RENDERED page output (terms,
    privacy, refund, and the footer shared by every page including
    checkout) rather than only on the constant - because the constant can be
    correct while an undeployed/uncommitted fix leaves production wrong."""
    name, html = page
    assert CORRECT_ARABIC_NAME in html, (
        f"{name} does not carry the correct Arabic company name "
        f"({CORRECT_ARABIC_NAME!r}) - the definite form the founder requires")
    assert WRONG_ARABIC_NAME not in html, (
        f"{name} carries the wrong Arabic company name "
        f"({WRONG_ARABIC_NAME!r}) - this is the 2026-09-22 defect")
