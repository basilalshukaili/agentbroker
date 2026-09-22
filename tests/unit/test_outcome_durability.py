"""Assignment #8 -- a booking receipt that says SUCCESS must be provably
retrievable by another process, or by this same process after a restart.

THE DEFECT. storage/outcome_store.py's set_complete() persists to Supabase
via `_fire_persist`, which does:

    if loop.is_running():
        asyncio.ensure_future(_supabase_upsert(...))   # fire-and-forget

Nothing ever awaits that task, and nothing checks whether it finished. In a
short-lived context (a single asyncio.run() call, or a process that is
killed shortly after returning the receipt), asyncio's own shutdown
machinery cancels any task still in flight -- if it is genuinely awaiting
something (real network I/O against Supabase), the write is lost with NO
trace it was ever attempted. The caller was already told "Appointment
booked" (and, on this path, already charged); a different process asking
get_status/get_outcome for the same operation_id -- a horizontally scaled
peer, a monitoring job, this SAME process after an automatic restart, or the
idempotency gate deciding whether a retry is safe -- sees "operation not
found" for a real appointment.

THE FIX: OutcomeStore.set_complete_durable() AWAITS the Supabase write (with
a bounded timeout) instead of firing it and hoping, and reports whether it
actually landed. core/schedule_appointment.py calls it (via _store_terminal's
new `durable=True`) only for the outcomes where losing the write matters: a
CONFIRMED booking, a booking pending the provider's own confirmation, a
CANCELLATION that ran, and the new UNKNOWN outcome (test_booking_retry_safety.py).
Every other terminal receipt on this path keeps the cheap fire-and-forget
write, unchanged.

These tests use a fake in-process "durable store" (a plain dict) in place of
Supabase, with a REAL suspension point (asyncio.sleep) in the fake write --
enough to be cancelled by asyncio.run()'s shutdown if nothing awaits it, and
enough to prove the fix actually waits for it.
"""
from __future__ import annotations

import asyncio

import pytest

import core.schedule_appointment as SA
import storage.supabase_client as sb
from core.models import ScheduleAppointmentRequest, AppointmentAction, OperationStatus
from storage.outcome_store import OutcomeStore, get_outcome_store


def _run(coro):
    return asyncio.run(coro)


class _SMB:
    smb_id = "smb_durability_test"
    name = "Durability Test Clinic"
    is_demo = False
    channels_available = ["direct_api:calcom"]
    calcom_event_type_id = "evt_1"
    phone = None
    email = None


class _Adapter:
    def __init__(self, booking_response, slots=None):
        self.slots = slots if slots is not None else [
            {"start": "2026-09-15T14:00:00.000Z"}]
        self.booking_response = booking_response

    async def get_availability(self, event_type_id, date_from, date_to):
        return self.slots

    async def book_slot(self, event_type_id, start, name, email, notes=None):
        return self.booking_response

    async def cancel_booking(self, booking_uid, reason=""):
        raise AssertionError("not exercised in this file")


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
        smb_id="smb_durability_test", action=AppointmentAction.BOOK,
        customer={"name": "Priya", "email": "priya@example.com"},
        requested_time={"preferred_iso": "2026-09-15T14:00:00Z"},
    )


@pytest.fixture(autouse=True)
def _clear_store():
    get_outcome_store()._records.clear()
    yield
    get_outcome_store()._records.clear()


def _fake_remote_upsert(remote_db: dict, delay: float = 0.02):
    """A fake `storage.supabase_client.upsert_row` with a REAL suspension
    point -- realistic enough that asyncio.run()'s shutdown will cancel it
    if nothing awaits it, which is exactly the failure mode being proven.

    Kept for signature-compatibility with older callers; the live code path
    (board row 206) no longer calls upsert_row for `operations` -- see
    _fake_rpc below, which is what actually stands in for it now."""
    async def _upsert(table, row, on_conflict="id"):
        await asyncio.sleep(delay)
        remote_db[row["operation_id"]] = dict(row)
        return dict(row)
    return _upsert


