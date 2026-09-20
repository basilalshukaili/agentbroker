"""The Celery async booking path carried the exact defect pattern that was
just repaired on the SYNCHRONOUS path (core/schedule_appointment.py), left
untouched at the time because exercising it needs a real CELERY_BROKER_URL --
which this test suite must never set. Proven here instead by calling the
task's own function body directly via `.__wrapped__`, the same technique
tests/unit/test_schedule_appointment_ownership.py already uses to reach this
exact task with no broker involved at all.

THE DEFECTS, IN reliability/async_runner.py's `enqueue_booking`:

  1. `booking.get("status")` was never read. A Cal.com PENDING response
     (accepted by us, not yet accepted by the business) was reported
     reason_code "appointment_confirmed" -- identical to the sync-path bug
     fixed in core/schedule_appointment.py and covered there by
     tests/unit/test_booking_confirmation_honesty.py.

  2. `booking.get("uid", operation_id)` fell back to a LOCALLY GENERATED
     UUID whenever Cal.com's response carried no booking id at all -- a
     booking this process cannot even name at the provider, reported
     confirmed with our own made-up id standing in for theirs.

  3. Both of the above were charged a HARDCODED FLAT $1.00 -- not even the
     sync path's own (already-too-high-until-fixed) $0.50 max, a distinct,
     larger, equally wrong number with no basis in billing/pricing.py's
     price table at all.

  4. `slots[0]["time"]` was booked unconditionally -- nothing here read the
     caller's preferred_iso, so an agent asking for 2pm could get a real
     booking at whatever the first open slot happened to be, exactly the
     "16 days earlier" defect _choose_slot exists to prevent on the sync
     path.

Fixed by having the async task apply the SAME honesty gate and the same
`_choose_slot` time-matching the sync path uses -- imported directly from
core.schedule_appointment rather than re-implemented, so the two paths
cannot silently drift apart again -- and by pricing a genuine confirmation
from billing/pricing.py's table (receipt_usd("schedule_appointment",
at_max=True) == $0.50) instead of a hardcoded number.
"""
from __future__ import annotations

import pytest


def _get_async_runner():
    try:
        from reliability import async_runner as ar
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"async_runner not importable: {exc}")
    if not ar.CELERY_AVAILABLE:
        pytest.skip("Celery not available in this environment")
    return ar


class _SMB:
    def __init__(self, smb_id="smb_async_defect", name="Async Defect Clinic",
                 calcom_event_type_id="evt_async_1"):
        self.smb_id = smb_id
        self.name = name
        self.calcom_event_type_id = calcom_event_type_id


class _StubDirectory:
    def __init__(self, smb):
        self._smb = smb

    def get(self, smb_id):
        return self._smb if smb_id == self._smb.smb_id else None


class _Adapter:
    """Same fake-adapter convention as the sync honesty tests: a
    controllable book_slot response, no HTTP involved, plus a call counter
    so a test can prove book_slot was (or crucially, was NOT) invoked."""

    def __init__(self, booking_response, slots=None):
        self.slots = slots if slots is not None else [
            {"time": "2026-09-15T14:00:00.000Z"}]
        self.booking_response = booking_response
        self.book_calls: list[str] = []

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.book_calls.append(start)
        return self.booking_response


def _wire(monkeypatch, smb, adapter):
    monkeypatch.setattr("supply.smb_directory.get_directory",
                         lambda: _StubDirectory(smb))
    monkeypatch.setattr("channels.direct_api.calcom.CalComAdapter",
                         lambda *a, **kw: adapter)
    # The task fires a second Celery task (deliver_webhook.delay) on every
    # terminal write -- that needs a broker exactly as much as this one does,
    # so it is neutralised the same way test_schedule_appointment_ownership.py
    # neutralises it.
    monkeypatch.setattr("reliability.async_runner._fire_webhook",
                         lambda *a, **kw: None)


def _run_task(ar, smb, request_data, agent_id="agent_async_test"):
    return ar.enqueue_booking.__wrapped__(
        f"op_async_{id(request_data)}", request_data, smb.smb_id, agent_id, None)


def _req(preferred_iso=None):
    data = {
        "smb_id": "smb_async_defect",
        "action": "book",
        "customer": {"name": "Priya", "email": "priya@example.test"},
    }
    if preferred_iso:
        data["requested_time"] = {"preferred_iso": preferred_iso}
    return data


# ---------------------------------------------------------------------------
# Defect 1 -- a PENDING provider response must never become "confirmed"
# ---------------------------------------------------------------------------

def test_pending_provider_status_is_never_reported_confirmed(monkeypatch):
    ar = _get_async_runner()
    smb = _SMB()
    adapter = _Adapter({"uid": "bk_async_pending", "status": "pending"})
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req("2026-09-15T14:00:00Z"))

    assert result["reason_code"] != "appointment_confirmed", (
        f"a PENDING Cal.com booking was reported as {result['reason_code']!r}")
    assert result["status"] != "success"
    assert result["cost"]["amount"] == 0.0, (
        f"charged ${result['cost']['amount']} for a booking the business "
        f"has not accepted")


# ---------------------------------------------------------------------------
# Defect 2 -- no provider id must never become "confirmed" via a local
# fallback id
# ---------------------------------------------------------------------------

