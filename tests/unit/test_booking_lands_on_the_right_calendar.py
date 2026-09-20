"""A booking must land on the calendar of the business the receipt names.

An imported business stores NO calcom_event_type_id (booking_page_importer
never sets one), so the book / check_availability paths resolved an event type
with `adapter.get_default_event_type_id()` - which lists the event types of the
ONE connected Cal.com account and returns the shortest-duration one. Nothing in
that lookup mentions the business that was asked for.

Measured against the unfixed handler, with a directory entry named
"Brooklyn Emergency Plumbing" whose booking page is https://cal.com/brooklyn-plumbing
and a connected account whose only event types are the founder's:

    requested : smb "Brooklyn Emergency Plumbing"
    BOOKED    : event type 8801 ("founder-intro") on the connected account
    receipt   : SUCCESS / appointment_confirmed / $0.50 per_confirmed_booking
                "Appointment booked at Brooklyn Emergency Plumbing for ..."

A real Cal.com booking, in a real customer's name, on a calendar belonging to
somebody else, reported as confirmed under the name of a business that was
never contacted. The caller is told it succeeded, which is what makes this
worse than not booking at all: nobody goes looking for a booking they were
told they have.

The sibling test file (test_booking_honours_the_requested_time.py) covers WHEN
the booking lands. This one covers WHERE.
"""
from __future__ import annotations

import asyncio

import pytest

import core.schedule_appointment as SA
from channels.direct_api.calcom import CalComAdapter, DestinationNotBound
from core.models import ScheduleAppointmentRequest, AppointmentAction


def _run(coro):
    return asyncio.run(coro)


class _SMB:
    """An imported business: real, non-demo, Cal.com, and NO event type id."""

    def __init__(self, event_type_id=None,
                 website="https://cal.com/brooklyn-plumbing"):
        self.smb_id = "smb_imp_test"
        self.name = "Brooklyn Emergency Plumbing"
        self.is_demo = False
        self.channels_available = ["direct_api:calcom"]
        self.calcom_event_type_id = event_type_id
        self.website = website
        self.phone = None
        self.email = None


class _Adapter:
    """Records what was booked, and answers every event-type question.

    It carries BOTH the old unbound resolver and the bound one so the same
    test runs against the unfixed handler (which calls the former and books)
    and the fixed one (which calls the latter and refuses).
    """

    def __init__(self, slots=None, bound_id=None):
        self.slots = slots if slots is not None else [
            {"start": "2026-09-15T14:00:00.000Z"}]
        self.booked = None
        self.booked_event_type = None
        self.bound_id = bound_id          # None => this destination is unmapped
        self.availability_event_type = None

    async def get_default_event_type_id(self):
        # The connected account's own shortest event type - the value the
        # unfixed handler booked against.
        return "8801"

    async def resolve_event_type_id_for_business(self, booking_url):
        if self.bound_id is None:
            raise DestinationNotBound(
                f"no Cal.com event type on the connected account belongs to "
                f"{booking_url}")
        return self.bound_id

    async def get_availability(self, event_type_id, date_from, date_to):
        self.availability_event_type = event_type_id
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.booked = start
        self.booked_event_type = event_type_id
        # A real provider id AND an accepted status: this file tests WHERE a
        # booking lands, not whether the confirmation itself is honest (that
        # is test_booking_confirmation_honesty.py). Both fields are required
        # by the honesty gate in schedule_appointment.py for a result to be
        # reported "appointment_confirmed" at all.
        return {"uid": "bk_1", "status": "accepted"}


@pytest.fixture
def _wired(monkeypatch):
    """Same wiring as test_booking_honours_the_requested_time, plus a
    per-test SMB so the event-type id and booking URL can vary."""

    def _install(adapter, smb=None):
        smb = smb or _SMB()

        class _Dir:
            def get(self, smb_id):
                return smb

        monkeypatch.setattr(SA, "get_directory", lambda: _Dir())
        monkeypatch.setattr(SA, "CalComAdapter", lambda *a, **kw: adapter)
        return adapter

    class _Consent:
        def is_opted_out(self, *a, **kw):
            return False

    import compliance.consent_store as cs
    monkeypatch.setattr(cs, "get_consent_store", lambda: _Consent())

    return _install


