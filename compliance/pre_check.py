"""
ComplianceAgent pre-check gate.

Every outbound channel call MUST invoke `pre_check(...)` before dispatching.
This is the only authorization point for outbound communications.
Raises ComplianceViolationError if the communication is not permitted.

Architecture rule: no other module may bypass this check or call channel
adapters directly without first passing through here.
"""
from __future__ import annotations

from typing import Optional

from core.models import ComplianceViolationError
from compliance.consent_store import get_consent_store
from compliance.content_classifier import classify_content
from compliance.jurisdiction_rules import get_rules, infer_jurisdiction
from compliance.campaign_registry import get_campaign_registry, UseCaseType
from compliance.audit_log import AuditEventType, get_audit_log


def pre_check(
    *,
    recipient_id: str,
    channel: str,               # "sms", "email", "voice"
    message_type: str,          # "marketing", "transactional", "reminder", etc.
    content: str,
    country_code: Optional[str] = None,
    state_code: Optional[str] = None,
    agent_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> None:
    """
    Perform all compliance checks. Returns None if compliant.
    Raises ComplianceViolationError with structured detail if not.

    Checks performed (in order):
    1. Content classification (restricted categories)
    2. Opt-out list check
    3. Consent check (for marketing/promotional messages)
    4. 10DLC campaign registration check (for US SMS)
    5. Jurisdiction-specific rules
    """
    rules = infer_jurisdiction(country_code, state_code)
    consent_store = get_consent_store()
    jurisdiction = f"{country_code}-{state_code}" if state_code else (country_code or "US")

    # 1. Content classification
    classification = classify_content(content)
    if classification.blocked:
        _audit_violation(
            "restricted_content",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason=classification.reason,
        )
        raise ComplianceViolationError(
            rule="restricted_content",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=f"Message content contains restricted category ({classification.category.value}) and cannot be sent.",
        )

    # 2. Opt-out check
    if consent_store.is_opted_out(recipient_id, channel):
        _audit_violation(
            "recipient_opted_out",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason="Recipient has opted out of this channel",
        )
        raise ComplianceViolationError(
            rule="recipient_opted_out",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=f"Recipient {recipient_id} has opted out of {channel} communications. Honor opt-out per regulatory requirement.",
        )

    # 3. Consent check for marketing messages
    if message_type == "marketing":
        if channel == "sms" and rules.sms_marketing_requires_prior_express_written_consent:
            if not consent_store.has_valid_consent(recipient_id, "sms", "marketing"):
                _audit_violation(
                    "TCPA_marketing_consent",
                    recipient_id, channel, jurisdiction, agent_id, trace_id,
                    reason="No TCPA prior express written consent on file",
                )
                raise ComplianceViolationError(
                    rule="TCPA_marketing_consent",
                    recipient_id=recipient_id,
                    channel="sms",
                    jurisdiction=jurisdiction,
                    message=f"Recipient {recipient_id} has not opted in to marketing SMS. TCPA prior express written consent is required.",
                )

        if rules.gdpr_applies and not consent_store.has_valid_consent(recipient_id, channel, "marketing"):
            _audit_violation(
                "GDPR_marketing_consent",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
                reason="No GDPR lawful basis / opt-in on file",
            )
            raise ComplianceViolationError(
                rule="GDPR_marketing_consent",
                recipient_id=recipient_id,
                channel=channel,
                jurisdiction=jurisdiction,
                message=f"GDPR requires explicit opt-in consent for marketing messages to EU/UK recipients.",
            )

        if rules.casl_applies and not consent_store.has_valid_consent(recipient_id, channel, "marketing"):
            _audit_violation(
                "CASL_marketing_consent",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
            )
            raise ComplianceViolationError(
                rule="CASL_marketing_consent",
                recipient_id=recipient_id,
                channel=channel,
                jurisdiction=jurisdiction,
                message="CASL requires express consent for commercial electronic messages to Canadian recipients.",
            )

    # 4. 10DLC campaign check for US SMS
    if channel == "sms" and (not country_code or country_code.upper() == "US"):
        use_case_map = {
            "marketing": UseCaseType.MARKETING,
            "transactional": UseCaseType.ACCOUNT_NOTIFICATION,
            "reminder": UseCaseType.APPOINTMENT_REMINDER,
            "otp": UseCaseType.TWO_FACTOR_AUTHENTICATION,
        }
        use_case = use_case_map.get(message_type, UseCaseType.MIXED)
        registry = get_campaign_registry()
        if not registry.is_sms_authorized(use_case):
            _audit_violation(
                "10DLC_campaign_not_registered",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
                reason=f"No registered 10DLC campaign for use case {use_case}",
            )
            raise ComplianceViolationError(
                rule="10DLC_campaign_not_registered",
                recipient_id=recipient_id,
                channel="sms",
                jurisdiction=jurisdiction,
                message=f"No registered 10DLC campaign for use_case={use_case}. A2P SMS requires carrier-registered campaign.",
            )

    # All checks passed — log authorized dispatch
    get_audit_log().record(
        event_type=AuditEventType.OUTBOUND_DISPATCHED,
        agent_id=agent_id,
        recipient_id=recipient_id,
        channel=channel,
        use_case=message_type,
        jurisdiction=jurisdiction,
        decision="allow",
        trace_id=trace_id,
    )


def _audit_violation(
    rule: str,
    recipient_id: str,
    channel: str,
    jurisdiction: str,
    agent_id: Optional[str],
    trace_id: Optional[str],
    reason: str = "",
) -> None:
    get_audit_log().record(
        event_type=AuditEventType.COMPLIANCE_VIOLATION,
        agent_id=agent_id,
        recipient_id=recipient_id,
        channel=channel,
        jurisdiction=jurisdiction,
        decision="deny",
        reason=reason or rule,
        trace_id=trace_id,
        metadata={"rule": rule},
    )
