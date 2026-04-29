"""
Jurisdiction rules engine.
Determines what consent + channel rules apply to a given recipient based on country/state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RecordingConsentType(str, Enum):
    ONE_PARTY = "one_party"
    TWO_PARTY = "two_party"


@dataclass(frozen=True)
class JurisdictionRules:
    jurisdiction_code: str  # e.g. "US-CA", "US", "EU", "CA-ON"
    sms_marketing_requires_prior_express_written_consent: bool = True
    voice_autodialed_requires_prior_express_consent: bool = True
    recording_consent_type: RecordingConsentType = RecordingConsentType.ONE_PARTY
    gdpr_applies: bool = False
    casl_applies: bool = False
    can_spam_applies: bool = False
    email_requires_unsubscribe: bool = True
    data_residency_region: str = "us"
    pii_retention_days: int = 365
    tcpa_dnc_check_required: bool = False


# Two-party (all-party) recording consent states
_TWO_PARTY_STATES = {"CA", "FL", "IL", "MD", "MA", "MT", "NV", "NH", "PA", "WA"}

_RULES: dict[str, JurisdictionRules] = {
    "US": JurisdictionRules(
        jurisdiction_code="US",
        sms_marketing_requires_prior_express_written_consent=True,
        voice_autodialed_requires_prior_express_consent=True,
        recording_consent_type=RecordingConsentType.ONE_PARTY,
        can_spam_applies=True,
        email_requires_unsubscribe=True,
        pii_retention_days=365,
        tcpa_dnc_check_required=True,
    ),
    "EU": JurisdictionRules(
        jurisdiction_code="EU",
        sms_marketing_requires_prior_express_written_consent=True,
        voice_autodialed_requires_prior_express_consent=True,
        recording_consent_type=RecordingConsentType.TWO_PARTY,
        gdpr_applies=True,
        email_requires_unsubscribe=True,
        data_residency_region="eu",
        pii_retention_days=90,
    ),
    "GB": JurisdictionRules(
        jurisdiction_code="GB",
        gdpr_applies=True,
        email_requires_unsubscribe=True,
        data_residency_region="eu",
        pii_retention_days=90,
    ),
    "CA": JurisdictionRules(
        jurisdiction_code="CA",
        casl_applies=True,
        email_requires_unsubscribe=True,
        pii_retention_days=180,
    ),
    # ----- Conservative international default -----
    # Used for any country we have NOT modeled explicitly.
    # Stance: opt-in required for marketing, transactional allowed,
    # recording consent two-party (safer default), 30-day retention.
    "INTERNATIONAL": JurisdictionRules(
        jurisdiction_code="INTERNATIONAL",
        sms_marketing_requires_prior_express_written_consent=True,
        voice_autodialed_requires_prior_express_consent=True,
        recording_consent_type=RecordingConsentType.TWO_PARTY,
        gdpr_applies=False,
        casl_applies=False,
        can_spam_applies=False,
        email_requires_unsubscribe=True,
        data_residency_region="default",
        pii_retention_days=30,
    ),
    # ----- Worldwide jurisdictions we support out of the box -----
    "AE": JurisdictionRules(jurisdiction_code="AE", email_requires_unsubscribe=True, pii_retention_days=180),    # UAE
    "SA": JurisdictionRules(jurisdiction_code="SA", email_requires_unsubscribe=True, pii_retention_days=180),    # Saudi Arabia
    "OM": JurisdictionRules(jurisdiction_code="OM", email_requires_unsubscribe=True, pii_retention_days=180),    # Oman
    "QA": JurisdictionRules(jurisdiction_code="QA", email_requires_unsubscribe=True, pii_retention_days=180),    # Qatar
    "KW": JurisdictionRules(jurisdiction_code="KW", email_requires_unsubscribe=True, pii_retention_days=180),    # Kuwait
    "BH": JurisdictionRules(jurisdiction_code="BH", email_requires_unsubscribe=True, pii_retention_days=180),    # Bahrain
    "IN": JurisdictionRules(jurisdiction_code="IN", email_requires_unsubscribe=True, pii_retention_days=180),    # India
    "PK": JurisdictionRules(jurisdiction_code="PK", email_requires_unsubscribe=True, pii_retention_days=180),    # Pakistan
    "JP": JurisdictionRules(jurisdiction_code="JP", email_requires_unsubscribe=True, pii_retention_days=90),     # Japan
    "SG": JurisdictionRules(jurisdiction_code="SG", email_requires_unsubscribe=True, pii_retention_days=90,
                             sms_marketing_requires_prior_express_written_consent=True),                         # Singapore PDPA
    "ID": JurisdictionRules(jurisdiction_code="ID", email_requires_unsubscribe=True, pii_retention_days=180),    # Indonesia
    "KR": JurisdictionRules(jurisdiction_code="KR", email_requires_unsubscribe=True, pii_retention_days=90,
                             sms_marketing_requires_prior_express_written_consent=True),                         # South Korea PIPA
    "AU": JurisdictionRules(jurisdiction_code="AU", email_requires_unsubscribe=True, pii_retention_days=180,
                             sms_marketing_requires_prior_express_written_consent=True),                         # Australia Spam Act
    "NZ": JurisdictionRules(jurisdiction_code="NZ", email_requires_unsubscribe=True, pii_retention_days=180),    # New Zealand
    "BR": JurisdictionRules(jurisdiction_code="BR", email_requires_unsubscribe=True, pii_retention_days=180),    # Brazil LGPD
    "MX": JurisdictionRules(jurisdiction_code="MX", email_requires_unsubscribe=True, pii_retention_days=180),    # Mexico
    "FR": JurisdictionRules(jurisdiction_code="FR", gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # France
    "DE": JurisdictionRules(jurisdiction_code="DE", gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Germany
    "IT": JurisdictionRules(jurisdiction_code="IT", gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Italy
    "ES": JurisdictionRules(jurisdiction_code="ES", gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Spain
    "NL": JurisdictionRules(jurisdiction_code="NL", gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Netherlands
}

# State-level overrides for US two-party recording states
for _state in _TWO_PARTY_STATES:
    _key = f"US-{_state}"
    _base = _RULES["US"]
    _RULES[_key] = JurisdictionRules(
        jurisdiction_code=_key,
        sms_marketing_requires_prior_express_written_consent=_base.sms_marketing_requires_prior_express_written_consent,
        voice_autodialed_requires_prior_express_consent=_base.voice_autodialed_requires_prior_express_consent,
        recording_consent_type=RecordingConsentType.TWO_PARTY,
        can_spam_applies=_base.can_spam_applies,
        email_requires_unsubscribe=_base.email_requires_unsubscribe,
        pii_retention_days=_base.pii_retention_days,
        tcpa_dnc_check_required=True,
    )


def get_rules(country_code: str, state_code: str | None = None) -> JurisdictionRules:
    """Return applicable rules for (country_code, state_code).
    Falls back to INTERNATIONAL conservative defaults for unknown countries."""
    if country_code == "US" and state_code and state_code.upper() in _TWO_PARTY_STATES:
        return _RULES[f"US-{state_code.upper()}"]
    return _RULES.get(country_code.upper(), _RULES["INTERNATIONAL"])


def requires_two_party_recording_consent(country_code: str, state_code: str | None = None) -> bool:
    return get_rules(country_code, state_code).recording_consent_type == RecordingConsentType.TWO_PARTY


def infer_jurisdiction(country_code: str | None, state_code: str | None = None) -> JurisdictionRules:
    """Best-effort jurisdiction inference when explicit codes are unavailable.
    Defaults to INTERNATIONAL (conservative) when no country is supplied."""
    if not country_code:
        import os
        default = os.getenv("COMPLIANCE_DEFAULT_JURISDICTION", "international").upper()
        return _RULES.get(default, _RULES["INTERNATIONAL"])
    return get_rules(country_code, state_code)


def list_supported_jurisdictions() -> list[str]:
    """Returns all explicitly-supported country codes (excluding US-state subkeys)."""
    return sorted([k for k in _RULES.keys() if "-" not in k])