def test_missing_provider_id_is_never_reported_confirmed(monkeypatch):
    ar = _get_async_runner()
    smb = _SMB()
    adapter = _Adapter({"status": "accepted"})  # no "uid" at all
    _wire(monkeypatch, smb, adapter)

    op_id = "op_async_missing_id_probe"
    result = ar.enqueue_booking.__wrapped__(
        op_id, _req("2026-09-15T14:00:00Z"), smb.smb_id,
        "agent_async_test", None)

    assert result["reason_code"] != "appointment_confirmed", (
        f"a booking with no provider id was reported as "
        f"{result['reason_code']!r}")
    assert result["status"] != "success"
    assert (result.get("result") or {}).get("appointment_id") != op_id, (
        "the locally generated operation_id leaked out as the appointment id")
    assert result["cost"]["amount"] == 0.0


# ---------------------------------------------------------------------------
# Defect 3 -- charge behaviour: never the hardcoded flat $1.00, on either
# broken path or on a genuine success (which must be billing/pricing.py's
# own $0.50 max for this op, not an unrelated hardcoded number).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("booking_response", [
    {"uid": "bk_async_pending_2", "status": "pending"},
    {"status": "accepted"},  # no uid
])
def test_an_unconfirmed_async_booking_is_never_charged_the_flat_dollar(
        monkeypatch, booking_response):
    ar = _get_async_runner()
    smb = _SMB()
    adapter = _Adapter(booking_response)
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req("2026-09-15T14:00:00Z"))

    assert result["cost"]["amount"] != 1.00, (
        f"charged the old hardcoded $1.00 flat fee for {booking_response!r}")
    assert result["cost"]["amount"] == 0.0


def test_a_genuine_confirmation_is_charged_the_real_price_not_the_flat_dollar(
        monkeypatch):
    """Positive control: a real, accepted, provider-id-bearing booking must
    still succeed -- and must be charged billing/pricing.py's actual
    schedule_appointment max ($0.50), never the old unrelated $1.00 flat
    fee."""
    from billing.pricing import receipt_usd

    ar = _get_async_runner()
    smb = _SMB()
    adapter = _Adapter(
        {"uid": "bk_async_real_1", "status": "accepted"},
        slots=[{"time": "2026-09-15T14:00:00.000Z"}],
    )
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req("2026-09-15T14:00:00Z"))

    assert result["status"] == "success", result
    assert result["reason_code"] == "appointment_confirmed"
    assert result["result"]["appointment_id"] == "bk_async_real_1"
    assert result["cost"]["amount"] != 1.00
    assert result["cost"]["amount"] == receipt_usd("schedule_appointment", at_max=True)
    assert result["cost"]["amount"] == 0.50


# ---------------------------------------------------------------------------
# Defect 4 -- no time matching: the caller's preferred_iso must actually be
# honoured, not silently overridden by slots[0].
# ---------------------------------------------------------------------------

def test_a_far_off_slot_is_not_booked_when_a_preferred_time_was_given(monkeypatch):
    """Reproduction: only one slot is offered and it is nowhere near the
    caller's preferred time (16 days earlier, mirroring the sync defect's own
    measured case). The old code booked it anyway. The fix must refuse to
    book a mismatched slot at all."""
    ar = _get_async_runner()
    smb = _SMB()
    far_slot = {"time": "2026-08-30T09:00:00.000Z"}  # ~16 days before pref
    adapter = _Adapter(
        {"uid": "bk_async_wrong_time", "status": "accepted"},
        slots=[far_slot],
    )
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req("2026-09-15T14:00:00Z"))

    assert adapter.book_calls == [], (
        f"a slot {far_slot['time']} outside tolerance of the requested time "
        f"was booked anyway: {adapter.book_calls!r}")
    assert result["reason_code"] != "appointment_confirmed"
    assert result["cost"]["amount"] == 0.0


def test_a_matching_slot_among_several_is_the_one_actually_booked(monkeypatch):
    """The fix must not merely refuse everything -- when a slot within
    tolerance of preferred_iso exists among several offered, THAT slot (not
    slots[0]) must be the one booked."""
    ar = _get_async_runner()
    smb = _SMB()
    slots = [
        {"time": "2026-08-30T09:00:00.000Z"},   # far away, would be slots[0]
        {"time": "2026-09-15T14:05:00.000Z"},   # within tolerance of pref
    ]
    adapter = _Adapter(
        {"uid": "bk_async_matched", "status": "accepted"}, slots=slots)
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req("2026-09-15T14:00:00Z"))

    assert adapter.book_calls == ["2026-09-15T14:05:00.000Z"], adapter.book_calls
    assert result["status"] == "success", result
    assert result["reason_code"] == "appointment_confirmed"


def test_no_preferred_time_still_books_the_first_offered_slot(monkeypatch):
    """Backward-compatible default: a caller that gives no preferred time at
    all still gets slots[0], exactly as before -- this fix must not make an
    ordinary, unconstrained booking request start failing."""
    ar = _get_async_runner()
    smb = _SMB()
    adapter = _Adapter(
        {"uid": "bk_async_default", "status": "accepted"},
        slots=[{"time": "2026-09-20T10:00:00.000Z"}],
    )
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req(preferred_iso=None))

    assert result["status"] == "success", result
    assert result["reason_code"] == "appointment_confirmed"
    assert adapter.book_calls == ["2026-09-20T10:00:00.000Z"]
