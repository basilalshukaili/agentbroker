"""
Unit tests — check_compliance read-only compliance pre-flight.

check_compliance runs the SAME compliance.pre_check gate that send_message /
call_business run, but in preview mode: no message is sent and no audit event is
written. These tests pin three properties:
  1. the verdict is a truthful mirror of the real gate (single source of truth),
  2. the preview is side-effect free (no audit-log writes),
  3. a truthful "no" is a SUCCESSFUL check, only malformed input is a failure.
"""
import asyncio
import json

import pytest

from core.check_compliance import handle_check_compliance
from core.models import OperationStatus, ComplianceViolationError
from compliance.pre_check import pre_check
from compliance.audit_log import get_audit_log
from agent_interface.mcp_server import handle_mcp_request, _build_tool_list


def run(coro):
    return asyncio.run(coro)


class TestCheckComplianceHandler:
    def test_transactional_email_is_compliant(self):
        r = run(handle_check_compliance(
            recipient_id="jane@example.com",
            content="Your appointment at Cuts & Co. is confirmed for Tuesday 10:30am.",
            message_type="transactional",
            country_code="US",
        ))
        assert r.status == OperationStatus.SUCCESS
        assert r.reason_code == "compliant"
        assert r.result["legal"] is True
        assert r.result["channel"] == "email"          # inferred from the '@'
        assert r.result["rule"] is None
        assert r.result["checked_live"] is False
        assert r.cost.basis == "free"
        assert r.cost.amount == 0.0

    def test_marketing_sms_us_blocked_without_consent(self):
        r = run(handle_check_compliance(
            recipient_id="+14045550200",
            content="20% off this week only!",
            channel="sms",
            message_type="marketing",
            country_code="US",
        ))
        # A truthful "this send is illegal" is a SUCCESSFUL check, not a failure.
        assert r.status == OperationStatus.SUCCESS
        assert r.reason_code == "not_compliant"
        assert r.result["legal"] is False
        assert r.result["rule"] == "TCPA_marketing_consent"
        assert r.result["remediation"]                  # actionable next step present
        assert r.next_actions                            # points at the fix
        assert r.cost.basis == "free"

    def test_channel_inferred_sms_from_phone(self):
        r = run(handle_check_compliance(
            recipient_id="+14045550100",
            content="Reminder: your appointment is tomorrow at 10am.",
            message_type="reminder",
            country_code="US",
        ))
        assert r.result["channel"] == "sms"

    def test_missing_recipient_is_bad_input(self):
        r = run(handle_check_compliance(recipient_id="", content="hi"))
        assert r.status == OperationStatus.FAILURE
        assert r.reason_code == "bad_input"

    def test_missing_content_is_bad_input(self):
        r = run(handle_check_compliance(recipient_id="jane@example.com", content="  "))
        assert r.status == OperationStatus.FAILURE
        assert r.reason_code == "bad_input"

    def test_invalid_channel_is_bad_input(self):
        r = run(handle_check_compliance(
            recipient_id="+14045550100", content="hi", channel="carrier_pigeon",
        ))
        assert r.status == OperationStatus.FAILURE
        assert r.reason_code == "bad_input"


class TestCheckComplianceIsSideEffectFree:
    """A read-only preview must not touch the append-only audit log — neither an
    OUTBOUND_DISPATCHED 'allow' on a pass nor a violation record on a block."""

    def test_compliant_preview_writes_no_audit_record(self):
        before = get_audit_log().count()
        run(handle_check_compliance(
            recipient_id="passthrough@example.com",
            content="Booking confirmed.",
            message_type="transactional",
            country_code="US",
        ))
        assert get_audit_log().count() == before

    def test_blocked_preview_writes_no_audit_record(self):
        before = get_audit_log().count()
        run(handle_check_compliance(
            recipient_id="+14045559998",
            content="Flash sale — buy now!",
            channel="sms",
            message_type="marketing",
            country_code="US",
        ))
        assert get_audit_log().count() == before


class TestVerdictMirrorsRealGate:
    """check_compliance must never disagree with the gate that actually enforces
    the send — the whole point is a trustworthy preview."""

    CASES = [
        # (recipient, channel, message_type, content, country)
        ("+14045550101", "sms", "transactional", "Your order shipped.", "US"),
        ("+14045550102", "sms", "marketing", "Big sale!", "US"),
        ("buyer@example.com", "email", "transactional", "Receipt attached.", "US"),
        ("+14045550103", "sms", "reminder", "See you at 3pm.", "US"),
    ]

    def _gate_says_legal(self, recipient, channel, mtype, content, country):
        try:
            pre_check(
                recipient_id=recipient, channel=channel, message_type=mtype,
                content=content, country_code=country, preview=True,
            )
            return True
        except ComplianceViolationError:
            return False

    def test_preview_verdict_matches_gate(self):
        for recipient, channel, mtype, content, country in self.CASES:
            gate_legal = self._gate_says_legal(recipient, channel, mtype, content, country)
            r = run(handle_check_compliance(
                recipient_id=recipient, content=content, channel=channel,
                message_type=mtype, country_code=country,
            ))
            assert r.result["legal"] is gate_legal, (
                f"disagreement for {recipient}/{channel}/{mtype}: "
                f"tool={r.result['legal']} gate={gate_legal}"
            )


class TestCheckComplianceOverMcp:
    def test_listed_as_read_only_idempotent_tool(self):
        tools = {t["name"]: t for t in _build_tool_list()}
        assert "check_compliance" in tools
        ann = tools["check_compliance"]["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["idempotentHint"] is True
        assert ann["destructiveHint"] is False

    def test_callable_via_mcp_tools_call(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 51, "method": "tools/call",
            "params": {
                "name": "check_compliance",
                "arguments": {
                    "recipient_id": "+14045550200",
                    "content": "20% off this week only!",
                    "channel": "sms",
                    "message_type": "marketing",
                    "country_code": "US",
                },
            },
        }))
        assert "result" in r, f"expected result, got {r}"
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["status"] == "success"
        assert data["result"]["legal"] is False
        assert data["result"]["rule"] == "TCPA_marketing_consent"

    def test_missing_recipient_arg_surfaces_named_param_error(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 52, "method": "tools/call",
            "params": {"name": "check_compliance", "arguments": {"content": "hi"}},
        }))
        assert "error" in r
        assert r["error"]["code"] == -32602
        assert "recipient_id" in r["error"]["message"]

    def test_preview_cost_reports_free(self):
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        resp = run(handle_preview_cost(PreviewCostRequest(
            operation="check_compliance",
            params={"recipient_id": "+14045550200", "content": "hi", "channel": "sms"},
        )))
        assert resp.estimated_cost_usd == 0.0