def _req(action=AppointmentAction.BOOK, **rt):
    return ScheduleAppointmentRequest(
        smb_id="smb_imp_test", action=action, service="leak repair",
        customer={"name": "Sara", "email": "sara@example.com"},
        requested_time=rt or None,
    )


# ---------------------------------------------------------------------------
# The one that books a stranger's calendar
# ---------------------------------------------------------------------------

def test_an_unmapped_business_is_never_booked_against_the_connected_account(_wired):
    """THE ONE THAT SENDS A CUSTOMER TO THE WRONG CALENDAR."""
    adapter = _wired(_Adapter(bound_id=None))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert adapter.booked is None, (
        f"booked event type {adapter.booked_event_type} for "
        f"'Brooklyn Emergency Plumbing', which owns no event type on the "
        f"connected account - a real appointment on somebody else's calendar")
    assert r.reason_code == "booking_destination_unmapped"
    assert r.status.value == "failure"
    assert r.cost.amount == 0.0, "charged for a booking that went nowhere"
    assert r.retriable is False, (
        "retrying cannot create a mapping, so retriable=True just repeats the "
        "refusal")
    assert "not booked" in r.human_message.lower()


def test_the_refusal_happens_before_any_mutation(_wired):
    """A destination we cannot bind must fail BEFORE the booking call, not be
    detected afterwards: there is no undo for a Cal.com booking, and the
    business whose calendar it landed on never asked to be on our network."""
    adapter = _wired(_Adapter(bound_id=None))
    _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))
    assert adapter.booked_event_type is None
    assert adapter.availability_event_type is None, (
        "availability was queried against an event type we had not bound - "
        "the resolution must come first so nothing downstream can use it")


def test_check_availability_does_not_report_a_stranger_calendar(_wired):
    """check_availability mutates nothing, but "Found 3 available slot(s) at
    Brooklyn Emergency Plumbing" is a false statement about a business we
    hold no calendar for, and the agent books on the strength of it."""
    adapter = _wired(_Adapter(bound_id=None))
    r = _run(SA.handle_schedule_appointment(
        _req(action=AppointmentAction.CHECK_AVAILABILITY)))

    assert r.reason_code == "booking_destination_unmapped"
    assert "available slot" not in r.human_message.lower()
    assert r.cost.amount == 0.0


# ---------------------------------------------------------------------------
# The refusal must not cost us the bookings that are genuinely ours
# ---------------------------------------------------------------------------

def test_an_explicit_per_smb_event_type_is_still_booked(_wired):
    """A directory entry that carries its own calcom_event_type_id IS the
    mapping. Nothing about it is ambiguous and it must keep working."""
    adapter = _wired(_Adapter(), smb=_SMB(event_type_id="1011"))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code == "appointment_confirmed"
    assert adapter.booked_event_type == "1011"


def test_a_business_that_owns_a_connected_event_type_is_still_booked(_wired):
    """When the resolver CAN bind the booking URL to an event type on the
    connected account, that is the right calendar and the booking proceeds."""
    adapter = _wired(_Adapter(bound_id="7701"),
                     smb=_SMB(website="https://cal.com/hatchloop/30min"))
    r = _run(SA.handle_schedule_appointment(
        _req(preferred_iso="2026-09-15T14:00:00Z")))

    assert r.reason_code == "appointment_confirmed"
    assert adapter.booked_event_type == "7701"
    assert r.result["calcom_event_type_id"] == "7701", (
        "the receipt must name the calendar it actually booked, so a wrong "
        "destination is visible in the receipt instead of only in Cal.com")


