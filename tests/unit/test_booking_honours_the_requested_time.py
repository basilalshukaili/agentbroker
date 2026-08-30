"""A booking must be at the time that was asked for, or not happen.

`preferred_iso` was read NOWHERE in the repo. The handler used only
window_start_iso/window_end_iso and then booked `slots[0]` unconditionally.
Measured by an adversarial reviewer against the real handler:

    requested : 2026-09-15T14:00:00Z
    BOOKED    : 2026-08-30T09:00:00Z   (16 days earlier)
    receipt   : SUCCESS / appointment_confirmed / $0.50 per_confirmed_booking

A real Cal.com booking, in a real customer's name, at a time nobody chose,
reported as confirmed and charged at the maximum booking fee.

The parameter reached the handler as None until dispatch was fixed the same
day, which is what made this reachable - and the dispatch test asserted the
field arrived on the REQUEST OBJECT, which the handler then discarded. That
test passes if the handler deletes the field. This one cannot.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import core.schedule_appointment as SA
from core.models import ScheduleAppointmentRequest, AppointmentAction


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
    """Records what was booked, and offers slots we control."""

    def __init__(self, slots):
        self.slots = slots
        self.booked = None

    async def get_availability(self, event_type_id, date_from, date_to):
        self.window = (date_from, date_to)
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.booked = start
        return {"uid": "bk_1"}


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


def test_a_slot_far_from_the_request_is_never_booked(_wired):
    """THE ONE THAT COST MONEY."""
    adapter = _wired(_Adapter([{"start": "2026-08-30T09:00:00.000Z"}]))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z", duration_minutes=60)))

    assert adapter.booked is None, (
        f"booked {adapter.booked} for a request of 2026-09-15T14:00Z - a real "
        f"appointment at a time nobody asked for")
    assert r.reason_code == "requested_time_unavailable"
    assert r.cost.amount == 0.0, "charged for a booking that did not happen"
    assert "NOT BOOKED" in r.human_message
    assert r.result["booked"] is False
    assert r.result["available_slots"], "no alternatives offered"


def test_the_requested_time_is_booked_when_it_is_available(_wired):
    """The refusal must not cost us the bookings that should succeed."""
    adapter = _wired(_Adapter([
        {"start": "2026-09-15T09:00:00.000Z"},
        {"start": "2026-09-15T14:00:00.000Z"},
    ]))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert adapter.booked == "2026-09-15T14:00:00.000Z", (
        f"booked {adapter.booked}, not the requested 14:00 slot")
    assert r.reason_code == "appointment_confirmed"
    assert r.result["confirmed_time"] == "2026-09-15T14:00:00.000Z"


def test_the_search_window_follows_the_preferred_time(_wired):
    """Before, preferred_iso did not even reach the availability query - the
    window was always "now to now+3 days", so a request three weeks out could
    not have found its own slot even in principle."""
    adapter = _wired(_Adapter([{"start": "2026-09-15T14:00:00.000Z"}]))
    _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))
    date_from, date_to = adapter.window
    assert date_from.startswith("2026-09-15"), (
        f"availability searched from {date_from}, not from the requested day")
    assert date_to > date_from


def test_no_preferred_time_still_books_the_first_slot(_wired):
    """A caller who gives a window and no preference means "anything in here"."""
    adapter = _wired(_Adapter([{"start": "2026-09-16T10:00:00.000Z"}]))
    r = _run(SA.handle_schedule_appointment(_req(
        window_start_iso="2026-09-16T00:00:00Z",
        window_end_iso="2026-09-17T00:00:00Z")))
    assert adapter.booked == "2026-09-16T10:00:00.000Z"
    assert r.reason_code == "appointment_confirmed"


def test_an_inverted_window_says_so_instead_of_blaming_credentials(_wired):
    """window_start_iso and window_end_iso fall back independently, so a start
    with no end produced date_from AFTER date_to. Cal.com 400s, availability
    comes back empty, and the caller was told "CALCOM_API_KEY absent" with
    retriable=False - a cause that was not true, and advice to give up."""
    _wired(_Adapter([]))
    r = _run(SA.handle_schedule_appointment(
        _req(window_start_iso="2026-10-15T09:00:00Z")))
    assert r.reason_code == "bad_request"
    assert "inverted" in r.human_message.lower()
    assert r.cost.amount == 0.0


def test_both_slot_key_spellings_are_understood(_wired):
    """Cal.com has used "start" and "time" across API versions, and our own
    stub emits "start" while the handler read "time" - a KeyError that was
    swallowed and re-emerged as the false no-channel-configured message."""
    for key in ("start", "time"):
        adapter = _wired(_Adapter([{key: "2026-09-15T14:00:00.000Z"}]))
        r = _run(SA.handle_schedule_appointment(
            _req(preferred_iso="2026-09-15T14:00:00Z")))
        assert r.reason_code == "appointment_confirmed", (
            f"a slot keyed {key!r} was not understood")
        assert adapter.booked == "2026-09-15T14:00:00.000Z"


@pytest.mark.parametrize("offset_min,should_book", [
    (0, True), (5, True), (14, True), (16, False), (120, False),
])
def test_the_tolerance_is_minutes_not_days(_wired, offset_min, should_book):
    pref = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
    slot = (pref + timedelta(minutes=offset_min)).isoformat().replace(
        "+00:00", "Z")
    adapter = _wired(_Adapter([{"start": slot}]))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso=pref.isoformat())))
    assert (adapter.booked is not None) is should_book, (
        f"a slot {offset_min} minutes from the request "
        f"{'was not' if should_book else 'was'} booked")
