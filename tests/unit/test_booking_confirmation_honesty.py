"""A confirmed booking must mean a confirmed booking.

Reproduced against the real adapter by an adversarial reviewer (2026-09-21),
before any CALCOM_API_KEY reaches production -- the missing credential is
currently the only thing stopping this from firing on a real customer:

  1. reason_code "appointment_confirmed" was returned when Cal.com's booking
     response carried status "pending" -- a request we sent, ACCEPTED BY US,
     not yet accepted by the business. booking.get("status") was never read.

  2. reason_code "appointment_confirmed" was returned when Cal.com's response
     carried NO booking id at all. `booking.get("uid", operation_id)` fell
     back to a locally generated UUID and reported THAT as the appointment
     id -- a booking we cannot even name at the provider, called confirmed.

  3. Both (1) and (2) were charged the full $0.50 per_confirmed_booking fee --
     the MAXIMUM price this operation has -- for an appointment that, as far
     as this process can prove, does not exist.

Fixed in core/schedule_appointment.py: reaching reason_code
"appointment_confirmed" now requires BOTH a real provider booking id AND a
provider status in {"accepted", "confirmed"}. Anything else is its own
distinguishable, uncharged outcome (booking_id_missing /
booking_pending_provider_confirmation), never silently promoted to success.

There is also a related, smaller hazard covered at the bottom of this file:
_choose_slot may pick a slot up to 15 minutes from the caller's preferred
time (see its docstring), and the confirmation used to say only "booked for
<slot time>" with no indication that the slot was not exactly what was
asked for. That shift is now disclosed in the receipt instead of removed
(removing the tolerance entirely would refuse legitimate near-matches; see
test_the_tolerance_is_minutes_not_days in the sibling file).
"""
from __future__ import annotations

import asyncio

import pytest

import core.schedule_appointment as SA
from core.models import ScheduleAppointmentRequest, AppointmentAction, OperationStatus


def _run(coro):
    return asyncio.run(coro)


class _SMB:
    smb_id = "smb_test"
    name = "Test Clinic"
    is_demo = False
    channels_available = ["direct_api:calcom"]
    calcom_event_type_id = "evt_1"
    phone = None
    email = None


class _Adapter:
    """Offers one controllable slot and returns whatever book_slot response
    the test wants -- the exact shape a real (or malfunctioning) Cal.com
    response would hand back, with no HTTP involved."""

    def __init__(self, booking_response, slots=None):
        self.slots = slots if slots is not None else [
            {"start": "2026-09-15T14:00:00.000Z"}]
        self.booking_response = booking_response
        self.booked = None

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.booked = start
        return self.booking_response


@pytest.fixture
def _wired(monkeypatch):
    class _Dir:
        def get(self, smb_id):
            return _SMB()

    monkeypatch.setattr(SA, "get_directory", lambda: _Dir())

    class _Consent:
        def is_opted_out(self, *a, **kw):
            return False

    import compliance.consent_store as cs
    monkeypatch.setattr(cs, "get_consent_store", lambda: _Consent())

    def _install(adapter):
        monkeypatch.setattr(SA, "CalComAdapter", lambda *a, **kw: adapter)
        return adapter

    return _install


def _req(**rt):
    return ScheduleAppointmentRequest(
        smb_id="smb_test", action=AppointmentAction.BOOK, service="checkup",
        customer={"name": "Sara", "email": "sara@example.com"},
        requested_time=rt or None,
    )


# ---------------------------------------------------------------------------
# Defect 1 -- a PENDING provider response must never become "confirmed"
# ---------------------------------------------------------------------------

def test_pending_provider_status_is_never_reported_confirmed(_wired):
    """THE ONE THAT COST MONEY FOR A BOOKING THE BUSINESS NEVER ACCEPTED."""
    _wired(_Adapter({"uid": "bk_pending_1", "status": "pending"}))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code != "appointment_confirmed", (
        f"a PENDING Cal.com booking was reported as {r.reason_code!r}")
    assert r.status != OperationStatus.SUCCESS
    assert r.reason_code == "booking_pending_provider_confirmation"
    assert r.cost.amount == 0.0, (
        f"charged ${r.cost.amount} for a booking the business has not "
        f"accepted")


