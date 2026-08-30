"""
Unit tests — core operation handlers.
"""
import pytest
import asyncio
from core.find_business import handle_find_business
from core.verify_business import handle_verify_business
from core.capture_lead import handle_capture_lead
from core.preview_cost import handle_preview_cost
from core.status_outcome import handle_get_status, handle_get_outcome
from core.handle_inbound import handle_inbound
from core.models import (
    FindBusinessRequest, LocationFilter, Vertical,
    VerifyBusinessRequest, CaptureLeadRequest, ProspectData,
    PreviewCostRequest, HandleInboundRequest, InboundChannel, InboundSender,
    OperationStatus,
)


def run(coro):
    return asyncio.run(coro)


class TestFindBusiness:
    def test_find_hair_salon_atlanta(self):
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="30309"),
            capability="haircut",
        )
        receipt = run(handle_find_business(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert receipt.result["businesses"]
        assert all("smb_id" in b for b in receipt.result["businesses"])

    def test_find_no_results_returns_success_empty(self):
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="99999"),
        )
        receipt = run(handle_find_business(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert receipt.result["businesses"] == []
        assert "supply_coverage_note" in receipt.result

    def test_find_plumber_boston(self):
        req = FindBusinessRequest(
            vertical=Vertical.HOME_SERVICES,
            location=LocationFilter(zip_or_city="02139"),
            capability="plumbing",
        )
        receipt = run(handle_find_business(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert any("plumbing" in b["capabilities"] for b in receipt.result["businesses"])

    def test_cost_matches_the_price_table(self):
        """find_business is FREE in billing/pricing.py. This test used to assert
        the receipt reported $0.01 - pinning a defect as if it were the spec, so
        nobody noticed a free tool claiming a charge (2026-08-26)."""
        from billing.pricing import price_cents
        req = FindBusinessRequest(
            vertical=Vertical.PROFESSIONAL_SERVICES,
            location=LocationFilter(zip_or_city="Boston"),
        )
        receipt = run(handle_find_business(req))
        assert price_cents("find_business") == 0
        assert receipt.cost.amount == 0.0

    def test_max_results_respected(self):
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="Atlanta"),
            max_results=2,
        )
        receipt = run(handle_find_business(req))
        assert len(receipt.result["businesses"]) <= 2


class TestVerifyBusiness:
    def test_verify_known_smb(self):
        req = VerifyBusinessRequest(smb_id="smb_001", capability_to_verify="haircut")
        receipt = run(handle_verify_business(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert receipt.result["verified"] is True

    def test_verify_wrong_capability_returns_failure(self):
        req = VerifyBusinessRequest(smb_id="smb_001", capability_to_verify="emergency_plumbing")
        receipt = run(handle_verify_business(req))
        assert receipt.result["verified"] is False

    def test_verify_unknown_smb_returns_failure(self):
        req = VerifyBusinessRequest(smb_id="smb_UNKNOWN")
        receipt = run(handle_verify_business(req))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "supply_unreachable"


class TestCaptureLead:
    def test_capture_lead_known_smb(self):
        # smb_001 is a demo SMB (is_demo=True) -- the CRITICAL-1 fix short-circuits
        # demo SMBs with status=failure/reason_code=demo_smb_no_live_booking before
        # any real action (and before any CDP settlement). Updated expected values.
        req = CaptureLeadRequest(
            smb_id="smb_001",
            prospect=ProspectData(name="Jane Doe", phone="+14045559999", service_interest="haircut"),
            source="agent_test",
        )
        receipt = run(handle_capture_lead(req))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "demo_smb_no_live_booking"
        assert receipt.cost.amount == 0.0

    def test_capture_lead_unknown_smb_fails(self):
        req = CaptureLeadRequest(
            smb_id="smb_GHOST",
            prospect=ProspectData(name="John"),
        )
        receipt = run(handle_capture_lead(req))
        assert receipt.status == OperationStatus.FAILURE


class TestPreviewCost:
    def test_schedule_appointment_preview(self):
        req = PreviewCostRequest(operation="schedule_appointment", params={"smb_id": "smb_001"})
        resp = run(handle_preview_cost(req))
        assert resp.estimated_cost_usd > 0
        # WAS `== "+/-5%"`. That asserted a constant nobody measured - and it
        # was arithmetically impossible here, where the range spans 0.15-0.50.
        # Assert the honest contract instead: the estimate lies inside its own
        # stated range, and the basis is declared.
        assert (resp.cost_range["min_usd"] <= resp.estimated_cost_usd
                <= resp.cost_range["max_usd"])
        assert resp.cost_accuracy_slo
        assert resp.success_probability_estimate > 0

    def test_self_test_is_free(self):
        req = PreviewCostRequest(operation="self_test", params={})
        resp = run(handle_preview_cost(req))
        assert resp.estimated_cost_usd == 0.0

    def test_all_12_operations_have_preview(self):
        ops = [
            "find_business", "verify_business", "send_message", "capture_lead",
            "schedule_appointment", "send_transactional_confirmation", "handle_inbound",
            "escalate_to_human", "get_status", "get_outcome", "preview_cost", "self_test",
        ]
        for op in ops:
            req = PreviewCostRequest(operation=op, params={})
            resp = run(handle_preview_cost(req))
            assert resp.estimated_cost_usd >= 0, f"Missing pricing for {op}"


class TestHandleInbound:
    def test_booking_inquiry_classified(self):
        req = HandleInboundRequest(
            smb_id="smb_001",
            inbound_channel=InboundChannel.SMS,
            sender=InboundSender(phone="+14045551234"),
            raw_message="Hi, do you have anything Saturday morning?",
        )
        receipt = run(handle_inbound(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert "booking_inquiry" in receipt.reason_code

    def test_cancellation_classified(self):
        req = HandleInboundRequest(
            smb_id="smb_001",
            inbound_channel=InboundChannel.SMS,
            sender=InboundSender(phone="+14045551234"),
            raw_message="I need to cancel my appointment",
        )
        receipt = run(handle_inbound(req))
        assert "cancellation" in receipt.reason_code

    def test_stop_keyword_triggers_opt_out(self):
        from unittest.mock import AsyncMock, patch
        req = HandleInboundRequest(
            smb_id="smb_001",
            inbound_channel=InboundChannel.SMS,
            sender=InboundSender(phone="+14045550001"),
            raw_message="STOP",
        )
        # FIX 3: opt_out_processed is now set from the Supabase durable write.
        # Mock the insert to return a row so the test verifies the opt-out path
        # without needing a live Supabase connection.
        fake_row = {"id": "test-consent-uuid", "recipient_id": "+14045550001"}
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=fake_row):
            receipt = run(handle_inbound(req))
        assert receipt.result.get("opt_out_processed") is True


class TestGetStatusAndOutcome:
    def test_get_status_unknown_operation(self):
        result = run(handle_get_status("op_UNKNOWN"))
        assert result["status"] == "not_found"

    def test_get_outcome_unknown_returns_failure(self):
        receipt = run(handle_get_outcome("op_UNKNOWN"))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "not_found"
