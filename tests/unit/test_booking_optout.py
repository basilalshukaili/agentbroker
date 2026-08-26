"""
"Stop" must mean stop on the booking path too.

Every messaging path passes through compliance/pre_check. The booking path
never did (`core/schedule_appointment.py`, `channels/direct_api/calcom.py` do
not import it), so a business that replied STOP could still be booked through
us - and a booking IS contact: Cal.com emails them and someone turns up
(found 2026-08-26).

Honouring an opt-out on one channel while quietly using another is the same
leak fixed in August, wearing a different hat.
"""
from __future__ import annotations

import asyncio

import pytest


def _req(smb_id="smb_test"):
    from core.models import (ScheduleAppointmentRequest, AppointmentAction,
                             CustomerInfo)
    return ScheduleAppointmentRequest(
        smb_id=smb_id,
        action=AppointmentAction.BOOK,
        customer=CustomerInfo(name="Ann", email="ann@example.com"),
    )


class _SMB:
    def __init__(self, phone=None, email=None):
        self.smb_id = "smb_test"
        self.name = "Test Barbers"
        self.phone = phone
        self.email = email
        self.is_demo = False
        self.channels_available = []
        self.calcom_event_type_id = None
        self.active = True


@pytest.fixture
def directory(monkeypatch):
    smb = _SMB(phone="+15551234567", email="shop@example.com")

    class _Dir:
        def get(self, _id):
            return smb

    # Patch the name BOUND IN THE HANDLER, not the source module.
    # core/schedule_appointment.py:17 does `from supply.smb_directory import
    # get_directory`, so the function is already bound in that namespace and
    # patching supply.smb_directory has no effect. These tests passed alone and
    # failed in the full suite until this was corrected - the isolated pass was
    # luck, not proof.
    import core.schedule_appointment as sa
    monkeypatch.setattr(sa, "get_directory", lambda: _Dir())
    return smb


@pytest.fixture
def consent(monkeypatch):
    from compliance.consent_store import ConsentStore
    store = ConsentStore()
    import compliance.consent_store as cs
    monkeypatch.setattr(cs, "get_consent_store", lambda: store)
    return store


def _run(req):
    from core.schedule_appointment import handle_schedule_appointment
    return asyncio.run(handle_schedule_appointment(req))


def test_opted_out_business_is_not_booked(directory, consent):
    consent.mark_opted_out("+15551234567", "sms")
    r = _run(_req())
    assert r.reason_code == "recipient_opted_out"
    assert r.cost.amount == 0.0, "never charge for a booking we refused to make"
    assert r.retriable is False, "an opt-out is not a transient failure"


def test_opt_out_on_any_channel_blocks_the_booking(directory, consent):
    """Someone who said stop by email has not consented to a calendar invite."""
    consent.mark_opted_out("shop@example.com", "email")
    assert _run(_req()).reason_code == "recipient_opted_out"


def test_whatsapp_stop_also_blocks_booking(directory, consent):
    """The STOP we actually receive most often arrives over WhatsApp."""
    consent.mark_opted_out("+15551234567", "whatsapp")
    assert _run(_req()).reason_code == "recipient_opted_out"


def test_a_business_that_never_opted_out_proceeds(directory, consent):
    """The guard must not block ordinary bookings."""
    r = _run(_req())
    assert r.reason_code != "recipient_opted_out"


def test_unreadable_consent_store_fails_CLOSED(directory, monkeypatch):
    """Everywhere else in booking we degrade gracefully. Not here: an
    unreadable consent store is not licence to contact someone who may have
    said stop."""
    import compliance.consent_store as cs

    def _boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(cs, "get_consent_store", _boom)

    r = _run(_req())
    assert r.reason_code == "recipient_opted_out"
    assert r.retriable is True, "unknown state is transient - the caller should retry"
    assert r.cost.amount == 0.0


def test_the_refusal_says_it_applies_network_wide(directory, consent):
    """An agent should learn that switching agents will not get round it."""
    consent.mark_opted_out("+15551234567", "sms")
    msg = _run(_req()).human_message.lower()
    assert "opted out" in msg
    assert "every agent" in msg or "network" in msg
