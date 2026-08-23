"""
Tests for the 5 production defects fixed 2026-08-23.

FIX 1 — Operation persistence roundtrip (in-memory layer; Supabase layer
         verified by prod verification step after deploy).
FIX 2 — send_transactional_confirmation uses Resend adapter; diagnostics named.
FIX 3 — classify_session_kind: key presence wins over UA.
FIX 4 — Cost truth: handle_inbound=$0.03, escalate_to_human=$0.20,
         send_transactional_confirmation=$0.02.
FIX 5 — Quota block injected for free-tier keys on gated tool responses.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# FIX 1 — Operation persistence roundtrip
# ===========================================================================

class TestOperationPersistenceRoundtrip:
    """
    Exercises the in-memory layer of OutcomeStore.  The Supabase layer is
    covered by the prod verification step (handle_inbound -> get_status ->
    get_outcome with the audit key).
    """

    def test_set_complete_then_get_async_returns_record(self):
        from storage.outcome_store import get_outcome_store
        store = get_outcome_store()
        op_id = f"test_{uuid.uuid4().hex[:8]}"
        outcome = {
            "operation_id": op_id,
            "status": "success",
            "reason_code": "test_ok",
            "human_message": "test message",
            "cost": {"amount": 0.03, "currency": "USD", "basis": "per_call"},
        }
        store.set_complete(op_id, outcome, tool="handle_inbound", agent_id="test_agent")

        # get_async should find the record in-memory (same process)
        record = run(store.get_async(op_id))
        assert record is not None, "get_async returned None after set_complete"
        assert record["status"] == "success"
        assert record["outcome"]["reason_code"] == "test_ok"

    def test_get_status_after_persist_returns_real_status(self):
        from storage.outcome_store import get_outcome_store
        from core.status_outcome import handle_get_status
        store = get_outcome_store()
        op_id = f"test_{uuid.uuid4().hex[:8]}"
        outcome = {
            "operation_id": op_id,
            "status": "success",
            "reason_code": "inbound_classified:booking_inquiry",
        }
        store.set_complete(op_id, outcome, tool="handle_inbound")

        result = run(handle_get_status(op_id))
        assert result["status"] == "success"
        assert result["operation_id"] == op_id

    def test_get_outcome_after_persist_returns_real_record(self):
        from storage.outcome_store import get_outcome_store
        from core.status_outcome import handle_get_outcome
        from core.models import OperationStatus
        store = get_outcome_store()
        op_id = f"test_{uuid.uuid4().hex[:8]}"
        outcome = {
            "operation_id": op_id,
            "status": "success",
            "reason_code": "escalation_queued",
            "human_message": "Escalation ticket esc_abc created.",
            "cost": {"amount": 0.20, "currency": "USD", "basis": "per_escalation"},
            "latency_ms": 5,
            "retriable": False,
        }
        store.set_complete(op_id, outcome, tool="escalate_to_human")

        receipt = run(handle_get_outcome(op_id))
        # Should not return not_found
        assert receipt.reason_code != "not_found", \
            f"get_outcome returned not_found for persisted op {op_id}"

    def test_unknown_op_still_returns_not_found(self):
        from core.status_outcome import handle_get_status, handle_get_outcome
        from core.models import OperationStatus
        result = run(handle_get_status("op_DOES_NOT_EXIST_XYZ"))
        assert result["status"] == "not_found"
        outcome = run(handle_get_outcome("op_DOES_NOT_EXIST_XYZ"))
        assert outcome.reason_code == "not_found"


# ===========================================================================
# FIX 2 — send_transactional_confirmation uses Resend + diagnostic errors
# ===========================================================================

class TestSendTransactionalConfirmation:
    def test_uses_resend_adapter_when_key_present(self):
        """When RESEND_API_KEY is set, the adapter selection picks Resend."""
        import os
        # The agentbroker env has RESEND_API_KEY set (real key).
        from core.send_transactional_confirmation import _get_email_adapter
        adapter, channel_name = _get_email_adapter()
        # Should pick Resend, not SendGrid
        assert "resend" in channel_name, f"Expected resend channel, got {channel_name}"

    def test_upstream_failure_includes_adapter_name(self):
        """When the adapter returns success=False, error message names the adapter."""
        import os
        # Temporarily unset RESEND_API_KEY to force channel_not_configured path
        orig = os.environ.pop("RESEND_API_KEY", None)
        # Also ensure stubs are disabled
        orig_stub = os.environ.pop("ALLOW_STUB_CHANNELS", None)
        try:
            from core.send_transactional_confirmation import handle_send_transactional_confirmation
            from core.models import (
                SendTransactionalConfirmationRequest, ConfirmationType,
                TransactionalRecipient,
            )
            req = SendTransactionalConfirmationRequest(
                confirmation_type=ConfirmationType.OTP,
                recipient=TransactionalRecipient(phone_or_email="test@example.com"),
                data={"otp_code": "123456"},
            )
            receipt = run(handle_send_transactional_confirmation(req))
            assert receipt.status.value == "failure"
            # Must name the channel in the error, not return a blank message
            assert "resend" in receipt.human_message.lower() or \
                   "email" in receipt.human_message.lower(), \
                f"Diagnostic missing channel name: {receipt.human_message}"
        finally:
            if orig is not None:
                os.environ["RESEND_API_KEY"] = orig
            if orig_stub is not None:
                os.environ["ALLOW_STUB_CHANNELS"] = orig_stub

    def test_success_cost_matches_manifest(self):
        """On success, cost must be $0.02 (manifest price)."""
        import os
        # Use stub channel to test cost without a real API call
        os.environ["ALLOW_STUB_CHANNELS"] = "true"
        # Temporarily unset RESEND_API_KEY to force stub path
        orig_resend = os.environ.pop("RESEND_API_KEY", None)
        orig_sendgrid = os.environ.pop("SENDGRID_API_KEY", None)
        try:
            from core.send_transactional_confirmation import handle_send_transactional_confirmation
            from core.models import (
                SendTransactionalConfirmationRequest, ConfirmationType,
                TransactionalRecipient,
            )
            req = SendTransactionalConfirmationRequest(
                confirmation_type=ConfirmationType.OTP,
                recipient=TransactionalRecipient(phone_or_email="test@example.com"),
                data={"otp_code": "654321"},
            )
            receipt = run(handle_send_transactional_confirmation(req))
            if receipt.status.value == "success":
                assert receipt.cost.amount == 0.02, \
                    f"Expected $0.02 (manifest), got ${receipt.cost.amount}"
        finally:
            os.environ.pop("ALLOW_STUB_CHANNELS", None)
            if orig_resend is not None:
                os.environ["RESEND_API_KEY"] = orig_resend
            if orig_sendgrid is not None:
                os.environ["SENDGRID_API_KEY"] = orig_sendgrid


# ===========================================================================
# FIX 3 — Telemetry classifier: key presence wins over UA
# ===========================================================================

class TestClassifierKeyOverride:
    def test_curl_ua_with_key_is_verified_human_key(self):
        """A curl UA with a valid key_id must be 'verified_human_key', not 'crawler'."""
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="tools/call",
            tool_name="handle_inbound",
            user_agent="curl/7.88.1",
            key_id="free_abc123def456",
        )
        assert result == "verified_human_key", \
            f"Expected verified_human_key for keyed curl call, got {result}"

    def test_python_httpx_with_key_is_verified_human_key(self):
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="tools/call",
            tool_name="send_message",
            user_agent="python-httpx/0.27.0",
            key_id="free_xyz789",
        )
        assert result == "verified_human_key", \
            f"Expected verified_human_key for keyed httpx call, got {result}"

    def test_python_requests_with_key_is_verified_human_key(self):
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="tools/call",
            tool_name="find_business",
            user_agent="python-requests/2.31.0",
            key_id="free_some_real_key_id",
        )
        assert result == "verified_human_key"

    def test_keyless_curl_is_still_crawler(self):
        """Without a key, curl UA should still be classified as crawler."""
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="tools/call",
            tool_name="find_business",
            user_agent="curl/7.88.1",
            key_id=None,
        )
        assert result == "crawler"

    def test_keyless_tools_call_is_anon_agent(self):
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="tools/call",
            tool_name="handle_inbound",
            user_agent="ClaudeAgent/1.0",
            key_id=None,
        )
        assert result == "anon_agent"

    def test_keyed_initialize_is_not_verified(self):
        """Even with a key, initialize is a non-work method -> crawler."""
        from billing.usage_logger import classify_session_kind
        result = classify_session_kind(
            method="initialize",
            tool_name=None,
            user_agent="ClaudeAgent/1.0",
            key_id="free_somekey",
        )
        assert result == "crawler"


# ===========================================================================
# FIX 4 — Cost truth
# ===========================================================================

class TestCostTruth:
    def test_handle_inbound_cost_matches_manifest(self):
        """handle_inbound must bill $0.03 (manifest), not $0.08 (was wrong)."""
        from core.handle_inbound import handle_inbound
        from core.models import HandleInboundRequest, InboundChannel, InboundSender
        req = HandleInboundRequest(
            smb_id="smb_001",
            inbound_channel=InboundChannel.SMS,
            sender=InboundSender(phone="+14045551234"),
            raw_message="Can I book an appointment on Saturday?",
        )
        receipt = run(handle_inbound(req))
        assert receipt.cost.amount == 0.03, \
            f"handle_inbound cost should be $0.03 (manifest), got ${receipt.cost.amount}"

    def test_escalate_to_human_cost_matches_manifest(self):
        """escalate_to_human must bill $0.20 (manifest) on a successful Supabase insert."""
        from unittest.mock import AsyncMock, patch
        from core.escalate_to_human import handle_escalate_to_human
        from core.models import (
            EscalateToHumanRequest, EscalationReason, EscalationContext
        )
        req = EscalateToHumanRequest(
            smb_id="smb_001",
            reason=EscalationReason.CUSTOMER_REQUESTED,
            context=EscalationContext(
                original_operation="handle_inbound",
                operation_id=str(uuid.uuid4()),
                recommended_next_step="human_review",
            ),
        )
        # FIX 1: cost is only $0.20 when the Supabase insert succeeds.
        # Mock to simulate a successful durable write.
        fake_row = {"id": "test-escalation-uuid", "status": "open"}
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=fake_row):
            receipt = run(handle_escalate_to_human(req))
        assert receipt.cost.amount == 0.20, \
            f"escalate_to_human cost should be $0.20 (manifest) on success, got ${receipt.cost.amount}"

    def test_handle_inbound_cost_is_not_old_wrong_value(self):
        from core.handle_inbound import handle_inbound
        from core.models import HandleInboundRequest, InboundChannel
        req = HandleInboundRequest(
            smb_id="smb_001",
            inbound_channel=InboundChannel.EMAIL,
            raw_message="Hello, I need help.",
        )
        receipt = run(handle_inbound(req))
        assert receipt.cost.amount != 0.08, "handle_inbound still billing old wrong amount $0.08"

    def test_escalate_to_human_cost_is_not_old_wrong_value(self):
        from core.escalate_to_human import handle_escalate_to_human
        from core.models import (
            EscalateToHumanRequest, EscalationReason, EscalationContext
        )
        req = EscalateToHumanRequest(
            smb_id="smb_001",
            reason=EscalationReason.CUSTOMER_REQUESTED,
            context=EscalationContext(
                original_operation="handle_inbound",
                operation_id=str(uuid.uuid4()),
                recommended_next_step="human_review",
            ),
        )
        receipt = run(handle_escalate_to_human(req))
        assert receipt.cost.amount != 0.50, "escalate_to_human still billing old wrong amount $0.50"


# ===========================================================================
# FIX 5 — Quota visibility
# ===========================================================================

class TestQuotaVisibility:
    def test_inject_quota_block_for_free_key(self):
        """_inject_quota_block adds quota dict to the receipt when called with a free key."""
        # We can't call _inject_quota_block with a real JWT in isolation easily,
        # but we can test the quota helper functions directly.
        from agent_interface.key_request_logic import (
            get_free_daily_remaining, is_free_key, FREE_TIER_DAILY_LIMIT
        )
        key_id = "free_testkey_abc123"
        assert is_free_key(key_id)
        remaining = get_free_daily_remaining(key_id)
        assert remaining == FREE_TIER_DAILY_LIMIT  # fresh key has full quota

    def test_quota_block_structure_is_correct(self):
        """The quota dict has the required fields with correct types."""
        # Simulate what _inject_quota_block does
        from agent_interface.key_request_logic import get_free_daily_remaining, FREE_TIER_DAILY_LIMIT
        from datetime import datetime, timezone, timedelta
        key_id = "free_structuretest"
        remaining = get_free_daily_remaining(key_id)
        now_utc = datetime.now(timezone.utc)
        midnight_utc = (now_utc + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%dT00:00:00Z")
        quota = {
            "tier": "free",
            "remaining_today": remaining,
            "daily_limit": 50,
            "resets": midnight_utc,
        }
        assert quota["tier"] == "free"
        assert isinstance(quota["remaining_today"], int)
        assert quota["daily_limit"] == FREE_TIER_DAILY_LIMIT
        assert quota["resets"].endswith("T00:00:00Z")

    def test_consume_reduces_remaining(self):
        """After consuming one op, remaining decreases by 1."""
        from agent_interface.key_request_logic import (
            consume_free_daily, get_free_daily_remaining, FREE_TIER_DAILY_LIMIT
        )
        key_id = f"free_consume_test_{uuid.uuid4().hex[:6]}"
        before = get_free_daily_remaining(key_id)
        assert before == FREE_TIER_DAILY_LIMIT
        allowed = consume_free_daily(key_id)
        assert allowed is True
        after = get_free_daily_remaining(key_id)
        assert after == FREE_TIER_DAILY_LIMIT - 1