def _fake_remote_select(remote_db: dict):
    """Stands in for `select_rows_strict`. Kept for signature-compatibility;
    the live code path (board row 206) no longer calls it for `operations`
    -- see _fake_rpc below."""
    async def _select(table, filters=None, limit=1000, order=None, gte=None):
        op_id = (filters or {}).get("operation_id")
        row = remote_db.get(op_id)
        return [row] if row else []
    return _select


def _fake_rpc(remote_db: dict, delay: float = 0.02):
    """Stands in for `storage.supabase_client.rpc` for the three
    `operations_*` SECURITY DEFINER functions (board row 206,
    sql/agentbroker/001_operations_security_definer_rpc.sql) --
    storage/outcome_store.py's _supabase_fetch / _supabase_upsert /
    _supabase_fetch_by_appointment_id call these instead of
    select_rows_strict/upsert_row directly now that this service deploys
    with only the Supabase anon key. Keeps the SAME `remote_db` dict shape
    _fake_remote_upsert/_fake_remote_select used, and the SAME real
    suspension point on the write (`delay`) -- this is what
    test_confirmed_booking_is_durably_persisted_before_the_call_returns
    actually depends on to prove the write is awaited, not fired and
    forgotten."""
    async def _rpc(fn, payload):
        if fn == "operations_upsert":
            await asyncio.sleep(delay)
            row = {
                "operation_id": payload["p_operation_id"],
                "tool": payload["p_tool"],
                "status": payload["p_status"],
                "reason_code": payload["p_reason_code"],
                "appointment_id": payload["p_appointment_id"],
                "result_json": payload["p_result_json"],
                "agent_id": payload["p_agent_id"],
            }
            remote_db[row["operation_id"]] = row
            return dict(row)
        if fn == "operations_get_by_id":
            row = remote_db.get(payload["p_operation_id"])
            return dict(row) if row else None
        if fn == "operations_get_by_appointment_id":
            for row in remote_db.values():
                if (row.get("appointment_id") == payload.get("p_appointment_id")
                        and row.get("reason_code") == "appointment_confirmed"):
                    return dict(row)
            return None
        raise AssertionError(f"unexpected rpc fn in test fake: {fn!r}")
    return _rpc


# ---------------------------------------------------------------------------
# DEFECT 3 -- reproduced: a confirmed booking must land durably BEFORE the
# call returns, not at the mercy of the event loop's shutdown cancellation.
# ---------------------------------------------------------------------------

def test_confirmed_booking_is_durably_persisted_before_the_call_returns(_wired, monkeypatch):
    remote_db: dict = {}
    monkeypatch.setattr(sb, "upsert_row", _fake_remote_upsert(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))

    _wired(_Adapter({"uid": "bk_durable_1", "status": "accepted"}))
    r = _run(SA.handle_schedule_appointment(_req(), agent_id="agent_durability"))

    assert r.reason_code == "appointment_confirmed"
    assert r.operation_id in remote_db, (
        "the confirmed booking was NOT durably written by the time the call "
        "returned -- a process killed right after this point would leave no "
        "trace it ever happened")
    assert remote_db[r.operation_id]["status"] == "success"
    assert remote_db[r.operation_id]["reason_code"] == "appointment_confirmed"
    assert remote_db[r.operation_id]["agent_id"] == "agent_durability"