def test_cancel_never_needs_a_destination_binding(_wired):
    """cancel targets an existing booking uid. Resolving an event type it does
    not use would turn every cancellation on an unmapped business into a
    refusal - and a customer who cannot cancel still turns up."""

    class _CancelAdapter(_Adapter):
        def __init__(self):
            super().__init__(bound_id=None)
            self.cancelled = None

        async def cancel_booking(self, uid, reason=""):
            self.cancelled = uid
            return {"status": "CANCELLED", "uid": uid}

    adapter = _wired(_CancelAdapter())
    req = ScheduleAppointmentRequest(
        smb_id="smb_imp_test", action=AppointmentAction.CANCEL,
        service="leak repair", existing_appointment_id="bk_1",
    )
    r = _run(SA.handle_schedule_appointment(req))
    assert r.reason_code == "cancelled"
    assert adapter.cancelled == "bk_1"


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------

_CONNECTED_EVENT_TYPES = [
    {"id": 8801, "slug": "founder-intro", "lengthInMinutes": 15},
    {"id": 7701, "slug": "30min", "lengthInMinutes": 30},
    {"id": 7702, "slug": "consult", "lengthInMinutes": 60},
]


@pytest.fixture
def _adapter(monkeypatch):
    monkeypatch.setenv("CALCOM_API_KEY", "test-key-not-used")
    monkeypatch.setenv("CALCOM_USERNAME", "hatchloop")
    a = CalComAdapter()

    async def _types():
        return list(_CONNECTED_EVENT_TYPES)

    monkeypatch.setattr(a, "get_event_types", _types)
    return a


def test_a_url_on_another_handle_does_not_bind(_adapter):
    """The whole defect in one assertion: the connected account cannot book
    for a business it does not host."""
    with pytest.raises(DestinationNotBound):
        _run(_adapter.resolve_event_type_id_for_business(
            "https://cal.com/brooklyn-plumbing"))


def test_the_event_slug_in_the_url_wins_over_the_shortest_event(_adapter):
    """Shortest-duration was the old heuristic. When the URL names the event,
    the URL is the answer - booking "founder-intro" for someone who asked for
    /30min is the same class of substitution, one calendar further in."""
    got = _run(_adapter.resolve_event_type_id_for_business(
        "https://cal.com/hatchloop/30min"))
    assert got == "7701", f"bound {got}, not the /30min event type"


def test_a_handle_only_url_binds_to_the_account(_adapter):
    """cal.com/<handle> with no event slug is the shape the manifest
    advertises. The handle matches the connected account, so the calendar is
    right by construction and the shortest event type is a safe default."""
    got = _run(_adapter.resolve_event_type_id_for_business(
        "https://cal.com/hatchloop"))
    assert got == "8801"


def test_an_event_slug_the_account_does_not_have_does_not_bind(_adapter):
    """Right account, wrong service. Falling back to any other event type
    would book a 15-minute intro for an emergency call-out."""
    with pytest.raises(DestinationNotBound):
        _run(_adapter.resolve_event_type_id_for_business(
            "https://cal.com/hatchloop/emergency-callout"))


def test_no_booking_url_at_all_does_not_bind(_adapter):
    """An entry with no website carries no evidence of a destination, so
    there is nothing to bind and nothing to guess from."""
    for url in (None, "", "   "):
        with pytest.raises(DestinationNotBound):
            _run(_adapter.resolve_event_type_id_for_business(url))


def test_a_team_url_does_not_bind_to_a_personal_handle(_adapter):
    """cal.com/team/<slug>/<event> is a different namespace; "team" is not a
    username, and matching on the first path segment would bind every team
    URL on the platform to our own account."""
    with pytest.raises(DestinationNotBound):
        _run(_adapter.resolve_event_type_id_for_business(
            "https://cal.com/team/hatchloop/30min"))


def test_a_non_calcom_url_does_not_bind(_adapter):
    """Only a Cal.com page can name a Cal.com event type. A Calendly or
    Doctolib page imported with calcom in its channel list must not resolve."""
    with pytest.raises(DestinationNotBound):
        _run(_adapter.resolve_event_type_id_for_business(
            "https://calendly.com/hatchloop/30min"))
