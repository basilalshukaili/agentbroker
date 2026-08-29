"""Regression tests for the STOP/opt-out enforcement leak (found + fixed 2026-08-24).

The bug: `revoke_consent` no-op'd (returned False, did nothing) when no prior
opt-in record existed - the NORMAL case, since consent records are in-memory and
empty after any restart. `is_opted_out` read ONLY the in-memory records, and the
durable `consent_optouts` table was WRITTEN by handle_inbound but never READ. Net:
after a redeploy a recorded STOP no longer blocked sends -> the "non-bypassable"
compliance gate leaked to opted-out recipients. These tests lock the fix in.
"""
import pytest

from compliance.consent_store import ConsentStore, get_consent_store
from compliance.pre_check import pre_check
from core.models import ComplianceViolationError


def test_revoke_with_no_prior_consent_registers_optout():
    """A STOP from a recipient who never opted in must still register (was the no-op)."""
    store = ConsentStore()
    assert store.is_opted_out("+15551234567", "sms") is False
    ok = store.revoke_consent("+15551234567", "sms", "marketing", "keyword_STOP")
    assert ok is True
    assert store.is_opted_out("+15551234567", "sms") is True


def test_mark_opted_out_email_channel():
    """Email opt-outs (recipient_id is an email) are honored, not just phone."""
    store = ConsentStore()
    store.mark_opted_out("user@example.com", "email")
    assert store.is_opted_out("user@example.com", "email") is True


def test_an_optout_covers_the_person_across_every_channel():
    """A STOP suppresses the CONTACT, not the transport it arrived on.

    This file used to assert the opposite - that a different channel for the
    same recipient was still sendable. That assertion described an impossible
    situation (its example opted an EMAIL ADDRESS out of email, then checked
    SMS to the same email address, which nothing can send) while permitting a
    very possible one: a phone number texts STOP, and we place an autodialed
    marketing call to it the next day.

    That is not what STOP means to the person who sent it, and it is the most
    likely way this gate produces a genuinely angry customer - someone who took
    the trouble to opt out and got phoned anyway.

    Widening the check can only ever SUPPRESS more messages, never release one,
    so it is safe in the only direction that matters here.
    """
    store = ConsentStore()
    store.mark_opted_out("+15551234567", "sms")
    assert store.is_opted_out("+15551234567", "sms") is True
    assert store.is_opted_out("+15551234567", "voice") is True, (
        "a number that texted STOP can still be autodialed")
    assert store.is_opted_out("+15551234567", "whatsapp") is True, (
        "a number that texted STOP can still be WhatsApped")


def test_an_optout_does_not_leak_to_other_people():
    """Identity-scoped, not global. The widening must not suppress anybody
    else's messages."""
    store = ConsentStore()
    store.mark_opted_out("+15551234567", "sms")
    assert store.is_opted_out("+15559999999", "sms") is False
    assert store.is_opted_out("someone@example.com", "email") is False


def test_optout_survives_restart_via_hydration():
    """Simulated restart: a fresh (empty) store hydrated from the durable table blocks."""
    fresh = ConsentStore()
    assert fresh.is_opted_out("+15559999999", "sms") is False
    loaded = fresh.hydrate_opted_out([("+15559999999", "sms"), (None, "sms"), ("+1", "")])
    assert loaded == 1  # the two malformed pairs are skipped
    assert fresh.is_opted_out("+15559999999", "sms") is True


def test_pre_check_blocks_opted_out_recipient_end_to_end():
    """The gate itself (pre_check) must raise for an opted-out recipient."""
    recipient = "+15550001111"
    get_consent_store().mark_opted_out(recipient, "sms")
    with pytest.raises(ComplianceViolationError) as excinfo:
        pre_check(
            recipient_id=recipient,
            channel="sms",
            message_type="transactional",
            content="Your appointment is confirmed for Saturday.",
            country_code="US",
            state_code="CA",
        )
    assert excinfo.value.rule == "recipient_opted_out"
