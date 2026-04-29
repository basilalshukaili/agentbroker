"""
Unit tests — ComplianceAgent module.
Covers: pre_check, consent_store, content_classifier, jurisdiction_rules,
        recording_consent, campaign_registry.
Target: >85% line coverage on /compliance/.
"""
import pytest
from compliance.consent_store import ConsentStore, ConsentStatus
from compliance.content_classifier import classify_content, ContentCategory
from compliance.jurisdiction_rules import get_rules, requires_two_party_recording_consent
from compliance.pre_check import pre_check
from compliance.campaign_registry import (
    CampaignRegistry, BrandRegistration, CampaignRegistration,
    CampaignStatus, UseCaseType,
)
from core.models import ComplianceViolationError


# ---------------------------------------------------------------------------
# content_classifier
# ---------------------------------------------------------------------------

class TestContentClassifier:
    def test_clean_content_passes(self):
        result = classify_content("Your haircut appointment is confirmed for tomorrow at 10am.")
        assert not result.blocked
        assert result.category == ContentCategory.CLEAN

    def test_gambling_content_blocked(self):
        result = classify_content("Win big at our casino tonight! Place your bets now.")
        assert result.blocked
        assert result.category == ContentCategory.GAMBLING

    def test_cannabis_content_blocked(self):
        result = classify_content("Visit our dispensary for premium cannabis products.")
        assert result.blocked
        assert result.category == ContentCategory.CANNABIS

    def test_lending_content_blocked(self):
        result = classify_content("You have been pre-approved for a payday loan!")
        assert result.blocked
        assert result.category == ContentCategory.LENDING

    def test_otp_content_passes(self):
        result = classify_content("Your verification code is 123456. Valid for 10 minutes.")
        assert not result.blocked


# ---------------------------------------------------------------------------
# consent_store
# ---------------------------------------------------------------------------

class TestConsentStore:
    def setup_method(self):
        self.store = ConsentStore()

    def test_no_consent_returns_false(self):
        assert not self.store.has_valid_consent("+14045551234", "sms", "marketing")

    def test_opt_in_grants_consent(self):
        self.store.record_consent(
            recipient_id="+14045551234",
            channel="sms",
            use_case="marketing",
            status=ConsentStatus.OPTED_IN,
            jurisdiction="US",
            consent_method="express_written",
            consent_source="web_form",
        )
        assert self.store.has_valid_consent("+14045551234", "sms", "marketing")

    def test_opt_out_blocks_consent(self):
        self.store.record_consent(
            recipient_id="+14045551234", channel="sms", use_case="marketing",
            status=ConsentStatus.OPTED_IN, jurisdiction="US",
            consent_method="express_written", consent_source="web_form",
        )
        self.store.revoke_consent("+14045551234", "sms", "marketing", "keyword_STOP")
        assert not self.store.has_valid_consent("+14045551234", "sms", "marketing")

    def test_is_opted_out_after_revocation(self):
        self.store.record_consent(
            recipient_id="+14045551234", channel="sms", use_case="marketing",
            status=ConsentStatus.OPTED_IN, jurisdiction="US",
            consent_method="express_written", consent_source="web_form",
        )
        self.store.revoke_consent("+14045551234", "sms", "marketing", "keyword_STOP")
        assert self.store.is_opted_out("+14045551234", "sms")


# ---------------------------------------------------------------------------
# jurisdiction_rules
# ---------------------------------------------------------------------------

class TestJurisdictionRules:
    def test_california_requires_two_party_recording(self):
        assert requires_two_party_recording_consent("US", "CA")

    def test_texas_is_one_party(self):
        assert not requires_two_party_recording_consent("US", "TX")

    def test_florida_requires_two_party(self):
        assert requires_two_party_recording_consent("US", "FL")

    def test_eu_gdpr_applies(self):
        rules = get_rules("EU")
        assert rules.gdpr_applies

    def test_canada_casl_applies(self):
        rules = get_rules("CA")
        assert rules.casl_applies


