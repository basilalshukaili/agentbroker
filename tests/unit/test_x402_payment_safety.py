from __future__ import annotations
import asyncio
import pytest
from billing.x402_gate import _receipt_is_error, _FAILURE_STATUSES
from core.capture_lead import handle_capture_lead
from core.schedule_appointment import handle_schedule_appointment
from core.models import (
    CaptureLeadRequest, ProspectData, ScheduleAppointmentRequest,
    AppointmentAction, RequestedTime, CustomerInfo, OperationStatus,
)

def run(coro):
    return asyncio.run(coro)

class TestDemoSMBNeverCharged:
    def test_capture_lead_demo_smb_failure(self):
        req = CaptureLeadRequest(smb_id="smb_001", prospect=ProspectData(name="A"), source="test")
        r = run(handle_capture_lead(req))
        assert r.status == OperationStatus.FAILURE, f"got {r.status!r}"
        assert r.reason_code == "demo_smb_no_live_booking"
        assert r.cost.amount == 0.0

    def test_capture_lead_demo_smb_receipt_is_error(self):
        req = CaptureLeadRequest(smb_id="smb_001", prospect=ProspectData(name="A"), source="test")
        r = run(handle_capture_lead(req))
        assert _receipt_is_error(r.model_dump(mode="json")) is True

    def test_capture_lead_demo_multiple_smbs(self):
        for sid in ["smb_001", "smb_002", "smb_044", "smb_081"]:
            req = CaptureLeadRequest(smb_id=sid, prospect=ProspectData(name="A"), source="test")
            r = run(handle_capture_lead(req))
            assert r.status == OperationStatus.FAILURE, f"{sid}: got {r.status!r}"
            assert r.reason_code == "demo_smb_no_live_booking"

    def test_schedule_appointment_demo_smb_failure(self):
        req = ScheduleAppointmentRequest(
            smb_id="smb_001", action=AppointmentAction.BOOK,
            requested_time=RequestedTime(),
            customer=CustomerInfo(name="A", email="a@example.com"), notes="test",
        )
        r = run(handle_schedule_appointment(req))
        assert r.status == OperationStatus.FAILURE, f"got {r.status!r}"
        assert r.reason_code == "demo_smb_no_live_booking"
        assert r.cost.amount == 0.0

    def test_schedule_appointment_demo_smb_receipt_is_error(self):
        req = ScheduleAppointmentRequest(
            smb_id="smb_001", action=AppointmentAction.BOOK,
            requested_time=RequestedTime(),
            customer=CustomerInfo(name="A", email="a@example.com"),
        )
        r = run(handle_schedule_appointment(req))
        assert _receipt_is_error(r.model_dump(mode="json")) is True


class TestPendingAsyncNotChargeable:
    def test_pending_async_in_failure_statuses(self):
        assert "pending_async" in _FAILURE_STATUSES

    def test_receipt_is_error_pending_async(self):
        assert _receipt_is_error({"status": "pending_async"}) is True

    def test_receipt_is_error_pending_async_casing(self):
        assert _receipt_is_error({"status": "PENDING_ASYNC"}) is True
        assert _receipt_is_error({"status": "Pending_Async"}) is True


class TestPartialNotChargeable:
    def test_partial_in_failure_statuses(self):
        assert "partial" in _FAILURE_STATUSES

    def test_receipt_is_error_partial(self):
        assert _receipt_is_error({"status": "partial"}) is True

    def test_receipt_is_error_partial_casing(self):
        assert _receipt_is_error({"status": "PARTIAL"}) is True

    def test_capture_lead_nondemo_returns_partial(self):
        from supply.smb_directory import get_directory
        smb = get_directory().get("smb_001")
        orig_demo, orig_name = smb.is_demo, smb.name
        try:
            smb.is_demo = False
            smb.name = "Real Test"
            req = CaptureLeadRequest(smb_id="smb_001", prospect=ProspectData(name="A"), source="test")
            r = run(handle_capture_lead(req))
            assert r.status == OperationStatus.PARTIAL, f"got {r.status!r}"
            assert r.reason_code == "lead_logged_no_crm"
            assert r.cost.amount == 0.0
            assert _receipt_is_error(r.model_dump(mode="json")) is True
        finally:
            smb.is_demo = orig_demo
            smb.name = orig_name


class TestRealSuccessStillChargeable:
    def test_success_not_in_failure_statuses(self):
        assert "success" not in _FAILURE_STATUSES

    def test_receipt_is_error_success_false(self):
        assert _receipt_is_error({"status": "success"}) is False

    def test_receipt_is_error_appointment_confirmed_false(self):
        assert _receipt_is_error({"status": "success", "reason_code": "appointment_confirmed"}) is False

    def test_receipt_is_error_empty_false(self):
        assert _receipt_is_error({}) is False
        assert _receipt_is_error({"status": ""}) is False


class TestUnknownSMBNotCharged:
    def test_capture_lead_unknown_smb_no_charge(self):
        req = CaptureLeadRequest(smb_id="smb_GHOST", prospect=ProspectData(name="Ghost"), source="test")
        r = run(handle_capture_lead(req))
        assert r.status == OperationStatus.FAILURE
        assert r.reason_code == "supply_unreachable"
        assert r.cost.amount == 0.0
        assert _receipt_is_error(r.model_dump(mode="json")) is True
