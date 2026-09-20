"""Assignment #8 -- the SAME uncertain-outcome defect as
tests/unit/test_booking_retry_safety.py, found on the ASYNC (Celery) booking
path while fixing the sync one, and arguably more dangerous there.

reliability/async_runner.py's `enqueue_booking` wraps its whole body in one
`try/except Exception`, exactly like the sync path did before today's fix.
But this branch does not stop at a false "nothing was booked" claim -- it
also does:

    raise self.retry(exc=exc, countdown=30)

which is Celery's OWN automatic retry mechanism (max_retries=3 on the task
decorator). So a timeout that happens AFTER Cal.com accepted the booking
used to be reported "execution_failure" / retriable=True AND Celery would
automatically re-run this exact task with no human or agent in the loop --
compounding the double-booking hazard into an AUTOMATIC one instead of
merely inviting a caller to retry.

Fixed the same way as the sync path: channels/direct_api/calcom.py's
BookingOutcomeUnknown is caught separately, BEFORE the generic
`except Exception`, reported as its own terminal, uncharged, NOT-retriable
outcome (status "unknown", reason_code "booking_outcome_unknown") -- and,
critically, WITHOUT calling self.retry, so Celery never automatically
re-executes it.

Uses the exact same no-broker technique tests/unit/test_async_booking_defect.py
already established: calling the task's own function body via
`.__wrapped__`, which needs no CELERY_BROKER_URL and touches no network.
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
    def __init__(self, smb_id="smb_async_retry_safety", name="Async Retry Safety Clinic",
                 calcom_event_type_id="evt_async_retry_1"):
        self.smb_id = smb_id
        self.name = name
        self.calcom_event_type_id = calcom_event_type_id


class _StubDirectory:
    def __init__(self, smb):
        self._smb = smb

    def get(self, smb_id):
        return self._smb if smb_id == self._smb.smb_id else None


class _RaisingAdapter:
    def __init__(self, book_exc):
        self.slots = [{"time": "2026-09-15T14:00:00.000Z"}]
        self._book_exc = book_exc
        self.book_calls: list[str] = []

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        self.book_calls.append(start)
        raise self._book_exc


def _wire(monkeypatch, smb, adapter):
    monkeypatch.setattr("supply.smb_directory.get_directory",
                         lambda: _StubDirectory(smb))
    monkeypatch.setattr("channels.direct_api.calcom.CalComAdapter",
                         lambda *a, **kw: adapter)
    monkeypatch.setattr("reliability.async_runner._fire_webhook",
                         lambda *a, **kw: None)


def _run_task(ar, smb, request_data, agent_id="agent_async_retry_test"):
    return ar.enqueue_booking.__wrapped__(
        f"op_async_retry_{id(request_data)}", request_data, smb.smb_id, agent_id, None)


def _req(preferred_iso="2026-09-15T14:00:00Z"):
    return {
        "smb_id": "smb_async_retry_safety",
        "action": "book",
        "customer": {"name": "Sara", "email": "sara@example.test"},
        "requested_time": {"preferred_iso": preferred_iso},
    }


def test_async_timeout_after_upstream_acceptance_is_reported_unknown_not_retried(monkeypatch):
    """THE DANGEROUS CASE, async flavour. Must return cleanly with an
    UNKNOWN outcome -- and must NEVER reach self.retry (which would mean
    Celery automatically re-executing book_slot a second time)."""
    from channels.direct_api.calcom import BookingOutcomeUnknown

    ar = _get_async_runner()
    smb = _SMB()
    adapter = _RaisingAdapter(
        BookingOutcomeUnknown("simulated timeout after Cal.com may have accepted"))
    _wire(monkeypatch, smb, adapter)

    # If the fix regressed and fell back into the generic `except Exception`
    # branch, this call would raise (self.retry constructs/raises Celery's
    # own Retry exception) instead of returning -- so simply completing
    # without raising is itself part of the proof.
    result = _run_task(ar, smb, _req())

    assert result["status"] == "unknown", (
        f"an uncertain upstream timeout was reported as {result['status']!r}, "
        f"not 'unknown'")
    assert result["reason_code"] == "booking_outcome_unknown"
    assert result["retriable"] is False
    assert result["cost"]["amount"] == 0.0
    assert "nothing was booked" not in result["human_message"].lower()
    assert adapter.book_calls == ["2026-09-15T14:00:00.000Z"], (
        "book_slot must be called, and only once by this task body")


def test_async_uncertain_outcome_is_never_charged(monkeypatch):
    from channels.direct_api.calcom import BookingOutcomeUnknown

    ar = _get_async_runner()
    smb = _SMB()
    adapter = _RaisingAdapter(BookingOutcomeUnknown("simulated 5xx after receipt"))
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req())
    assert result["cost"]["amount"] == 0.0
    assert result["cost"]["amount"] != 1.00, "must never carry the old flat hardcoded fee"


def test_async_uncertain_outcome_is_durably_stored(monkeypatch):
    """The Celery worker runs in its own process; get_status/get_outcome
    from the API-server process must still be able to see this state."""
    from channels.direct_api.calcom import BookingOutcomeUnknown
    from storage.outcome_store import get_outcome_store

    ar = _get_async_runner()
    smb = _SMB()
    adapter = _RaisingAdapter(BookingOutcomeUnknown("simulated timeout"))
    _wire(monkeypatch, smb, adapter)

    result = _run_task(ar, smb, _req())
    stored = get_outcome_store().get(result["operation_id"])
    assert stored is not None
    assert stored["outcome"]["status"] == "unknown"


def test_async_a_genuine_certain_failure_still_retries_via_celery(monkeypatch):
    """POSITIVE CONTROL: a certain, definite failure (never reached Cal.com,
    or Cal.com definitively rejected it) is a plain RuntimeError, unchanged
    -- it must still fall into the generic `except Exception` / self.retry
    path, not the new UNKNOWN one. Outside a real worker/broker context,
    self.retry(exc=exc, ...) surfaces as the ORIGINAL exception propagating
    (no broker to hand the retry to) -- this pins that this is still what
    happens for a certain failure, unchanged by this fix."""
    ar = _get_async_runner()
    smb = _SMB()
    adapter = _RaisingAdapter(RuntimeError("Cal.com booking failed: connect error"))
    _wire(monkeypatch, smb, adapter)

    with pytest.raises(RuntimeError, match="connect error"):
        _run_task(ar, smb, _req())