# ---------------------------------------------------------------------------
# campaign_registry
# ---------------------------------------------------------------------------

class TestCampaignRegistry:
    def setup_method(self):
        self.registry = CampaignRegistry()
        brand = BrandRegistration(
            brand_id="brand_001", company_name="SMB Broker Inc.",
            ein="12-3456789", street="123 Main St", city="Atlanta",
            state="GA", zip_code="30309",
        )
        self.registry.register_brand(brand)

    def test_sms_not_authorized_without_campaign(self):
        assert not self.registry.is_sms_authorized(UseCaseType.MARKETING)

    def test_sms_authorized_after_campaign_registration(self):
        campaign = CampaignRegistration(
            campaign_id="camp_001", brand_id="brand_001",
            use_case=UseCaseType.MARKETING,
            description="SMB appointment marketing messages",
            status=CampaignStatus.REGISTERED,
        )
        self.registry.register_campaign(campaign)
        assert self.registry.is_sms_authorized(UseCaseType.MARKETING)

    def test_pending_campaign_not_authorized(self):
        campaign = CampaignRegistration(
            campaign_id="camp_002", brand_id="brand_001",
            use_case=UseCaseType.APPOINTMENT_REMINDER,
            description="Appointment reminders",
            status=CampaignStatus.PENDING,
        )
        self.registry.register_campaign(campaign)
        assert not self.registry.is_sms_authorized(UseCaseType.APPOINTMENT_REMINDER)


# ---------------------------------------------------------------------------
# pre_check integration
# ---------------------------------------------------------------------------

class TestPreCheck:
    def setup_method(self):
        # Register a valid campaign so transactional SMS can pass
        from compliance.campaign_registry import get_campaign_registry, CampaignStatus
        reg = get_campaign_registry()
        brand = BrandRegistration(
            brand_id="brand_test", company_name="Test Co", ein="00-0000000",
            street="1 Test St", city="Boston", state="MA", zip_code="02139",
        )
        try:
            reg.register_brand(brand)
        except Exception:
            pass
        campaign = CampaignRegistration(
            campaign_id="camp_test_reminder",
            brand_id="brand_test",
            use_case=UseCaseType.APPOINTMENT_REMINDER,
            description="Appointment reminders",
            status=CampaignStatus.REGISTERED,
        )
        try:
            reg.register_campaign(campaign)
        except Exception:
            pass

    def test_clean_transactional_sms_blocked_without_campaign(self):
        """marketing SMS without campaign → compliance_violation"""
        with pytest.raises(ComplianceViolationError) as exc_info:
            pre_check(
                recipient_id="+14045550999",
                channel="sms",
                message_type="marketing",
                content="20% off this week only!",
                country_code="US",
            )
        assert "consent" in exc_info.value.message.lower() or "10DLC" in exc_info.value.message

    def test_opted_out_recipient_blocked(self):
        from compliance.consent_store import get_consent_store
        store = get_consent_store()
        store.record_consent(
            recipient_id="+10000000001", channel="sms", use_case="marketing",
            status=ConsentStatus.OPTED_IN, jurisdiction="US",
            consent_method="express_written", consent_source="test",
        )
        store.revoke_consent("+10000000001", "sms", "marketing", "STOP")
        with pytest.raises(ComplianceViolationError) as exc_info:
            pre_check(
                recipient_id="+10000000001",
                channel="sms",
                message_type="reminder",
                content="Reminder: your appointment is tomorrow.",
                country_code="US",
            )
        assert "opted out" in exc_info.value.message.lower()

    def test_restricted_content_blocked(self):
        with pytest.raises(ComplianceViolationError) as exc_info:
            pre_check(
                recipient_id="+14045550888",
                channel="email",
                message_type="marketing",
                content="Win big at our online casino tonight!",
                country_code="US",
            )
        assert "restricted" in exc_info.value.message.lower()