# ---------------------------------------------------------------------------
# Defect 2 -- no provider id must never become "confirmed" (no local-uuid
# fallback)
# ---------------------------------------------------------------------------

def test_missing_provider_id_is_never_reported_confirmed(_wired):
    """THE ONE WHERE A LOCAL UUID BECAME THE APPOINTMENT ID."""
    _wired(_Adapter({"status": "accepted"}))  # no "uid" at all
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code != "appointment_confirmed", (
        f"a booking with no provider id was reported as {r.reason_code!r}")
    assert r.status != OperationStatus.SUCCESS
    assert r.reason_code == "booking_id_missing"
    assert not (r.result or {}).get("appointment_id") == r.operation_id, (
        "the locally generated operation_id leaked out as the appointment id")
    assert r.cost.amount == 0.0, (
        f"charged ${r.cost.amount} for a booking with no provider id to "
        f"verify it by")


# ---------------------------------------------------------------------------
# Defect 3 -- charge behaviour on both broken paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("booking_response", [
    {"uid": "bk_pending_2", "status": "pending"},
    {"status": "accepted"},  # no uid
])
def test_an_unconfirmed_booking_is_never_charged_the_confirmed_fee(_wired, booking_response):
    _wired(_Adapter(booking_response))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))
    assert r.cost.amount != 0.50, (
        f"quoted the $0.50 per_confirmed_booking fee for {booking_response!r}")
    assert r.cost.amount == 0.0
    assert r.cost.basis != "per_confirmed_booking"


# ---------------------------------------------------------------------------
# Positive controls -- a genuinely accepted booking must still succeed. The
# honesty gate must not swallow real successes along with the fake ones.
# ---------------------------------------------------------------------------

def test_an_accepted_booking_with_a_real_id_is_still_confirmed(_wired):
    _wired(_Adapter({"uid": "bk_real_1", "status": "accepted"}))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code == "appointment_confirmed"
    assert r.status == OperationStatus.SUCCESS
    assert r.result["appointment_id"] == "bk_real_1"
    assert r.cost.amount == 0.50
    assert r.cost.basis == "per_confirmed_booking"


def test_provider_status_is_case_insensitive(_wired):
    """Cal.com's own stub (book_slot with no API key) emits "ACCEPTED"
    uppercase; the documented v2 API is lowercase. Both must be honoured."""
    _wired(_Adapter({"uid": "bk_real_2", "status": "ACCEPTED"}))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))
    assert r.reason_code == "appointment_confirmed"


def test_a_cancelled_or_rejected_provider_status_is_not_confirmed(_wired):
    """Not just "pending" -- any state outside {accepted, confirmed} must be
    refused the same way, including ones Cal.com uses for a dead booking."""
    for bad_status in ("cancelled", "rejected", "", None):
        _wired(_Adapter({"uid": "bk_x", "status": bad_status}))
        r = _run(SA.handle_schedule_appointment(
            _req(preferred_iso="2026-09-15T14:00:00Z")))
        assert r.reason_code != "appointment_confirmed", (
            f"status {bad_status!r} was reported confirmed")
        assert r.cost.amount == 0.0


# ---------------------------------------------------------------------------
# The 15-minute shift hazard: made explicit, not removed.
# ---------------------------------------------------------------------------

def test_a_slot_within_tolerance_discloses_the_shift(_wired):
    """A slot 10 minutes from the request is still booked (within
    _SLOT_TOLERANCE) -- but the receipt must say so, not just report the slot
    time as if it were exactly what was asked for."""
    _wired(_Adapter(
        {"uid": "bk_real_3", "status": "accepted"},
        slots=[{"start": "2026-09-15T14:10:00.000Z"}],
    ))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code == "appointment_confirmed"
    assert r.result["time_shift_minutes"] == pytest.approx(10.0)
    assert r.result["requested_time"] is not None
    assert "10 minute" in r.human_message, (
        f"the time shift was not disclosed: {r.human_message!r}")


def test_an_exact_match_discloses_no_shift(_wired):
    _wired(_Adapter(
        {"uid": "bk_real_4", "status": "accepted"},
        slots=[{"start": "2026-09-15T14:00:00.000Z"}],
    ))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code == "appointment_confirmed"
    assert r.result["time_shift_minutes"] == 0.0
    assert "Note:" not in r.human_message
