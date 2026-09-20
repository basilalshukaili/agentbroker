"""Caller B must not be able to cancel caller A's booking.

THE HOLE, AS FOUND. core/schedule_appointment.py's cancel branch called

    result = await adapter.cancel_booking(request.existing_appointment_id or "")

directly -- the SAME kind of upstream mutation a real Cal.com booking makes,
with a real side effect and no undo -- with no check anywhere that the caller
asking for the cancellation is the same one who created the booking. The
only guard on the cancel path was the existing "existing_appointment_id must
be present" input check (line ~134); once that passed, ANY caller holding
(or guessing) a Cal.com booking uid could cancel ANY business's appointment
for ANY other agent's customer.

This is the exact same shape of hole tests/unit/test_schedule_appointment_ownership.py
found and closed for READS of the operation receipt (get_status / get_outcome):
an authenticated caller's identity has to survive to the point that matters,
or "authenticated" means nothing. core/ownership.py's `read_denial` is that
fix, already proven correct for reads. This file reuses it -- unchanged --
for the cancellation MUTATION, rather than inventing a second ownership
mechanism: the question ("does this caller match the recorded owner, and
what do we do when the owner is unknown") is identical, only the subject
changes from "operation receipt" to "appointment".

The lookup this needed did not exist: OutcomeStore had no way to find who
owns a CONFIRMED booking given only the provider's own booking id (the only
identifier a canceller ever holds -- never our internal operation_id). Added
as OutcomeStore.get_appointment_owner_async, checked in-memory first (same
process) then durably (storage/outcome_store.py's existing Supabase fallback
pattern, extended with an `appointment_id` column so a cross-process cancel
resolves too).

Fail-closed, matching read_denial's own policy: an appointment we cannot
attribute to anyone is refused to everyone, not granted to whoever asks
first -- the same reasoning that already governs an unowned operation
receipt (see TestHistoricalUnownedRecordsFailClosed in the ownership test
file). The alternative -- trusting whichever caller merely CLAIMS an
appointment_id -- is not a smaller version of the hole, it is the hole.

Also covered here: an unsupported appointment action (`reschedule` is a
declared AppointmentAction with no handling branch in
handle_schedule_appointment) must fail WITHOUT ever reaching an upstream
adapter call -- not attempt a mutation and apologise afterwards.
"""
from __future__ import annotations

import asyncio

import pytest

import core.schedule_appointment as SA
from core.models import (
    ScheduleAppointmentRequest, AppointmentAction, OperationStatus,
)


def _run(coro):
    return asyncio.run(coro)


class _SMB:
    smb_id = "smb_cancel_test"
    name = "Test Clinic"
    is_demo = False
    channels_available = ["direct_api:calcom"]
    calcom_event_type_id = "evt_1"
    phone = None
    email = None


class _Adapter:
    """Same convention as test_booking_confirmation_honesty.py's fake
    adapter, extended with a spy on cancel_booking so tests can prove the
    upstream mutation was (or, in the denied cases, was NEVER) invoked."""

    def __init__(self, booking_response=None, slots=None):
        self.slots = slots if slots is not None else [
            {"start": "2026-09-15T14:00:00.000Z"}]
        self.booking_response = booking_response or {
            "uid": "bk_owned_1", "status": "accepted"}
        self.cancel_calls: list[str] = []
        self.book_calls = 0

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.book_calls += 1
        return self.booking_response

    async def cancel_booking(self, booking_uid, reason=""):
        self.cancel_calls.append(booking_uid)
        return {"uid": booking_uid, "status": "cancelled"}


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


def _book_req():
    return ScheduleAppointmentRequest(
        smb_id="smb_cancel_test", action=AppointmentAction.BOOK,
        customer={"name": "Sara", "email": "sara@example.com"},
        requested_time={"preferred_iso": "2026-09-15T14:00:00Z"},
    )


def _cancel_req(appointment_id: str) -> ScheduleAppointmentRequest:
    return ScheduleAppointmentRequest(
        smb_id="smb_cancel_test", action=AppointmentAction.CANCEL,
        existing_appointment_id=appointment_id,
    )


A, B = "agent_alice_cancel", "agent_mallory_cancel"


def _book_as(_wired, agent_id, uid="bk_owned_1"):
    adapter = _Adapter({"uid": uid, "status": "accepted"})
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req(), agent_id=agent_id))
    assert r.reason_code == "appointment_confirmed", r
    return r.result["appointment_id"], adapter


# ---------------------------------------------------------------------------
# THE DEFECT — reproduced: caller B cancelling caller A's real booking.
# ---------------------------------------------------------------------------

