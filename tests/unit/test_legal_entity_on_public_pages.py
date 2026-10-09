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

THE NAME ITSELF (corrected 2026-10-09, read off the CR certificate). The
register holds the company's name in Arabic only, «شركه رفيق التقنية تضامنية»,
legal form joint partnership (شركة تضامنية); TechMate is the Latin spelling.
Until then these pages named "Techmate (شركة الرفيق التقني)" - the BRAND
«الرفيق التقني» with شركة in front - and the checks below asserted that brand
as the correct legal name. The canonical transcription is
projects/profile/lib/site.ts (REGISTERED_NAME_AR), published at
techmate.om/company.
"""
from __future__ import annotations

import html as _html
import sys
import os
import unicodedata

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# The registered name, copied (never retyped) from projects/profile/lib/site.ts
# REGISTERED_NAME_AR: ه in the first word, no article on رفيق, تضامنية without
# the article.
REGISTERED_NAME_AR = "شركه رفيق التقنية تضامنية"
# Forms that must never stand as the legal name on these pages: the brand (and
# with it «شركة الرفيق التقني»), the name retyped with ة or with the article on
# the legal form, and the Latin name in the wrong case.
NOT_THE_LEGAL_NAME = ("الرفيق التقني", "شركة رفيق التقنية", "التضامنية", "Techmate")


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

def _normalize(html):
    """Numeric character references and canonically equivalent Arabic compare
    equal - the 2026-09-22 defect hid in decimal references (below)."""
    return unicodedata.normalize("NFC", _html.unescape(html))


def test_every_public_page_names_the_registered_seller(page):
    name, html = page
    text = _normalize(html)
    assert "TechMate" in text, f"{name} does not name the selling entity"
    assert REGISTERED_NAME_AR in text, (
        f"{name} does not carry the registered name as the register holds it")
    assert "joint partnership" in text, f"{name} does not state the legal form"
    assert "1661879" in text, f"{name} does not carry the commercial registration"


def test_no_page_names_an_unregistered_sole_proprietorship(page):
    """The specific defect: a legal form that holds no CR."""
    name, html = page
    assert "sole proprietor" not in html.lower(), (
        f"{name} names a sole proprietorship - TechMate is the registered seller")


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
      2. a clause is present and correctly names TechMate - a pass;
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
    window = _normalize(html[i:i + 600])
    assert "TechMate" in window and REGISTERED_NAME_AR in window, (
        f"{name}'s indemnification clause does not name TechMate by its "
        f"registered name - the protection runs to whoever is named here")
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
    assert "TechMate" in _partials.LEGAL_ENTITY
    assert REGISTERED_NAME_AR in _partials.LEGAL_ENTITY


# --------------------------------------------------------------------------
# The Arabic legal name. History, kept because it is the lesson: on
# 2026-09-22 the contract pages served «رفيق التقنية» as decimal numeric
# character references (invisible to a literal grep), and this section then
# asserted the BRAND «الرفيق التقني» as the correct legal name. Read off the
# CR certificate on 2026-10-09, the register's name is «شركه رفيق التقنية
# تضامنية» - so the form removed on 2026-09-22 was closer to the register than
# the one put in its place. These checks look at the actual Arabic substrings,
# in both the constant AND the rendered output of every public page, since a
# source fix that never reaches production is exactly the failure mode this
# file needs to catch.
# --------------------------------------------------------------------------


def test_legal_entity_constant_uses_the_registered_name():
    """Narrowest check: the source constant itself, independent of any page
    render or env override."""
    from web import _partials
    entity = _partials.LEGAL_ENTITY
    assert REGISTERED_NAME_AR in entity, (
        "LEGAL_ENTITY default does not carry the registered name as the "
        "register holds it")
    assert "joint partnership" in entity, "LEGAL_ENTITY default omits the legal form"
    for wrong in NOT_THE_LEGAL_NAME:
        assert wrong not in entity, (
            f"LEGAL_ENTITY default carries {wrong!r}, which is not the "
            f"registered name")


def test_every_public_page_carries_the_registered_name_only(page):
    """Checked on RENDERED page output (terms, privacy, refund, and the
    footer shared by every page including checkout) rather than only on the
    constant - because the constant can be correct while an
    undeployed/uncommitted fix leaves production wrong."""
    name, html = page
    text = _normalize(html)
    assert REGISTERED_NAME_AR in text, (
        f"{name} does not carry the registered name ({REGISTERED_NAME_AR!r})")
    for wrong in NOT_THE_LEGAL_NAME:
        assert wrong not in text, (
            f"{name} carries {wrong!r}, which is not the registered name")
