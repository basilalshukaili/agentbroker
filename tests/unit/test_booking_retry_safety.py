"""Assignment #8 -- booking retries must be safe.

THE DANGEROUS DEFECT, as described by the technical lead and verified against
the code: core/schedule_appointment.py's Cal.com dispatch wraps book_slot,
cancel_booking, get_availability and the event-type resolution in ONE
`try/except Exception`. Every exception -- a DNS failure that never reached
Cal.com at all, AND a timeout that occurred only after Cal.com had already
accepted the booking -- fell into the SAME branch and produced the SAME
receipt: status=FAILURE, reason_code="calcom_booking_failed", human_message
"Booking via Cal.com did not complete ... Nothing was booked and nothing was
charged."

That message is a confident, false claim whenever the timeout happened AFTER
Cal.com processed the request. A caller (or an agent acting on the receipt)
reads "nothing was booked" as licence to retry -- which is exactly how a real
business gets double-booked and a real customer gets double-charged, since
the request that "failed" may already be sitting in Cal.com's calendar.

THE FIX has two halves:

  1. channels/direct_api/calcom.py's book_slot and cancel_booking now raise a
     NEW exception, BookingOutcomeUnknown, for every failure mode that does
     NOT prove the mutation never happened: a timeout waiting for the
     response (httpx.TimeoutException), a 5xx (Cal.com's own server errored
     AFTER receiving the request), or a 2xx body that could not be parsed
     (Cal.com said it worked and we could not read what it said). A plain
     RuntimeError is still raised -- unchanged -- for the cases that really
     do prove nothing happened: a transport failure before any bytes reached
     Cal.com, or an affirmative 4xx rejection of the request as sent.

  2. core/schedule_appointment.py catches BookingOutcomeUnknown SEPARATELY
     from the generic `except Exception`, before it, and reports a new,
     distinguishable OperationStatus.UNKNOWN outcome: uncharged, NOT
     retriable, and worded to say the outcome is UNKNOWN and that retrying
     risks a duplicate booking/charge -- never "nothing was booked". This
     outcome is also persisted DURABLY (see test_outcome_durability.py) so a
     concurrent or later retry, through the idempotency gate or a plain
     get_status poll, can see this exact state.

Positive controls (must still pass, unchanged): a genuine, certain failure
(the request never reached Cal.com, or Cal.com affirmatively rejected it)
must still be reported as FAILURE/"calcom_booking_failed", saying nothing was
booked -- because for THAT failure mode, that claim is true.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import core.schedule_appointment as SA
from channels.direct_api.calcom import CalComAdapter, BookingOutcomeUnknown
from core.models import ScheduleAppointmentRequest, AppointmentAction, OperationStatus


def _run(coro):
    return asyncio.run(coro)


class _SMB:
    smb_id = "smb_retry_test"
    name = "Retry Safety Clinic"
    is_demo = False
    channels_available = ["direct_api:calcom"]
    calcom_event_type_id = "evt_1"
    phone = None
    email = None


class _RaisingAdapter:
    """Offers one slot; book_slot / cancel_booking raise whatever the test
    hands in, instead of returning a response -- the exact shape of an
    adapter failure, with no HTTP involved."""

    def __init__(self, book_exc=None, cancel_exc=None, slots=None):
        self.slots = slots if slots is not None else [
            {"start": "2026-09-15T14:00:00.000Z"}]
        self._book_exc = book_exc
        self._cancel_exc = cancel_exc
        self.book_calls = 0
        self.cancel_calls: list[str] = []

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.book_calls += 1
        if self._book_exc is not None:
            raise self._book_exc
        return {"uid": "bk_should_not_reach_here", "status": "accepted"}

    async def cancel_booking(self, booking_uid, reason=""):
        self.cancel_calls.append(booking_uid)
        if self._cancel_exc is not None:
            raise self._cancel_exc
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
        smb_id="smb_retry_test", action=AppointmentAction.BOOK,
        customer={"name": "Sara", "email": "sara@example.com"},
        requested_time={"preferred_iso": "2026-09-15T14:00:00Z"},
    )


def _cancel_req(appointment_id="bk_owned_1"):
    return ScheduleAppointmentRequest(
        smb_id="smb_retry_test", action=AppointmentAction.CANCEL,
        existing_appointment_id=appointment_id,
    )


# ---------------------------------------------------------------------------
# THE DEFECT -- reproduced: a timeout AFTER upstream acceptance must never be
# reported as "nothing was booked".
# ---------------------------------------------------------------------------

def test_timeout_after_upstream_acceptance_is_reported_unknown(_wired):
    """THE DANGEROUS CASE. Cal.com may already hold this booking."""
    adapter = _RaisingAdapter(
        book_exc=BookingOutcomeUnknown(
            "Cal.com did not respond in time -- the request may already "
            "have been accepted"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.status == OperationStatus.UNKNOWN, (
        f"an uncertain upstream timeout was reported as {r.status!r}, not UNKNOWN")
    assert r.status not in (OperationStatus.SUCCESS, OperationStatus.FAILURE,
                            OperationStatus.PARTIAL), (
        "an uncertain outcome must be distinguishable from every other status")
    assert r.reason_code == "booking_outcome_unknown"
    assert "nothing was booked" not in (r.human_message or "").lower(), (
        f"the receipt still claims nothing was booked: {r.human_message!r}")
    assert "unknown" in (r.human_message or "").lower()
    assert adapter.book_calls == 1, "book_slot must be called, and only once"


def test_uncertain_outcome_tells_the_caller_not_to_retry_blindly(_wired):
    adapter = _RaisingAdapter(book_exc=BookingOutcomeUnknown("timed out"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.retriable is False, (
        "an uncertain outcome must never advertise itself as safely retriable")
    msg = (r.human_message or "").lower()
    assert "retry" in msg or "verify" in msg, (
        f"the receipt gives no guidance against blind retry: {r.human_message!r}")


def test_uncertain_outcome_is_never_charged(_wired):
    adapter = _RaisingAdapter(book_exc=BookingOutcomeUnknown("timed out"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.cost is not None
    assert r.cost.amount == 0.0, (
        f"charged ${r.cost.amount} for a booking whose outcome is unknown")
    assert r.cost.amount != 0.50, "must never carry the confirmed-booking fee"
    assert r.cost.basis.startswith("no_charge")


def test_a_5xx_after_the_request_was_received_is_also_unknown(_wired):
    """A 5xx from Cal.com's own server after it received the POST -- may
    have partially committed the booking before failing."""
    adapter = _RaisingAdapter(
        book_exc=BookingOutcomeUnknown("Cal.com returned server error 502"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))
    assert r.status == OperationStatus.UNKNOWN
    assert r.cost.amount == 0.0


# ---------------------------------------------------------------------------
# Same treatment for cancellation -- a mutation with a real side effect and
# no undo, same as booking.
# ---------------------------------------------------------------------------

def test_cancellation_timeout_is_reported_unknown_not_a_bare_failure(_wired):
    adapter = _RaisingAdapter(
        cancel_exc=BookingOutcomeUnknown(
            "Cal.com did not respond in time to the cancellation"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(
        _cancel_req("bk_owned_1"), agent_id=None))

    assert r.status == OperationStatus.UNKNOWN
    assert r.reason_code == "cancellation_outcome_unknown"
    assert r.cost.amount == 0.0
    msg = (r.human_message or "").lower()
    assert "did not happen" not in msg
    assert "cancellation" in msg


# ---------------------------------------------------------------------------
# POSITIVE CONTROLS -- a CERTAIN failure (never reached Cal.com, or an
# affirmative rejection) must still say nothing was booked. This is TRUE for
# these cases, so the fix must not swallow them into UNKNOWN too.
# ---------------------------------------------------------------------------

def test_a_definite_transport_failure_is_still_reported_as_failure(_wired):
    """The request never reached Cal.com at all (e.g. DNS/connect failure) --
    a plain RuntimeError, unchanged behaviour."""
    adapter = _RaisingAdapter(book_exc=RuntimeError("Cal.com booking failed: connect error"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == "calcom_booking_failed"
    assert r.cost.amount == 0.0
    assert "nothing was booked" in (r.human_message or "").lower()


def test_an_affirmative_rejection_is_still_reported_as_failure(_wired):
    """A 4xx is Cal.com refusing the request AS SENT -- certain, not uncertain."""
    adapter = _RaisingAdapter(
        book_exc=RuntimeError("Cal.com booking failed: HTTP 422 validation error"))
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.status == OperationStatus.FAILURE
    assert r.cost.amount == 0.0


def test_a_genuinely_accepted_booking_is_still_confirmed_and_charged(_wired):
    """The honesty gate from today's earlier fix must still work: this
    change must not accidentally swallow real successes into UNKNOWN."""
    adapter = _RaisingAdapter(book_exc=None)
    _wired(adapter)
    r = _run(SA.handle_schedule_appointment(_book_req()))

    assert r.status == OperationStatus.SUCCESS
    assert r.reason_code == "appointment_confirmed"
    assert r.cost.amount == 0.50


# ---------------------------------------------------------------------------
# Unit-level proof against the REAL adapter (not the fake): calcom.py itself
# must classify each HTTP failure mode correctly. httpx.MockTransport
# intercepts every request in-process -- no socket is ever opened, no env
# var is set (CALCOM_API_KEY is set directly on the instance, in memory,
# never via os.environ, per this task's hard rule against touching real
# service credentials).
# ---------------------------------------------------------------------------

def _adapter_with_transport(transport: httpx.MockTransport) -> CalComAdapter:
    adapter = CalComAdapter()
    adapter._api_key = "test_key_never_sent_to_a_real_service"
    orig_client = httpx.AsyncClient

    class _PatchedClient(orig_client):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    return adapter, _PatchedClient


def test_adapter_raises_unknown_on_read_timeout(monkeypatch):
    def _handler(request):
        raise httpx.ReadTimeout("simulated timeout", request=request)

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(BookingOutcomeUnknown):
        _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))


def test_adapter_raises_unknown_on_5xx(monkeypatch):
    def _handler(request):
        return httpx.Response(502, text="bad gateway")

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(BookingOutcomeUnknown):
        _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))


def test_adapter_raises_unknown_on_unparseable_2xx_body(monkeypatch):
    def _handler(request):
        return httpx.Response(200, content=b"not json at all")

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(BookingOutcomeUnknown):
        _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))


def test_adapter_raises_plain_runtime_error_on_connect_failure(monkeypatch):
    def _handler(request):
        raise httpx.ConnectError("simulated connect failure", request=request)

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(RuntimeError) as excinfo:
        _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))
    assert not isinstance(excinfo.value, BookingOutcomeUnknown)


def test_adapter_raises_plain_runtime_error_on_4xx_rejection(monkeypatch):
    def _handler(request):
        return httpx.Response(422, text="validation failed")

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(RuntimeError) as excinfo:
        _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))
    assert not isinstance(excinfo.value, BookingOutcomeUnknown)


def test_adapter_book_slot_succeeds_through_a_mock_transport(monkeypatch):
    """Positive control for the adapter-level tests above: a normal 2xx with
    a parseable body must still return the booking, unaffected."""
    def _handler(request):
        return httpx.Response(
            200, json={"data": {"uid": "bk_real", "status": "accepted"}})

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    result = _run(adapter.book_slot("1", "2026-09-15T14:00:00Z", "Sara", "sara@example.com"))
    assert result["uid"] == "bk_real"
    assert result["status"] == "accepted"


def test_adapter_cancel_raises_unknown_on_read_timeout(monkeypatch):
    def _handler(request):
        raise httpx.ReadTimeout("simulated timeout", request=request)

    adapter, patched = _adapter_with_transport(httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", patched)

    with pytest.raises(BookingOutcomeUnknown):
        _run(adapter.cancel_booking("bk_owned_1"))
