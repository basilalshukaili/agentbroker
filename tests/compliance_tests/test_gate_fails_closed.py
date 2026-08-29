"""
Where the gate errs, it must refuse - and the holes it used to allow through.

An external compliance review exercised the LIVE gate on 2026-08-29 and walked
straight through three of them:

  autodialed VOICE marketing to Oman, no consent   -> legal: True
  marketing EMAIL to Oman, no consent              -> legal: True
  marketing EMAIL to country "ZZ", no consent      -> legal: True

`voice_autodialed_requires_prior_express_consent` was defined for all 26
jurisdictions and read by NOTHING. The consent block only ever fired for SMS,
for the GDPR bloc, or for CASL - so every GCC, Asian and Latin American
recipient could be marketed to on voice or email with no opt-in at all,
including Oman, whose PDPL this company advertises on three public pages.

Autodialed telemarketing without prior express consent is the most-litigated
TCPA category there is. For a product whose entire claimed moat is the
compliance layer, an unenforced consent rule is not a missing feature - it is
the product not existing.

Separately: the quiet-hours check swallowed every exception and fell through to
ALLOWED, which is the wrong direction for the only time-of-day protection in
front of marketing voice and SMS. Every other bar here already failed closed;
that one was the exception.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compliance.pre_check import pre_check  # noqa: E402
from core.models import ComplianceViolationError  # noqa: E402


def _marketing(**kw):
    base = dict(recipient_id="+96890000000", channel="voice",
                content="Special offer this week only",
                message_type="marketing", country_code="OM", preview=True)
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# The three holes the review walked through
# --------------------------------------------------------------------------

def test_autodialed_marketing_voice_needs_consent():
    """Live result before this: legal=True, with quiet-hours - which was itself
    failing open - as the only thing in the way."""
    with pytest.raises(ComplianceViolationError) as e:
        pre_check(**_marketing(channel="voice"))
    assert "consent" in str(e.value).lower()


def test_marketing_email_outside_gdpr_and_casl_needs_consent():
    """Oman: not GDPR, not CASL, not CAN-SPAM. The INTERNATIONAL default's own
    docstring promises opt-in and nothing enforced it off the SMS path."""
    with pytest.raises(ComplianceViolationError):
        pre_check(**_marketing(channel="email", recipient_id="a@example.om"))


def test_marketing_to_an_unknown_country_is_refused_not_guessed():
    """With no country the RULES resolved to INTERNATIONAL while the LABEL said
    "US" - two different answers to "which law applies" in one call. Opt-in and
    opt-out regimes reach opposite conclusions on the same message, so guessing
    is not a small error."""
    with pytest.raises(ComplianceViolationError) as e:
        pre_check(**_marketing(channel="email", recipient_id="a@example.com",
                               country_code=None))
    assert "country_code" in str(e.value)


# --------------------------------------------------------------------------
# ...without breaking what was already lawful
# --------------------------------------------------------------------------

def test_can_spam_email_still_sends_without_prior_optin():
    """US commercial email is an OPT-OUT regime (15 U.S.C. 7704): lawful
    without prior consent, given a working unsubscribe.

    My first version of the fix required opt-in everywhere outside GDPR/CASL
    and would have refused every lawful US marketing email - a false BLOCK,
    which is the other way to be wrong. An existing test caught it.
    """
    pre_check(**_marketing(channel="email", recipient_id="a@example.com",
                           country_code="US"))


def test_transactional_messages_are_unaffected():
    """None of this touches transactional traffic - a booking confirmation must
    not start demanding marketing consent."""
    pre_check(recipient_id="+96890000000", channel="email",
              content="Your appointment is confirmed for 3pm.",
              message_type="transactional", country_code="OM", preview=True)


def test_transactional_without_a_country_still_works():
    """The country requirement is scoped to MARKETING, where it decides which
    consent regime applies. Demanding it for a receipt would be friction with
    no legal purpose."""
    pre_check(recipient_id="a@example.com", channel="email",
              content="Your receipt.", message_type="transactional",
              country_code=None, preview=True)


# --------------------------------------------------------------------------
# The fail direction
# --------------------------------------------------------------------------

def test_a_broken_quiet_hours_check_refuses_the_send(monkeypatch):
    """It used to set the result to None and fall through to ALLOWED.

    Quiet-hours is the only time-of-day protection in front of marketing voice
    and SMS, so swallowing its errors removed the last guard - and it was the
    single check in this file that failed open.
    """
    def _boom(*a, **kw):
        raise RuntimeError("timezone database unavailable")

    monkeypatch.setattr("compliance.quiet_hours.check", _boom)
    with pytest.raises(ComplianceViolationError) as e:
        pre_check(recipient_id="+14045550100", channel="sms",
                  content="Your appointment is tomorrow.",
                  message_type="transactional", country_code="US", preview=True)
    assert "could not be evaluated" in str(e.value) or "unavailable" in str(e.value).lower()
