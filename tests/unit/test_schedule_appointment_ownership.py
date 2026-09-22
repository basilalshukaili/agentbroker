"""
schedule_appointment (and its sibling write-tools) leak operation receipts to
callers who did not create them.

THE HOLE, REPRODUCED. The REST route authenticates the caller:

    @app.post("/ops/schedule_appointment", ...)
    async def schedule_appointment(req, x_agent_identity=Header(None)):
        _get_identity(x_agent_identity, "schedule_appointment")   # checks it
        return await handle_schedule_appointment(req)              # drops it

`_get_identity` validates the bearer token and can 401 an invalid one, but the
PARSED agent_id it proves is never handed to the handler that stores the
receipt. core/schedule_appointment.py's `_store_terminal` (and its
`set_pending` call on the async path) then persisted every receipt with
agent_id=None — unowned. core/status_outcome.py's read policy for operations
used to say an unowned row is free to read for ANYONE holding the id,
including an anonymous caller (the one deliberate difference from
get_conversation, which already denies unowned rows to everyone). Put the two
together: authenticate at the door, lose the badge on the way in, and the
"capability" model that policy rested on collapses — anyone can walk in.

A validation-failure receipt is the cleanest reproduction because it is the
FIRST branch in handle_schedule_appointment (a "cancel" with no
existing_appointment_id, checked before the SMB directory is even
consulted) — no adapter, no directory, no network involved, exactly what an
external reviewer would reach for first.

The same drop existed on the MCP dispatch call site, in the async "pending"
write issued before a worker ever runs, and in the Celery worker's own
completion write (which runs in a SEPARATE PROCESS with a blank in-memory
store, so it cannot rely on an in-memory guard the API-server process holds —
only on being explicitly handed the agent_id, which it already receives as a
task parameter and previously never passed on). The same identity-drop
pattern existed, independently, in call_business, escalate_to_human and
send_transactional_confirmation.

These tests drive the real REST routes, the real MCP dispatcher, the real
Celery task body and the real OutcomeStore, for the reasons
tests/unit/test_conversation_ownership.py gives: the wire between the
caller's token and the stored row is exactly what was missing.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import uuid
from unittest.mock import patch

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent_interface.identity import TokenRequest, issue_token  # noqa: E402
from agent_interface.mcp_server import _dispatch_operation  # noqa: E402
import main as main_app  # noqa: E402
from core.models import (  # noqa: E402
    AppointmentAction, ScheduleAppointmentRequest,
    EscalateToHumanRequest, EscalationContext, EscalationReason,
    SendTransactionalConfirmationRequest, TransactionalRecipient,
    ConfirmationType, CallBusinessRequest,
)
from core.schedule_appointment import handle_schedule_appointment  # noqa: E402
from core.status_outcome import handle_get_status, handle_get_outcome  # noqa: E402
from storage.outcome_store import OutcomeStore, get_outcome_store  # noqa: E402
from supply.smb_directory import get_directory  # noqa: E402


# --------------------------------------------------------------------------
# Fake Supabase — the same in-memory table test_conversation_ownership.py
# uses, extended with nothing: outcome_store.py already writes to
# "operations" through the same insert_row/upsert_row/select_rows surface.
# --------------------------------------------------------------------------
class FakeSB:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}

    async def insert_row(self, table, row):
        self.rows.setdefault(table, []).append(dict(row))
        return dict(row)

    async def upsert_row(self, table, row, on_conflict="id"):
        for r in self.rows.setdefault(table, []):
            if r.get(on_conflict) == row.get(on_conflict):
                r.update(row)
                return r
        self.rows[table].append(dict(row))
        return dict(row)

    async def select_rows(self, table, filters=None, limit=1000, order=None, gte=None):
        out = []
        for r in self.rows.get(table, []):
            if not all(r.get(k) == v for k, v in (filters or {}).items()):
                continue
            out.append(r)
        return out[:limit]

    # FIX (2026-09-21, unavailable-vs-absent): storage/outcome_store.py's
    # _supabase_fetch now reads through `select_rows_strict` (which raises
    # SupabaseUnavailable instead of returning [] on a real failure) rather
    # than the lenient `select_rows` this fake used to stand in for alone.
    # This fake represents a REACHABLE store, so its strict variant behaves
    # exactly like its lenient one -- it never raises.
    async def select_rows_strict(self, table, filters=None, limit=1000, order=None, gte=None):
        return await self.select_rows(table, filters=filters, limit=limit, order=order, gte=gte)

    # FIX (board row 206, 2026-09-22): _supabase_fetch/_supabase_upsert/
    # _supabase_fetch_by_appointment_id now call the three operations_*
    # SECURITY DEFINER RPCs instead of the raw table surface above -- this
    # service deploys with only the Supabase anon key, which has no grant on
    # `operations` at all (see
    # sql/agentbroker/001_operations_security_definer_rpc.sql). Translate
    # each RPC call onto the SAME self.rows["operations"] list the
    # table-level methods already maintain, so every test in this file that
    # asserts against `fake_sb.rows["operations"]` keeps working unchanged.
    async def rpc(self, fn, payload):
        if fn == "operations_upsert":
            row = {
                "operation_id": payload["p_operation_id"],
                "tool": payload["p_tool"],
                "status": payload["p_status"],
                "reason_code": payload["p_reason_code"],
                "appointment_id": payload["p_appointment_id"],
                "result_json": payload["p_result_json"],
                "agent_id": payload["p_agent_id"],
            }
            return await self.upsert_row("operations", row, on_conflict="operation_id")
        if fn == "operations_get_by_id":
            rows = await self.select_rows(
                "operations", filters={"operation_id": payload["p_operation_id"]}, limit=1)
            return rows[0] if rows else None
        if fn == "operations_get_by_appointment_id":
            rows = await self.select_rows(
                "operations",
                filters={"appointment_id": payload["p_appointment_id"],
                          "reason_code": "appointment_confirmed"},
                limit=1)
            return rows[0] if rows else None
        raise AssertionError(f"unexpected rpc fn in test fake: {fn!r}")


@pytest.fixture(autouse=True)
def fake_sb(monkeypatch):
    sb = FakeSB()
    import storage.supabase_client as real
    monkeypatch.setattr(real, "insert_row", sb.insert_row)
    monkeypatch.setattr(real, "upsert_row", sb.upsert_row)
    monkeypatch.setattr(real, "select_rows", sb.select_rows)
    monkeypatch.setattr(real, "select_rows_strict", sb.select_rows_strict)
    monkeypatch.setattr(real, "rpc", sb.rpc)
    return sb


def _token(agent_id: str) -> str:
    return issue_token(TokenRequest(agent_id=agent_id, principal_id="p_" + agent_id)).token


def _headers(agent_id):
    return {} if agent_id is None else {"x-agent-identity": _token(agent_id)}


def run(coro):
    return asyncio.run(coro)


A, B = "agent_alice_sched", "agent_mallory_sched"


def _cancel_request(smb_id: str) -> ScheduleAppointmentRequest:
    """The fastest honest receipt in the whole handler: validated, terminal,
    and returned before the SMB directory is even consulted."""
    return ScheduleAppointmentRequest(smb_id=smb_id, action=AppointmentAction.CANCEL)


# ==========================================================================
# 1. THE REPRODUCTION — REST route. This is the exact shape the reviewer
#    used: authenticate, then read anonymously.
# ==========================================================================
class TestRestRouteReproduction:
    def test_rest_route_no_longer_leaks_an_authenticated_callers_receipt(self):
        receipt = run(main_app.schedule_appointment(
            _cancel_request("smb_probe_rest_1"), x_agent_identity=_token(A)))
        assert receipt.status.value == "failure"
        assert receipt.reason_code == "bad_input"
        op_id = receipt.operation_id

        # THE EXACT REPRODUCTION: fetch it back with no key at all.
        stolen = run(main_app.get_outcome(op_id, x_agent_identity=None))
        assert stolen.status.value == "failure", (
            "an anonymous caller read a receipt created by an authenticated "
            "agent through the REST route — this is the reported hole")
        assert stolen.reason_code in ("identity_required", "operation_owner_unknown")

        stolen_by_b = run(main_app.get_outcome(op_id, x_agent_identity=_token(B)))
        assert stolen_by_b.status.value == "failure"
        assert stolen_by_b.reason_code in ("not_your_operation", "operation_owner_unknown")

    def test_the_owner_can_still_read_its_own_rest_created_receipt(self):
        """The other half: binding the caller must not merely refuse
        everyone — the creator has to still get its own content back."""
        receipt = run(main_app.schedule_appointment(
            _cancel_request("smb_probe_rest_2"), x_agent_identity=_token(A)))
        op_id = receipt.operation_id
        mine = run(main_app.get_outcome(op_id, x_agent_identity=_token(A)))
        assert mine.status.value == "failure"      # the ORIGINAL content...
        assert mine.reason_code == "bad_input"      # ...not an ownership refusal

    def test_rest_created_receipt_is_owned_in_storage(self, fake_sb):
        run(main_app.schedule_appointment(
            _cancel_request("smb_probe_rest_3"), x_agent_identity=_token(A)))
        rows = fake_sb.rows.get("operations", [])
        assert rows, "no operation row was durably written"
        assert rows[-1]["agent_id"] == A, (
            "the REST route did not bind the caller onto the stored receipt "
            "— %r" % (rows[-1],))


# ==========================================================================
# 2. THE SAME REPRODUCTION — MCP path. The per-tool dispatch call site
#    (`receipt = await handle_schedule_appointment(req)`) dropped agent_id
#    exactly like the REST route.
# ==========================================================================
class TestMcpPathReproduction:
    def test_mcp_path_no_longer_leaks_an_authenticated_callers_receipt(self):
        receipt = run(_dispatch_operation(
            "schedule_appointment", {"smb_id": "smb_probe_mcp_1", "action": "cancel"},
            _headers(A)))
        assert receipt["status"] == "failure"
        assert receipt["reason_code"] == "bad_input"
        op_id = receipt["operation_id"]

        stolen = run(_dispatch_operation("get_outcome", {"operation_id": op_id}, _headers(None)))
        assert stolen["status"] == "failure"
        assert stolen["reason_code"] in ("identity_required", "operation_owner_unknown")

        stolen_by_b = run(_dispatch_operation("get_outcome", {"operation_id": op_id}, _headers(B)))
        assert stolen_by_b["status"] == "failure"

    def test_the_owner_can_still_read_its_own_mcp_created_receipt(self):
        receipt = run(_dispatch_operation(
            "schedule_appointment", {"smb_id": "smb_probe_mcp_2", "action": "cancel"},
            _headers(A)))
        op_id = receipt["operation_id"]
        mine = run(_dispatch_operation("get_outcome", {"operation_id": op_id}, _headers(A)))
        assert mine["reason_code"] == "bad_input"


# ==========================================================================
# 3. PENDING RECORDS — before a Celery worker ever runs, the async path's
#    FIRST write (set_pending) must already carry the owner.
# ==========================================================================
def _fake_async_smb(smb_id: str):
    base = get_directory().get("smb_001")
    return dataclasses.replace(
        base,
        smb_id=smb_id,
        name="Pending Ownership Test",
        is_demo=False,
        calcom_event_type_id=None,
        channels_available=["voice_ai:vapi"],
        phone="+15550009" + str(abs(hash(smb_id)) % 1000).zfill(3),
        email=f"{smb_id}@example.test",
    )


class TestPendingRecordOwnership:
    def test_pending_booking_is_owned_before_any_worker_runs(self, monkeypatch):
        import core.schedule_appointment as sa

        smb = _fake_async_smb("smb_pending_owner_1")

        class _FakeDirectory:
            def get(self, smb_id):
                return smb if smb_id == "smb_pending_owner_1" else None

        # sa.py does `from supply.smb_directory import get_directory` at
        # import time, so the name to patch is sa.get_directory, not
        # supply.smb_directory.get_directory (dead-monkeypatch bug class).
        monkeypatch.setattr(sa, "get_directory", lambda: _FakeDirectory())
        monkeypatch.setattr(sa, "_has_celery_worker", lambda: True)
        monkeypatch.setattr(sa, "_enqueue_async_booking", lambda *a, **k: True)

        req = ScheduleAppointmentRequest(smb_id="smb_pending_owner_1", action=AppointmentAction.BOOK)
        receipt = run(handle_schedule_appointment(req, agent_id=A))
        assert receipt.status.value == "pending_async", receipt
        op_id = receipt.operation_id

        # STILL PENDING. Neither B nor an anonymous caller may read it.
        stolen = run(handle_get_status(op_id, agent_id=B))
        assert stolen["status"] == "forbidden", stolen
        stolen_anon = run(handle_get_status(op_id, agent_id="anonymous"))
        assert stolen_anon["status"] == "forbidden", stolen_anon

        # The owner CAN see it, in its true (pending) state.
        mine = run(handle_get_status(op_id, agent_id=A))
        assert mine["status"] == "pending", mine


# ==========================================================================
# 4. WORKER COMPLETION — the Celery task runs in a separate process with its
#    own blank in-memory OutcomeStore, so the owner has to arrive as an
#    explicit parameter, not survive by accident.
# ==========================================================================
class TestWorkerCompletionOwnership:
    def test_owner_survives_a_cross_process_completion_write(self):
        """Simulates the real topology: the API server's OutcomeStore sets
        the owner at set_pending time; a SECOND, independent OutcomeStore
        instance (standing in for the worker's own process) completes it.
        Passing agent_id through (the fix) must leave the owner intact even
        after the API-server's in-memory cache is evicted."""
        api_store = get_outcome_store()
        op_id = f"op_worker_{uuid.uuid4().hex[:8]}"
        worker_store = OutcomeStore()   # a different process's store

        async def _seed():
            # The durable (fake Supabase) write is fire-and-forget, scheduled
            # onto the running loop — it needs one running to ever execute,
            # which a plain synchronous call in a sync test body does not
            # provide.
            api_store.set_pending(op_id, "schedule_appointment", agent_id=A)
            worker_store.set_complete(
                op_id, {"operation_id": op_id, "status": "success"}, agent_id=A)
            await asyncio.sleep(0)

        run(_seed())

        # Cache eviction: the api_store's in-memory copy is gone; only the
        # durable (fake Supabase) row remains.
        api_store._records.pop(op_id, None)

        mine = run(handle_get_status(op_id, agent_id=A))
        assert mine["status"] == "success", mine
        stolen = run(handle_get_status(op_id, agent_id=B))
        assert stolen["status"] == "forbidden", stolen
        stolen_anon = run(handle_get_status(op_id, agent_id="anonymous"))
        assert stolen_anon["status"] == "forbidden", stolen_anon

    def test_the_old_no_agent_id_call_shape_would_have_cleared_the_owner(self, fake_sb):
        """Documents WHY the worker fix is necessary, independent of Celery
        being importable in this environment, and independent of the
        separate fail-closed-on-unowned fix in status_outcome.py (which
        would otherwise mask this at the READ layer -- an unowned row is
        denied either way now, so this checks the STORED row directly).
        reliability/async_runner.py used to call
        store.set_complete(operation_id, result) with no agent_id at all.
        Reproduced directly against OutcomeStore and the durable row it
        writes: the upsert uses PostgREST 'merge-duplicates', which REPLACES
        every column named in the payload, including an explicit
        agent_id=None, clearing an owner a different process had already
        set. If this assertion ever starts failing, the upsert semantics
        changed and reliability/async_runner.py's fix should be re-examined
        against the new semantics."""
        api_store = get_outcome_store()
        op_id = f"op_worker_bug_{uuid.uuid4().hex[:8]}"
        worker_store = OutcomeStore()

        async def _seed():
            api_store.set_pending(op_id, "schedule_appointment", agent_id=A)
            await asyncio.sleep(0)
            seeded = next(r for r in fake_sb.rows["operations"] if r["operation_id"] == op_id)
            assert seeded["agent_id"] == A, (
                "set_pending did not durably record the owner -- precondition "
                "for this test failed: %r" % (seeded,))
            # The OLD call shape (no agent_id kwarg).
            worker_store.set_complete(op_id, {"operation_id": op_id, "status": "success"})
            await asyncio.sleep(0)

        run(_seed())

        durable_row = next(r for r in fake_sb.rows["operations"] if r["operation_id"] == op_id)
        assert durable_row["agent_id"] is None, (
            "expected the pre-fix call shape (set_complete with no agent_id) "
            "to clear the durably-recorded owner; got %r -- if upsert "
            "semantics changed, re-verify reliability/async_runner.py's fix "
            "against the new behaviour" % (durable_row,))

    def test_enqueue_booking_task_preserves_the_owner_on_completion(self):
        """The real Celery task body, called directly (bypassing .delay() /
        the broker) the same way tests/unit/test_honesty_fixes.py does."""
        try:
            from reliability import async_runner as ar
        except Exception:
            pytest.skip("Celery not importable in this environment")
        if not ar.CELERY_AVAILABLE:
            pytest.skip("Celery not available")

        smb = _fake_async_smb("smb_worker_task_owner_1")

        class _FakeDirectory:
            def get(self, smb_id):
                return smb if smb_id == "smb_worker_task_owner_1" else None

        op_id = f"op_worker_task_{uuid.uuid4().hex[:8]}"
        store = get_outcome_store()
        store.set_pending(op_id, "schedule_appointment", agent_id=A)

        with patch("supply.smb_directory.get_directory", return_value=_FakeDirectory()), \
             patch("reliability.async_runner._fire_webhook"):
            result = ar.enqueue_booking.__wrapped__(
                op_id,
                {"smb_id": "smb_worker_task_owner_1", "action": "book"},
                "smb_worker_task_owner_1",
                A,
                None,
            )

        # No VAPI credentials in the test environment -> honest failure. The
        # point here is ownership, not the booking outcome.
        assert result["status"] == "failure"
        assert result["reason_code"] == "voice_not_provisioned"

        mine = run(handle_get_status(op_id, agent_id=A))
        assert mine["status"] == "failure", mine
        stolen = run(handle_get_status(op_id, agent_id=B))
        assert stolen["status"] == "forbidden", stolen
        stolen_anon = run(handle_get_status(op_id, agent_id="anonymous"))
        assert stolen_anon["status"] == "forbidden", stolen_anon


# ==========================================================================
# 5. HISTORICAL / GENUINELY UNOWNED RECORDS — fail closed, not open. No
#    field distinguishes "predates this fix" from "caller had no identity",
#    so both answer the same way: nobody.
# ==========================================================================
class TestHistoricalUnownedRecordsFailClosed:
    def _seed_unowned(self, op_id: str):
        store = get_outcome_store()
        store._records[op_id] = {
            "operation_id": op_id,
            "status": "success",
            "outcome": {
                "operation_id": op_id,
                "status": "success",
                "reason_code": "appointment_confirmed",
                "human_message": "Appointment booked at Cuts & Co. for 2026-10-01T14:00:00Z.",
                "result": {"customer_name": "Real Customer", "smb_name": "Cuts & Co."},
                "cost": {"amount": 1.0, "currency": "USD", "basis": "per_confirmed_booking"},
                "latency_ms": 5,
                "retriable": False,
            },
            # Deliberately no "agent_id" key: the historical shape (a row
            # written before ownership was ever attached, or one written for
            # a caller that genuinely presented no identity).
        }
        return store

    def test_get_status_denies_a_named_caller(self):
        op_id = f"op_legacy_{uuid.uuid4().hex[:8]}"
        self._seed_unowned(op_id)
        denied = run(handle_get_status(op_id, agent_id=A))
        assert denied["status"] == "forbidden", denied
        assert denied["reason_code"] == "operation_owner_unknown", denied

    def test_get_status_denies_an_anonymous_caller(self):
        op_id = f"op_legacy_{uuid.uuid4().hex[:8]}"
        self._seed_unowned(op_id)
        denied = run(handle_get_status(op_id, agent_id="anonymous"))
        assert denied["status"] == "forbidden", denied
        assert denied["reason_code"] == "operation_owner_unknown", denied

    def test_get_outcome_asks_the_same_question_as_get_status(self):
        op_id = f"op_legacy_{uuid.uuid4().hex[:8]}"
        self._seed_unowned(op_id)
        denied = run(handle_get_outcome(op_id, agent_id=A))
        assert denied.status.value == "failure", denied
        assert denied.reason_code == "operation_owner_unknown", denied
        assert not denied.result

    def test_denied_reads_do_not_assign_the_row_to_the_reader(self):
        """NOT a takeover primitive: a failed read by a named agent must not
        make that agent the owner. Probe with A, then prove B is refused
        exactly the same way A was, and the record is still ownerless."""
        op_id = f"op_legacy_{uuid.uuid4().hex[:8]}"
        store = self._seed_unowned(op_id)

        run(handle_get_status(op_id, agent_id=A))
        assert store._records[op_id].get("agent_id") is None, (
            "a denied read assigned ownership to the reader — this is the "
            "takeover primitive the fix must not introduce")

        still_denied_for_a = run(handle_get_status(op_id, agent_id=A))
        assert still_denied_for_a["status"] == "forbidden"
        also_denied_for_b = run(handle_get_status(op_id, agent_id=B))
        assert also_denied_for_b["status"] == "forbidden"

    def test_in_process_polling_is_unaffected(self):
        """core/schedule_appointment.py's own __main__ smoke path polls with
        no caller at all (agent_id=None, the in-process sentinel — never
        reachable from outside). That must keep working; only external
        callers (a real string, including "anonymous") are subject to this
        policy at all."""
        op_id = f"op_legacy_{uuid.uuid4().hex[:8]}"
        self._seed_unowned(op_id)
        mine = run(handle_get_status(op_id))   # agent_id defaults to None
        assert mine["status"] == "success", mine


# ==========================================================================
# 6. THE SAME PATTERN, THE OTHER THREE WRITE-TOOLS. status_outcome.py's
#    read policy is shared by every operation type, so tightening it to fail
#    closed on unowned rows would have silently broken these tools' own
#    owners' ability to poll their own receipts unless the same propagation
#    fix was made here too.
# ==========================================================================
class TestSiblingProducersOwnership:
    def test_send_transactional_confirmation_rest_route_binds_the_caller(self, monkeypatch):
        from channels.sms_email.resend_email import ResendEmailAdapter
        from channels.adapter_interface import ChannelResponse

        async def fake_send(self, req):
            return ChannelResponse(success=True, provider_message_id="resend_test_1")

        monkeypatch.setattr(ResendEmailAdapter, "send", fake_send)

        req = SendTransactionalConfirmationRequest(
            recipient=TransactionalRecipient(phone_or_email="customer@example.test", name="Sara"),
            confirmation_type=ConfirmationType.BOOKING_CONFIRMATION,
            data={"name": "Sara", "smb_name": "Cuts & Co.",
                  "appointment_time": "2026-10-01 2pm", "address": "1 Main St"},
        )
        receipt = run(main_app.send_transactional_confirmation(req, x_agent_identity=_token(A)))
        assert receipt.status.value == "success", receipt
        op_id = receipt.operation_id

        stolen = run(main_app.get_outcome(op_id, x_agent_identity=_token(B)))
        assert stolen.status.value == "failure"
        assert stolen.reason_code == "not_your_operation"

        mine = run(main_app.get_outcome(op_id, x_agent_identity=_token(A)))
        assert mine.status.value == "success"

    def test_escalate_to_human_rest_route_binds_the_caller(self):
        req = EscalateToHumanRequest(
            smb_id="smb_escalate_owner_test",
            reason=EscalationReason.CUSTOMER_REQUESTED,
            context=EscalationContext(original_operation="send_message"),
        )
        receipt = run(main_app.escalate_to_human(req, x_agent_identity=_token(A)))
        assert receipt.status.value == "success", receipt
        op_id = receipt.operation_id

        stolen = run(main_app.get_outcome(op_id, x_agent_identity=_token(B)))
        assert stolen.status.value == "failure"
        assert stolen.reason_code == "not_your_operation"

        mine = run(main_app.get_outcome(op_id, x_agent_identity=_token(A)))
        assert mine.status.value == "success"

    def test_call_business_rest_route_binds_the_caller(self, monkeypatch):
        monkeypatch.setenv("VAPI_API_KEY", "test_key")
        monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "test_number_id")

        import core.call_business as cb
        from channels.adapter_interface import ChannelResponse

        async def fake_send(req):
            return ChannelResponse(success=True, provider_message_id="vapi_call_test_1")

        monkeypatch.setattr(cb._VOICE_ADAPTER, "send", fake_send)

        req = CallBusinessRequest(business_phone="+14045550123", objective="Ask about hours")
        receipt = run(main_app.call_business(req, x_agent_identity=_token(A)))
        assert receipt.status.value == "pending_async", receipt
        op_id = receipt.operation_id

        stolen = run(main_app.get_outcome(op_id, x_agent_identity=_token(B)))
        assert stolen.status.value == "failure"
        assert stolen.reason_code == "not_your_operation"

        mine = run(main_app.get_outcome(op_id, x_agent_identity=_token(A)))
        assert mine.status.value == "pending_async"
