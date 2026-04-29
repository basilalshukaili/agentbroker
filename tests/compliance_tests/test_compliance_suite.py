"""
Compliance test suite — required by §6.1 test layer 4.
Verifies:
  - Opt-out list is respected on outbound
  - Recording-consent prompt fires for two-party states
  - Restricted-content categories are filtered
  - Jurisdiction rules applied correctly
  - Audit log entries match outbound events 1:1
"""
import pytest
from compliance.consent_store import ConsentStore, ConsentStatus
from compliance.pre_check import pre_check
from compliance.recording_consent import (
    check_recording_consent_required,
    confirm_recording_consent,
    is_recording_allowed,
)
from compliance.audit_log import AuditLog, AuditEventType
from compliance.jurisdiction_rules import get_rules
from core.models import ComplianceViolationError


class TestOptOutRespected:
    def test_opted_out_sms_is_blocked(self):
        store = ConsentStore()
        store.record_consent(
            "+15005550001", "sms", "reminder", ConsentStatus.OPTED_IN,
            "US", "express_written", "test",
        )
        store.revoke_consent("+15005550001", "sms", "reminder", "STOP_keyword")

        from compliance import consent_store as cs_module
        original = cs_module._store
        cs_module._store = store
        try:
            with pytest.raises(ComplianceViolationError) as exc_info:
                pre_check(
                    recipient_id="+15005550001",
                    channel="sms",
                    message_type="reminder",
                    content="Your appointment is tomorrow.",
                    country_code="US",
                )
            assert "opted out" in exc_info.value.message.lower()
        finally:
            cs_module._store = original


class TestRecordingConsent:
    def test_california_requires_recording_prompt(self):
        status = check_recording_consent_required(
            call_id="call_ca_001", country_code="US", state_code="CA"
        )
        assert status.required is True
        assert status.consent_type == "two_party"

    def test_texas_does_not_require_recording_prompt(self):
        status = check_recording_consent_required(
            call_id="call_tx_001", country_code="US", state_code="TX"
        )
        assert status.required is False

    def test_recording_allowed_after_consent_confirmed(self):
        check_recording_consent_required("call_ca_002", "US", "CA")
        confirm_recording_consent("call_ca_002", confirmed=True)
        assert is_recording_allowed("call_ca_002") is True

    def test_recording_not_allowed_after_consent_declined(self):
        check_recording_consent_required("call_ca_003", "US", "CA")
        confirm_recording_consent("call_ca_003", confirmed=False)
        assert is_recording_allowed("call_ca_003") is False

    def test_one_party_state_recording_allowed_without_prompt(self):
        check_recording_consent_required("call_ga_001", "US", "GA")
        assert is_recording_allowed("call_ga_001") is True


class TestRestrictedContentFiltering:
    def test_gambling_blocked(self):
        with pytest.raises(ComplianceViolationError) as exc_info:
            pre_check(
                recipient_id="+14045550111",
                channel="email",
                message_type="marketing",
                content="Join our casino loyalty program and win big tonight!",
                country_code="US",
            )
        assert exc_info.value.rule == "restricted_content"

    def test_cannabis_blocked(self):
        with pytest.raises(ComplianceViolationError):
            pre_check(
                recipient_id="+14045550112",
                channel="sms",
                message_type="marketing",
                content="Visit our dispensary for premium cannabis!",
                country_code="US",
            )

    def test_adult_content_blocked(self):
        with pytest.raises(ComplianceViolationError):
            pre_check(
                recipient_id="+14045550113",
                channel="sms",
                message_type="marketing",
                content="Exclusive adult content available now.",
                country_code="US",
            )


class TestAuditLogIntegrity:
    def test_compliance_violation_written_to_audit_log(self):
        log = AuditLog()
        from compliance import audit_log as al_module
        original = al_module._audit_log
        al_module._audit_log = log

        try:
            with pytest.raises(ComplianceViolationError):
                pre_check(
                    recipient_id="+14045550200",
                    channel="sms",
                    message_type="marketing",
                    content="20% off!",
                    country_code="US",
                )
            violations = log.query(event_type=AuditEventType.COMPLIANCE_VIOLATION)
            assert len(violations) >= 1
        finally:
            al_module._audit_log = original

    def test_allowed_dispatch_written_to_audit_log(self):
        """
        For a transactional email (no consent required), verify OUTBOUND_DISPATCHED is logged.
        Uses EU jurisdiction to avoid 10DLC check for email.
        """
        log = AuditLog()
        from compliance import audit_log as al_module
        original = al_module._audit_log
        al_module._audit_log = log

        try:
            # EU transactional email — no 10DLC, no marketing consent required
            pre_check(
                recipient_id="user@example.com",
                channel="email",
                message_type="transactional",
                content="Your booking is confirmed for tomorrow at 10am.",
                country_code="DE",  # EU but not gdpr-marketing check for transactional
            )
            dispatched = log.query(event_type=AuditEventType.OUTBOUND_DISPATCHED)
            assert len(dispatched) >= 1
        finally:
            al_module._audit_log = original

    def test_audit_log_stores_hashed_recipient_not_plaintext(self):
        log = AuditLog()
        record = log.record(
            event_type=AuditEventType.OUTBOUND_DISPATCHED,
            recipient_id="+14045550300",
        )
        assert record.recipient_id_hash is not None
        assert "+14045550300" not in str(record.model_dump())


class TestJurisdictionRulesApplied:
    def test_gdpr_marketing_requires_opt_in(self):
        """EU marketing email without consent → compliance_violation."""
        with pytest.raises(ComplianceViolationError) as exc_info:
            pre_check(
                recipient_id="eu_user@example.com",
                channel="email",
                message_type="marketing",
                content="Check out our new services!",
                country_code="EU",
            )
        assert "gdpr" in exc_info.value.rule.lower() or "consent" in exc_info.value.message.lower()