def test_a_fresh_process_can_read_the_confirmed_booking_back(_wired, monkeypatch):
    """The other half: durability is worthless if nothing can read it back.
    A brand-new OutcomeStore (empty in-memory cache, exactly what a
    restarted process or a separate horizontally-scaled instance has) must
    resolve the operation from the durable layer alone -- through the SAME
    get_async() the real get_status/get_outcome handlers call, and with the
    outcome payload usable exactly like the in-memory path's (a dict, not
    the raw JSON string the durable row stores it as)."""
    remote_db: dict = {}
    monkeypatch.setattr(sb, "upsert_row", _fake_remote_upsert(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))
    monkeypatch.setattr(sb, "select_rows_strict", _fake_remote_select(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))

    _wired(_Adapter({"uid": "bk_durable_2", "status": "accepted"}))
    r = _run(SA.handle_schedule_appointment(_req(), agent_id="agent_durability"))

    fresh_store = OutcomeStore()  # simulates a different process entirely
    resolved = _run(fresh_store.get_async(r.operation_id))

    assert resolved is not None, (
        "a fresh process (no in-memory state) could not find a booking this "
        "process already confirmed and charged for")
    assert resolved["status"] == "success"
    assert isinstance(resolved["outcome"], dict), (
        f"the durable outcome payload was not a usable dict: "
        f"{type(resolved['outcome'])!r} -- a consumer calling "
        f"OutcomeReceipt(**outcome) would crash on it")
    assert resolved["outcome"]["reason_code"] == "appointment_confirmed"
    assert resolved["outcome"]["result"]["appointment_id"] == "bk_durable_2"
    assert resolved["agent_id"] == "agent_durability"


def test_get_outcome_resolves_a_confirmed_booking_from_a_fresh_process(_wired, monkeypatch):
    """End-to-end through the ACTUAL consumer (core/status_outcome.py), not
    just outcome_store internals -- this is what an agent polling
    get_status/get_outcome after a restart actually calls."""
    from core.status_outcome import handle_get_outcome

    remote_db: dict = {}
    monkeypatch.setattr(sb, "upsert_row", _fake_remote_upsert(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))
    monkeypatch.setattr(sb, "select_rows_strict", _fake_remote_select(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))

    _wired(_Adapter({"uid": "bk_durable_3", "status": "accepted"}))
    r = _run(SA.handle_schedule_appointment(_req(), agent_id="agent_durability"))

    import storage.outcome_store as os_mod
    monkeypatch.setattr(os_mod, "_store", OutcomeStore())  # fresh process's singleton

    polled = _run(handle_get_outcome(r.operation_id, agent_id="agent_durability"))
    assert polled.status == OperationStatus.SUCCESS
    assert polled.reason_code == "appointment_confirmed"
    assert polled.result["appointment_id"] == "bk_durable_3"


def test_unknown_outcome_is_also_durably_persisted(_wired, monkeypatch):
    """The new UNKNOWN outcome (test_booking_retry_safety.py) is exactly the
    state a retry guard must be able to find -- it must be durable too."""
    from channels.direct_api.calcom import BookingOutcomeUnknown

    class _RaisingAdapter(_Adapter):
        async def book_slot(self, *a, **kw):
            raise BookingOutcomeUnknown("simulated timeout after acceptance")

    remote_db: dict = {}
    monkeypatch.setattr(sb, "upsert_row", _fake_remote_upsert(remote_db))
    monkeypatch.setattr(sb, "rpc", _fake_rpc(remote_db))

    _wired(_RaisingAdapter(None))
    r = _run(SA.handle_schedule_appointment(_req(), agent_id="agent_durability"))

    assert r.status == OperationStatus.UNKNOWN
    assert r.operation_id in remote_db, (
        "an UNKNOWN-outcome receipt was not durably persisted -- a retry "
        "guard checking this operation_id later would find nothing and "
        "might conclude it is safe to retry")
    assert remote_db[r.operation_id]["status"] == "unknown"


# ---------------------------------------------------------------------------
# POSITIVE CONTROL -- an ordinary, uncharged rejection is unaffected by this
# fix (kept on the cheap fire-and-forget path) and its RECEIPT CONTENT is
# unchanged. This is expected to pass on both the pre-fix and post-fix tree
# (declared as a non-discriminating control, not proof of the fix).
# ---------------------------------------------------------------------------

def test_bad_input_receipt_shape_is_unaffected_by_the_durability_change():
    req = ScheduleAppointmentRequest(
        smb_id="smb_durability_test", action=AppointmentAction.CANCEL)
    r = _run(SA.handle_schedule_appointment(req))
    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == "bad_input"
    assert r.cost.amount == 0.0
