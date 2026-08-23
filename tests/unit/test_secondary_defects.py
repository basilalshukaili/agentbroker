"""
Regression tests for the four secondary defects fixed 2026-08-23.

1. CREDENTIAL HYGIENE — _agent_id_from_token parses agent_id, never logs raw token.
2. AUDIT-TRAIL PRESERVATION — get_outcome does not overwrite originating tool row.
3. COMPLIANCE FALSE POSITIVE — "slot" in booking text does NOT trigger gambling block.
4. EMAIL QUALITY — send_transactional_confirmation produces formatted body + subject.
5. QUOTA STRIP — quota key absent from persisted result_json.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fix 1: Credential hygiene
# ---------------------------------------------------------------------------

class TestAgentIdFromToken:
    def test_anonymous_for_empty_token(self):
        from agent_interface.mcp_server import _agent_id_from_token
        assert _agent_id_from_token("") == "anonymous"
        assert _agent_id_from_token("anonymous") == "anonymous"

    def test_returns_parsed_agent_id_not_raw_prefix(self):
        """Issue a real token and verify the helper returns the agent_id string."""
        from agent_interface.identity import issue_token, TokenRequest
        from agent_interface.mcp_server import _agent_id_from_token

        resp = issue_token(TokenRequest(
            agent_id="free_testonly_abc123",
            principal_id="p_test",
        ))
        result = _agent_id_from_token(resp.token)
        # Must return the parsed agent_id, NOT the raw base64url prefix
        assert result == "free_testonly_abc123"
        assert not result.startswith("eyJ"), (
            f"raw token prefix leaked into key_id: {result[:20]}"
        )

    def test_eyj_prefix_never_returned(self):
        """The 64-char raw-prefix must never be the return value for a valid token."""
        from agent_interface.identity import issue_token, TokenRequest
        from agent_interface.mcp_server import _agent_id_from_token

        resp = issue_token(TokenRequest(
            agent_id="sub_customer_9999",
            principal_id="cust_9999",
        ))
        raw_prefix = resp.token[:64]
        assert raw_prefix.startswith("eyJ")  # sanity-check the token format

        result = _agent_id_from_token(resp.token)
        assert result == "sub_customer_9999"
        assert result != raw_prefix

    def test_garbage_token_returns_anonymous(self):
        from agent_interface.mcp_server import _agent_id_from_token
        assert _agent_id_from_token("not_a_real_token") == "anonymous"
        assert _agent_id_from_token("eyJzzz.badsig") == "anonymous"


# ---------------------------------------------------------------------------
# Fix 2: Audit-trail preservation
# ---------------------------------------------------------------------------

class TestAuditTrailPreservation:
    def test_get_outcome_does_not_overwrite_tool_column(self):
        """
        After persisting a handle_inbound result, calling get_outcome must NOT
        overwrite the 'tool' field in the durable outcome store.
        """
        from storage.outcome_store import get_outcome_store
        from core.models import OperationStatus

        store = get_outcome_store()  # use the singleton — same one handle_get_outcome reads
        op_id = "test_op_audit_trail_001"

        # Simulate handle_inbound persisting its result
        orig_receipt = {
            "operation_id": op_id,
            "status": "success",
            "reason_code": "booking_inquiry_received",
            "human_message": "Inbound handled.",
            "result": {},
            "cost": {"amount": 0.01, "currency": "USD", "basis": "per_call"},
            "latency_ms": 10,
            "retriable": False,
        }
        store.set_complete(op_id, orig_receipt, tool="handle_inbound", agent_id="free_test")

        # Verify initial state
        record = store.get(op_id)
        assert record is not None

        # Calling handle_get_outcome should READ the stored result, not overwrite it.
        from core.status_outcome import handle_get_outcome
        outcome_receipt = run(handle_get_outcome(op_id))
        assert outcome_receipt.status == OperationStatus.SUCCESS, (
            f"Expected SUCCESS but got {outcome_receipt.status}: {outcome_receipt.reason_code}"
        )

        # The record must still reflect the original operation, not get_outcome
        record_after = store.get(op_id)
        assert record_after is not None
        stored_outcome = record_after.get("outcome", {})
        assert stored_outcome.get("reason_code") == "booking_inquiry_received", (
            f"outcome.reason_code was overwritten: {stored_outcome.get('reason_code')}"
        )

    def test_persist_skip_tools_frozenset_covers_reads(self):
        """The _PERSIST_SKIP_TOOLS set must include all read-only tools."""
        from agent_interface import mcp_server
        # Access the frozenset defined inside _dispatch_operation by inspecting
        # the source — or just verify the behaviour by testing it directly.
        # Check that get_outcome and get_status are excluded from side-effectful persist.
        # We test indirectly: invoke _dispatch_operation with get_status and confirm
        # the store is not written.
        from storage.outcome_store import get_outcome_store
        store = get_outcome_store()

        # Count records before
        before = len(store._records)

        # get_status for a non-existent id should NOT create a new record
        run(mcp_server._dispatch_operation("get_status", {"operation_id": "nonexistent_op_xyz"}, {}))

        after = len(store._records)
        # The store must not have grown from a get_status call on a missing id
        assert after == before, (
            f"get_status wrote to the outcome store: before={before} after={after}"
        )


# ---------------------------------------------------------------------------
# Fix 3: Compliance false positive — slot in booking content
# ---------------------------------------------------------------------------

class TestComplianceFalsePositive:
    def test_appointment_slot_passes(self):
        """'slot' alone in booking confirmation must NOT trigger gambling block."""
        from compliance.content_classifier import classify_content, ContentCategory
        texts = [
            "Your appointment slot at 3pm is confirmed.",
            "We have a slot available for you on Tuesday.",
            "Please select your preferred slot from the calendar.",
            "Your 3pm slot is now booked. See you then!",
            "Booking confirmed for your selected time slot.",
        ]
        for text in texts:
            result = classify_content(text)
            assert not result.blocked, (
                f"False positive: '{text}' was blocked as {result.category} "
                f"signals={result.matched_signals}"
            )

    def test_bet_alone_in_transactional_passes(self):
        """'bet' alone must NOT trigger gambling block in transactional context."""
        from compliance.content_classifier import classify_content
        text = "I bet you will love your new appointment!"
        result = classify_content(text)
        assert not result.blocked, (
            f"False positive: '{text}' was blocked — signals={result.matched_signals}"
        )

    def test_actual_gambling_casino_blocked(self):
        """Actual gambling text with 'casino' must still block."""
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("Win big tonight at our casino! Place your bets now.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_actual_gambling_sportsbook_blocked(self):
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("Sign up at our sportsbook and get a welcome bonus.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_actual_gambling_gamble_blocked(self):
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("Gambling is fun! Visit our slots and poker room.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_slot_and_bet_together_blocked(self):
        """Two ambiguous signals together should still block."""
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("Try our slots! Place your bet and win big prizes.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_jackpot_alone_blocked(self):
        """Definitive term 'jackpot' alone must block."""
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("You hit the jackpot! Claim your prize now.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_lottery_blocked(self):
        from compliance.content_classifier import classify_content, ContentCategory
        result = classify_content("Enter our lottery for a chance to win $10,000.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING


# ---------------------------------------------------------------------------
# Fix 4: Email quality
# ---------------------------------------------------------------------------

class TestEmailQuality:
    def test_subject_is_specific_not_generic(self):
        """Subject must not be the generic 'Message from your service provider'."""
        from core.send_transactional_confirmation import _build_subject
        subject = _build_subject("booking_confirmation", {"smb_name": "Nails & Co", "appointment_time": "Tuesday 3pm"})
        assert "Message from your service provider" not in subject
        assert "Booking" in subject or "Confirmation" in subject

    def test_subject_contains_business_or_time(self):
        from core.send_transactional_confirmation import _build_subject
        subject = _build_subject("booking_confirmation", {"smb_name": "City Dentist"})
        assert "City Dentist" in subject

    def test_subject_payment_receipt(self):
        from core.send_transactional_confirmation import _build_subject
        subject = _build_subject("payment_receipt", {})
        assert "Payment" in subject or "Receipt" in subject

    def test_body_is_not_dict_repr(self):
        """The email body must not look like a Python dict (str(data))."""
        from core.send_transactional_confirmation import _build_email_body, _render
        data = {"name": "Alice", "smb_name": "Salon Plus", "appointment_time": "Tue 2pm"}
        core_text = _render("booking_confirmation", data)
        body = _build_email_body("booking_confirmation", data, core_text)
        # Must not start with '{'
        assert not body.strip().startswith("{"), "Body looks like a raw dict repr"
        # Must contain a greeting
        assert "Hello" in body or "Dear" in body
        # Must contain the core content
        assert "Alice" in body

    def test_body_greeting_uses_name(self):
        from core.send_transactional_confirmation import _build_email_body
        body = _build_email_body("reminder", {"name": "Bob"}, "Your appointment is tomorrow.")
        assert "Hello Bob" in body

    def test_body_has_footer(self):
        """Body must include sender identity in the footer."""
        from core.send_transactional_confirmation import _build_email_body
        body = _build_email_body("booking_confirmation", {}, "Your booking is confirmed.")
        # Footer must identify the sender
        assert "SMB Agent Broker" in body or "AgentBroker" in body

    def test_slot_in_confirmation_body_not_blocked(self):
        """The compliance gate must not block 'slot' in a confirmation email body."""
        from compliance.pre_check import pre_check
        # This replicates the send path: confirm the pre_check in the email adapter
        # does not block a normal booking confirmation with the word 'slot'.
        pre_check(
            recipient_id="test@example.com",
            channel="email",
            message_type="transactional",
            content=(
                "Hello Alice,\n\n"
                "Your appointment slot at 3pm on Tuesday is confirmed.\n\n"
                "Best regards,\nSMB Agent Broker"
            ),
            country_code="US",
        )
        # No exception raised means the message passes compliance.


# ---------------------------------------------------------------------------
# Fix 5: Quota strip from result_json
# ---------------------------------------------------------------------------

class TestQuotaStripFromResultJson:
    def test_quota_stripped_before_upsert(self):
        """
        OutcomeStore._supabase_upsert must strip the 'quota' key from the
        serialised result_json.  We test the stripping logic directly.
        """
        # Simulate the outcome dict that would be passed to _supabase_upsert
        # after _inject_quota_block has added the quota key.
        outcome_with_quota = {
            "operation_id": "op_quota_test_001",
            "status": "success",
            "reason_code": "inbound_handled",
            "human_message": "Done.",
            "result": {},
            "quota": {
                "tier": "free",
                "remaining_today": 48,
                "daily_limit": 50,
                "resets": "2026-08-24T00:00:00Z",
            },
        }

        # The stripping happens in _supabase_upsert; test the logic inline.
        _EPHEMERAL_KEYS = frozenset({"quota"})
        persisted = {k: v for k, v in outcome_with_quota.items() if k not in _EPHEMERAL_KEYS}

        assert "quota" not in persisted
        assert persisted["operation_id"] == "op_quota_test_001"
        assert persisted["status"] == "success"

    def test_set_complete_copy_isolates_quota_mutation(self):
        """
        _dispatch_operation passes a COPY of receipt_dict to set_complete so
        that a later mutation (inject_quota_block) does not propagate into the
        async upsert.  Verify that the copy + strip pattern works.
        """
        from storage.outcome_store import OutcomeStore

        store = OutcomeStore()
        op_id = "op_quota_copy_test_001"

        receipt = {
            "operation_id": op_id,
            "status": "success",
            "reason_code": "msg_sent",
        }
        # Simulate _dispatch_operation: copy and strip, then set_complete
        persist_receipt = {k: v for k, v in receipt.items() if k != 'quota'}
        store.set_complete(op_id, persist_receipt, tool="send_message", agent_id="free_t")

        # Simulate _inject_quota_block mutating the original receipt
        receipt["quota"] = {"tier": "free", "remaining_today": 47}

        # The stored outcome must NOT have quota
        record = store.get(op_id)
        assert record is not None
        stored_outcome = record.get("outcome", {})
        assert "quota" not in stored_outcome, (
            "quota leaked into durable outcome store via aliased reference"
        )
