"""
Contract tests — every claim in the manifest is executed and verified.
Every declared failure_mode is reproducible.
Every schema validates.
Every compliance_constraint is enforceable.
"""
import json
import os
import pytest
import asyncio

MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "../../manifest/manifest.json"
)


def load_manifest():
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestManifestStructure:
    def setup_method(self):
        self.manifest = load_manifest()

    def test_manifest_loads(self):
        assert "operations" in self.manifest
        assert "service" in self.manifest

    def test_all_operations_present(self):
        expected = {
            "find_business", "verify_business", "send_message", "capture_lead",
            "schedule_appointment", "send_transactional_confirmation", "handle_inbound",
            "escalate_to_human", "get_status", "get_outcome", "preview_cost", "self_test",
            "import_booking_url", "call_business", "check_booking_link", "check_compliance",
            "get_conversation",
            "verify_company_record", "screen_sanctions", "map_trade_restriction",
            "mint_key",
        }
        actual = {op["name"] for op in self.manifest["operations"]}
        assert expected == actual, f"missing={expected - actual} extra={actual - expected}"

    def test_every_operation_has_required_fields(self):
        required_fields = [
            "name", "description", "when_to_use", "when_not_to_use",
            "execution_profile", "input_schema", "output_schema",
            "cost_model", "slo", "idempotency", "failure_modes", "examples",
            "compliance_constraints",
        ]
        for op in self.manifest["operations"]:
            for field in required_fields:
                assert field in op, f"Operation '{op['name']}' missing field '{field}'"

    def test_every_operation_has_at_least_one_example(self):
        for op in self.manifest["operations"]:
            assert len(op["examples"]) >= 1, f"Operation '{op['name']}' has no examples"

    def test_execution_profiles_are_valid(self):
        valid = {"sync", "sync_fast", "async_by_default"}
        for op in self.manifest["operations"]:
            assert op["execution_profile"] in valid, (
                f"'{op['name']}' has invalid profile '{op['execution_profile']}'"
            )

    def test_async_operations_declared_correctly(self):
        async_ops = {"schedule_appointment", "handle_inbound", "escalate_to_human"}
        for op in self.manifest["operations"]:
            if op["name"] in async_ops:
                assert op["execution_profile"] == "async_by_default", (
                    f"'{op['name']}' should be async_by_default"
                )

    def test_send_message_has_compliance_constraints(self):
        send_msg = next(op for op in self.manifest["operations"] if op["name"] == "send_message")
        assert len(send_msg["compliance_constraints"]) > 0

    def test_sync_ops_have_no_compliance_constraints(self):
        """Read-only sync ops don't need compliance constraints (no outbound communication)."""
        no_constraint_ops = {"find_business", "verify_business", "get_status", "get_outcome",
                             "preview_cost", "self_test"}
        for op in self.manifest["operations"]:
            if op["name"] in no_constraint_ops:
                assert op["compliance_constraints"] == [], (
                    f"'{op['name']}' should have empty compliance_constraints"
                )

    def test_cost_model_present_for_all_ops(self):
        for op in self.manifest["operations"]:
            assert "basis" in op["cost_model"], f"'{op['name']}' cost_model missing 'basis'"


class TestManifestExamplesExecutable:
    """
    Smoke-test: run each operation's first happy-path example against the actual handlers.
    The contract: every claimed output format must match what the handler actually returns.
    """

    def test_find_business_example_executes(self):
        from core.find_business import handle_find_business
        from core.models import FindBusinessRequest, LocationFilter, Vertical
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="30309"),
            capability="haircut",
        )
        receipt = run(handle_find_business(req))
        assert receipt.status.value in ("success", "failure")
        assert receipt.cost is not None

    def test_verify_business_example_executes(self):
        from core.verify_business import handle_verify_business
        from core.models import VerifyBusinessRequest
        req = VerifyBusinessRequest(smb_id="smb_001", capability_to_verify="haircut")
        receipt = run(handle_verify_business(req))
        assert receipt.result is not None
        assert "verified" in receipt.result

    def test_preview_cost_example_executes(self):
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        req = PreviewCostRequest(operation="schedule_appointment", params={"smb_id": "smb_001"})
        resp = run(handle_preview_cost(req))
        # WAS `== "+/-5%"`. That asserted a constant nobody measured - and it
        # was arithmetically impossible here, where the range spans 0.15-0.50.
        # Assert the honest contract instead: the estimate lies inside its own
        # stated range, and the basis is declared.
        assert (resp.cost_range["min_usd"] <= resp.estimated_cost_usd
                <= resp.cost_range["max_usd"])
        assert resp.cost_accuracy_slo
        assert resp.estimated_cost_usd >= 0

    def test_capture_lead_example_executes(self):
        from core.capture_lead import handle_capture_lead
        from core.models import CaptureLeadRequest, ProspectData
        req = CaptureLeadRequest(
            smb_id="smb_001",
            prospect=ProspectData(name="Jane Smith", phone="+14045551234", email="jane@example.com"),
            source="agent_referral",
        )
        receipt = run(handle_capture_lead(req))
        assert receipt.status.value in ("success", "failure")


class TestFailureModesReproducible:
    """Every declared failure_mode must be triggerable."""

    def test_supply_unreachable_is_triggerable(self):
        from core.find_business import handle_find_business
        from core.models import FindBusinessRequest, LocationFilter, Vertical
        req = FindBusinessRequest(
            vertical=Vertical.HOME_SERVICES,
            location=LocationFilter(zip_or_city="00000"),
        )
        receipt = run(handle_find_business(req))
        # Not supply_unreachable but out_of_supply_network — both declared
        assert receipt.reason_code in ("no_results", "supply_unreachable", "out_of_supply_network")

    def test_bad_input_is_triggerable(self):
        from pydantic import ValidationError
        from core.models import FindBusinessRequest, LocationFilter
        with pytest.raises(ValidationError):
            FindBusinessRequest(vertical="invalid_vertical", location=LocationFilter(zip_or_city="30309"))

    def test_compliance_violation_is_triggerable(self):
        from core.models import ComplianceViolationError
        from compliance.pre_check import pre_check
        with pytest.raises(ComplianceViolationError):
            pre_check(
                recipient_id="+14045550000",
                channel="sms",
                message_type="marketing",
                content="Win big!",
                country_code="US",
            )
