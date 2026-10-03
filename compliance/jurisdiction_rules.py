"""
Jurisdiction rules engine.
Determines what consent + channel rules apply to a given recipient based on country/state.
"""
from __future__ import annotations

import re
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
    # The statutes this jurisdiction's rules are MODELED ON. EMPTY MEANS NONE IS: the rules are then the
    # service's own conservative default (recorded opt-in for marketing, an 08:00-21:00 local solicitation
    # window), and every answer that applies them must say so rather than cite a law that was not applied.
    # This field is what stopped being implicit on 2026-10-04: the defaults above are US-shaped, so a rule set
    # with no statute named here used to be reported as "TCPA" because that was the only name the gate knew.
    statutes: tuple = ()


_GDPR = ("GDPR",)

# Two-party (all-party) recording consent states
_TWO_PARTY_STATES = {"CA", "FL", "IL", "MD", "MA", "MT", "NV", "NH", "PA", "WA"}

_RULES: dict[str, JurisdictionRules] = {
    "US": JurisdictionRules(
        jurisdiction_code="US",
        statutes=("TCPA", "CAN-SPAM", "10DLC"),
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
        statutes=_GDPR,
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
        statutes=_GDPR,
        gdpr_applies=True,
        email_requires_unsubscribe=True,
        data_residency_region="eu",
        pii_retention_days=90,
    ),
    "CA": JurisdictionRules(
        jurisdiction_code="CA",
        statutes=("CASL",),
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
    "FR": JurisdictionRules(jurisdiction_code="FR", statutes=_GDPR, gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # France
    "DE": JurisdictionRules(jurisdiction_code="DE", statutes=_GDPR, gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Germany
    "IT": JurisdictionRules(jurisdiction_code="IT", statutes=_GDPR, gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Italy
    "ES": JurisdictionRules(jurisdiction_code="ES", statutes=_GDPR, gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Spain
    "NL": JurisdictionRules(jurisdiction_code="NL", statutes=_GDPR, gdpr_applies=True, email_requires_unsubscribe=True,
                             data_residency_region="eu", pii_retention_days=90),                                 # Netherlands
}

# State-level overrides for US two-party recording states
for _state in _TWO_PARTY_STATES:
    _key = f"US-{_state}"
    _base = _RULES["US"]
    _RULES[_key] = JurisdictionRules(
        jurisdiction_code=_key,
        statutes=_base.statutes,
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


# ---------------------------------------------------------------------------
# Saying what a rule set IS, honestly
# ---------------------------------------------------------------------------

_DEFAULT_POLICY = ("recorded opt-in consent for marketing, and a 08:00-21:00 local solicitation window")
_SAFE_COUNTRY = re.compile(r"^[A-Z0-9-]{2,8}$")


def _clean_country(country_code) -> str | None:
    """The country as it may appear in OUR sentences: a plain code, or None. Never caller prose."""
    if not isinstance(country_code, str):
        return None
    cc = country_code.strip().upper()
    return cc if _SAFE_COUNTRY.match(cc) else (None if not cc else "?")


def _has_own_entry(cc: str | None) -> bool:
    return bool(cc) and cc in _RULES and cc != "INTERNATIONAL"


def consent_basis_sentence(country_code, what: str = "marketing SMS") -> str:
    """Why a marketing message to this country needs recorded opt-in, in words that do not name a law that was
    not applied. `what` names the kind of message ("marketing SMS", "marketing calls", "marketing email").
    For a jurisdiction with a modeled statute the statute's own rule id says it, so this is only used where
    none is."""
    cc = _clean_country(country_code)
    tail = ("recorded opt-in consent is required for " + what + ". This is the service's own policy, not a "
            "citation of {law} law.")
    if cc is None:
        return ("No country was supplied and none could be read from the recipient, so the INTERNATIONAL "
                "conservative default applies: " + tail.replace("{law}", "any country's"))
    shown = cc if cc != "?" else "the supplied country_code"
    if _has_own_entry(cc):
        return (f"No {cc}-specific consent statute is implemented in this gate, so the service's conservative "
                f"default applies: " + tail.replace("{law}", cc))
    return (f"No rule set is implemented for {shown}, so the INTERNATIONAL conservative default applies: "
            + tail.replace("{law}", shown))


def describe_rule_set(country_code, state_code: str | None = None) -> dict:
    """{code, basis, statutes_modeled, note} for the rule set a send to this country is judged under.

    basis is "statute" when the rule set models a named statute and "conservative_default" when it does not -
    which is the honest description of every jurisdiction other than the US, the EU/UK states and Canada."""
    cc = _clean_country(country_code)
    rules = infer_jurisdiction(cc if cc != "?" else "ZZ", state_code)
    if rules.statutes:
        return {"code": rules.jurisdiction_code, "basis": "statute",
                "statutes_modeled": list(rules.statutes),
                "note": f"Modeled: {', '.join(rules.statutes)}."}
    if cc is None:
        note = ("No country was supplied and none could be read from the recipient, so the INTERNATIONAL "
                f"conservative default applies ({_DEFAULT_POLICY}). It is the service's own policy, not a "
                "citation of any country's law.")
    elif _has_own_entry(cc):
        note = (f"No {cc}-specific consent statute is implemented in this gate; the service's conservative "
                f"default applies ({_DEFAULT_POLICY}). It is the service's own policy, not a citation of {cc} law.")
    else:
        shown = cc if cc != "?" else "the supplied country_code"
        note = (f"No rule set is implemented for {shown}, so the INTERNATIONAL conservative default applies "
                f"({_DEFAULT_POLICY}). It is the service's own policy, not a citation of {shown} law.")
    return {"code": rules.jurisdiction_code, "basis": "conservative_default",
            "statutes_modeled": [], "note": note}


def describe_resolved(resolution, state_code: str | None = None) -> dict:
    """describe_rule_set for a number_jurisdiction.Resolution - except that a solicitation whose recipient number
    and country_code name different countries has NO rule set: the gate refused to choose, so it says nothing
    about "the rule set that applied" (basis "undecided") rather than describing a default it did not use."""
    if getattr(resolution, "contradicts", False) and resolution.country is None:
        return {"code": None, "basis": "undecided", "statutes_modeled": [],
                "note": "No rule set was applied: " + (resolution.conflict or "the recipient number and "
                                                       "country_code name different countries.")}
    return describe_rule_set(resolution.country, state_code)


def rule_sets_listing() -> list[dict]:
    """One entry per supported code: what its rule set is based on. Used by the public jurisdiction list."""
    out = []
    for code in list_supported_jurisdictions():
        rules = _RULES[code]
        out.append({"code": code,
                    "basis": "statute" if rules.statutes else "conservative_default",
                    "statutes_modeled": list(rules.statutes)})
    return out