class TestCrossCallerCancellationIsDenied:
    def test_caller_b_cannot_cancel_caller_as_booking(self, _wired):
        appt_id, _booking_adapter = _book_as(_wired, A)

        cancel_adapter = _Adapter()
        _wired(cancel_adapter)
        r = _run(SA.handle_schedule_appointment(
            _cancel_req(appt_id), agent_id=B))

        assert r.status != OperationStatus.SUCCESS, (
            "caller B was allowed to cancel caller A's appointment")
        assert r.reason_code == "not_your_appointment", r.reason_code
        assert cancel_adapter.cancel_calls == [], (
            "adapter.cancel_booking (the upstream mutation) was invoked "
            "for an unauthorized cancellation — the denial must happen "
            "BEFORE the mutation, not instead of one that already ran")
        assert r.cost.amount == 0.0

    def test_anonymous_caller_cannot_cancel_caller_as_booking(self, _wired):
        appt_id, _ = _book_as(_wired, A)

        cancel_adapter = _Adapter()
        _wired(cancel_adapter)
        r = _run(SA.handle_schedule_appointment(
            _cancel_req(appt_id), agent_id="anonymous"))

        assert r.status != OperationStatus.SUCCESS
        assert r.reason_code in ("not_your_appointment", "identity_required")
        assert cancel_adapter.cancel_calls == []
        assert r.cost.amount == 0.0

    def test_an_appointment_id_with_no_recorded_owner_is_denied_to_everyone(self, _wired):
        """Fail closed: a booking uid this store never confirmed (fabricated,
        or genuinely lost) must not be cancellable just because a caller
        asserts a matching agent_id — same policy as an unowned operation
        receipt (read_denial's own rule), reused here unchanged."""
        cancel_adapter = _Adapter()
        _wired(cancel_adapter)
        r = _run(SA.handle_schedule_appointment(
            _cancel_req("bk_never_confirmed_by_us"), agent_id=A))

        assert r.status != OperationStatus.SUCCESS
        assert r.reason_code == "appointment_owner_unknown", r.reason_code
        assert cancel_adapter.cancel_calls == []
        assert r.cost.amount == 0.0


# ---------------------------------------------------------------------------
# POSITIVE CONTROL — the fix must not merely refuse everyone. The true owner
# must still be able to cancel their own booking, and the upstream mutation
# must actually run for them.
# ---------------------------------------------------------------------------

class TestOwnerCanStillCancelItsOwnBooking:
    def test_the_booking_owner_can_cancel_its_own_appointment(self, _wired):
        appt_id, _ = _book_as(_wired, A)

        cancel_adapter = _Adapter()
        _wired(cancel_adapter)
        r = _run(SA.handle_schedule_appointment(
            _cancel_req(appt_id), agent_id=A))

        assert r.status == OperationStatus.SUCCESS, r
        assert r.reason_code == "cancelled"
        assert cancel_adapter.cancel_calls == [appt_id], (
            "the legitimate owner's cancellation never reached the "
            "provider — the fix must not just deny everyone")

    def test_two_different_agents_each_own_their_own_bookings(self, _wired):
        """Not a global lock — A's ownership must not block B from managing
        B's own, separate appointment."""
        appt_a, _ = _book_as(_wired, A, uid="bk_owned_a")
        appt_b, _ = _book_as(_wired, B, uid="bk_owned_b")

        cancel_adapter = _Adapter()
        _wired(cancel_adapter)
        r_a = _run(SA.handle_schedule_appointment(_cancel_req(appt_a), agent_id=A))
        r_b = _run(SA.handle_schedule_appointment(_cancel_req(appt_b), agent_id=B))

        assert r_a.status == OperationStatus.SUCCESS
        assert r_b.status == OperationStatus.SUCCESS
        assert sorted(cancel_adapter.cancel_calls) == sorted([appt_a, appt_b])


# ---------------------------------------------------------------------------
# UNSUPPORTED ACTION — must fail before any upstream mutation, not after.
# `reschedule` is declared in AppointmentAction but has no handling branch.
# ---------------------------------------------------------------------------

class TestUnsupportedActionNeverReachesTheProvider:
    def test_reschedule_never_calls_book_slot_or_cancel_booking(self, _wired):
        appt_id, _ = _book_as(_wired, A)

        adapter = _Adapter()
        _wired(adapter)
        req = ScheduleAppointmentRequest(
            smb_id="smb_cancel_test", action=AppointmentAction.RESCHEDULE,
            existing_appointment_id=appt_id,
        )
        r = _run(SA.handle_schedule_appointment(req, agent_id=A))

        assert r.status != OperationStatus.SUCCESS or r.cost.amount == 0.0, (
            "an unsupported action must never be charged")
        assert adapter.cancel_calls == [], (
            "reschedule (unimplemented) reached adapter.cancel_booking")
        assert adapter.book_calls == 0, (
            "reschedule (unimplemented) reached adapter.book_slot")
        assert r.cost.amount == 0.0
