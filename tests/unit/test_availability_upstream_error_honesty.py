"""An upstream failure is not "no availability".

Reproduced against the real adapter by an adversarial reviewer (2026-09-21):
channels/direct_api/calcom.py's get_availability() caught a non-200 HTTP
response AND any raised transport exception (timeout, connection failure,
malformed JSON) and turned both into an empty list, exactly like a business
that genuinely has zero open slots. core/schedule_appointment.py's
check_availability action then reported that empty list as
status=SUCCESS / reason_code="availability_returned" / "Found 0 available
slot(s) at <business>" -- and charged the $0.15 per_availability_check fee.

A Cal.com 503 became a confident, false, CHARGED answer to a question the
provider was never actually asked.

Fixed two ways:
  - channels/direct_api/calcom.py: get_availability() now RAISES on a non-200
    response or a transport exception instead of swallowing it into [].
  - core/schedule_appointment.py: check_availability wraps that call in its
    own try/except and reports a distinguishable, uncharged
    reason_code="availability_check_failed" -- never "0 slots".

A genuine zero-slot answer (HTTP 200, no open times) must still come back as
an honest SUCCESS with count 0; the fix must not turn every empty result into
a manufactured failure.
"""
from __future__ import annotations

import asyncio

import pytest

import core.schedule_appointment as SA
from core.models import ScheduleAppointmentRequest, AppointmentAction, OperationStatus


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Layer 1: the adapter itself, against a mocked HTTP transport.
# Never touches a real calendar -- httpx.AsyncClient is monkeypatched.
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _MockHTTPClient:
    """Stands in for httpx.AsyncClient -- an isolated mock HTTP transport.
    No socket is ever opened."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


@pytest.fixture
def _adapter(monkeypatch):
    monkeypatch.setenv("CALCOM_API_KEY", "test-key-not-real")
    from channels.direct_api.calcom import CalComAdapter
    return CalComAdapter()


def test_a_503_response_raises_instead_of_returning_empty(monkeypatch, _adapter):
    """THE ONE THAT TURNED AN OUTAGE INTO A CHARGED, FALSE ANSWER."""
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockHTTPClient(response=_Resp(status_code=503)))

    with pytest.raises(RuntimeError):
        _run(_adapter.get_availability("999", "2026-01-01", "2026-01-04"))


def test_a_transport_failure_raises_instead_of_returning_empty(monkeypatch, _adapter):
    """A timeout / connection failure is the same class of problem as a 503:
    we could not ask, not "the answer is zero"."""
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockHTTPClient(
            raise_exc=httpx.ConnectTimeout("simulated timeout")))

    with pytest.raises(RuntimeError):
        _run(_adapter.get_availability("999", "2026-01-01", "2026-01-04"))


def test_a_genuine_200_with_no_slots_does_not_raise(monkeypatch, _adapter):
    """The fix must not turn every empty result into a manufactured error --
    a real "nothing open" answer is still a normal, successful []."""
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: _MockHTTPClient(
            response=_Resp(status_code=200, payload={"data": {}})))

    slots = _run(_adapter.get_availability("999", "2026-01-01", "2026-01-04"))
    assert slots == []


# ---------------------------------------------------------------------------
# Layer 2: the handler. Uses a fake adapter object (same convention as the
# sibling booking test files) so the failure mode is controlled directly,
# without needing a second HTTP mock for the same assertion.
# ---------------------------------------------------------------------------

class _SMB:
    smb_id = "smb_test"
    name = "Test Clinic"
    is_demo = False
    channels_available = ["direct_api:calcom"]
    calcom_event_type_id = "evt_1"
    phone = None
    email = None


class _FailingAvailabilityAdapter:
    """get_availability raises, exactly as the real adapter now does on a
    503 / timeout / transport failure."""

    def __init__(self, exc):
        self._exc = exc

    async def get_availability(self, event_type_id, date_from, date_to):
        raise self._exc


class _WorkingAvailabilityAdapter:
    def __init__(self, slots):
        self._slots = slots

    async def get_availability(self, event_type_id, date_from, date_to):
        return self._slots


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


def _req():
    return ScheduleAppointmentRequest(
        smb_id="smb_test", action=AppointmentAction.CHECK_AVAILABILITY,
        service="checkup",
    )


def test_an_upstream_failure_is_not_reported_as_zero_slots(_wired):
    """DEFECT 4, at the handler: a 503-shaped failure must never come back as
    a successful, charged "0 slots" answer."""
    _wired(_FailingAvailabilityAdapter(RuntimeError("Cal.com availability lookup failed: HTTP 503")))
    r = _run(SA.handle_schedule_appointment(_req()))

    assert r.reason_code != "availability_returned", (
        "an upstream failure was reported as a real availability answer")
    assert r.status != OperationStatus.SUCCESS
    assert r.reason_code == "availability_check_failed"
    assert "0 available slot" not in (r.human_message or ""), (
        f"an outage was phrased as zero slots: {r.human_message!r}")
    assert r.cost.amount == 0.0, (
        f"charged ${r.cost.amount} for an availability check that never "
        f"reached the provider")


def test_a_genuine_zero_slots_answer_is_still_an_honest_success(_wired):
    """The fix must not overcorrect: a real "nothing open" answer is still
    SUCCESS, still reason_code availability_returned, still charged the
    normal per_availability_check fee -- it is a real answer, just an empty
    one."""
    _wired(_WorkingAvailabilityAdapter([]))
    r = _run(SA.handle_schedule_appointment(_req()))

    assert r.status == OperationStatus.SUCCESS
    assert r.reason_code == "availability_returned"
    assert r.result["count"] == 0
    assert r.cost.amount == 0.15
