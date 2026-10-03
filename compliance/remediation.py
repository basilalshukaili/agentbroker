"""What to do about each rule the compliance gate can refuse a send under - one table, one function.

Two copies of this table used to exist, core/check_compliance.py's and main.py's `_remediation_for`
(the public HTTP /compliance/check), kept "local" so the read-only tool layer did not import the web app. They
had already drifted: the HTTP one dropped a sentence and the unknown-rule fallback pointed at a different page.
Nothing here imports the web app or the gate, so both surfaces read the same text.

A rule id that is a statute names the statute (TCPA_marketing_consent, GDPR_marketing_consent,
CASL_marketing_consent). A rule id that is the service's own policy does not (sms_marketing_consent,
quiet_hours, voice_marketing_consent, email_marketing_consent): applying a country's law by name is a claim
the gate may only make where it implements that law.
"""
from __future__ import annotations

REMEDIATION = {
    "restricted_content": "Reword the message to remove the restricted category, or seek explicit licensing for the regulated content.",
    "recipient_opted_out": "Honor the opt-out — the recipient has unsubscribed. Do not send. Add to suppression list.",
    "TCPA_marketing_consent": "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers, and pass its consent_record_id.",
    "GDPR_marketing_consent": "Obtain GDPR Article 6/7 consent before sending marketing messages to EU/UK recipients.",
    "CASL_marketing_consent": "Obtain explicit CASL consent before commercial electronic messages to Canadian recipients.",
    "10DLC_campaign_not_registered": "Register a 10DLC campaign with The Campaign Registry (TCR) before sending US A2P SMS. Required by US carriers since 2023.",
    "10DLC_unregistered": "Register your sending number under a 10DLC campaign with The Campaign Registry before sending US A2P SMS.",
    "sms_marketing_consent": (
        "Record the recipient's opt-in consent for marketing SMS and pass its consent_record_id, or send a "
        "non-marketing message type. Outside the jurisdictions with a modeled statute this gate applies the "
        "service's conservative opt-in default - see rule_set in the result for which one applied."),
    "voice_marketing_consent": (
        "Obtain and record the recipient's prior express consent for marketing calls, or use a non-marketing "
        "purpose for the call."),
    "whatsapp_marketing_consent": (
        "Record the recipient's opt-in for marketing on WhatsApp and pass its consent_record_id, or send a "
        "non-marketing message type. An opt-in given for SMS, email or calls does not cover WhatsApp, and this "
        "service's own policy asks for it in every country."),
    "marketing_consent": (
        "Record the recipient's opt-in for marketing on this channel and pass its consent_record_id, or send a "
        "non-marketing message type. An opt-in given for another channel does not cover it."),
    "jurisdiction_conflict": (
        "The recipient's number belongs to one country and country_code names another. For a marketing or "
        "follow-up message the gate does not choose between them. Pass the country_code the number belongs to "
        "(the answer names it), or correct the recipient number, then run the check again."),
    "email_marketing_consent": (
        "Obtain and record the recipient's opt-in for marketing email, or send a non-marketing message type."),
    "TCPA_quiet_hours": (
        "Wait until the recipient's permitted calling window opens (the retry time is in the message), or send a "
        "non-solicitation message type. Transactional messages are unaffected."),
    "quiet_hours": (
        "Wait until the recipient's permitted solicitation window opens (the retry time is in the message), or "
        "send a non-solicitation message type. Transactional messages are unaffected."),
    "jurisdiction_required": (
        "Pass country_code (ISO 3166-1 alpha-2, e.g. 'OM') or an E.164 recipient number such as "
        "'+96891234567', so the right rule set can be selected. A +1 or +7 number needs country_code."),
    "restricted_content_jev_advisory": (
        "The deterministic gate found nothing, but the additional jev "
        "restricted-category read flagged this content (jev catches "
        "Arabic/Cyrillic/obfuscated restricted-category phrasing the regex "
        "classifier measurably misses). Re-word to remove any restricted "
        "category, in any language or script, then re-run check_compliance. "
        "This signal is advisory, not authoritative — see result.jev_advisory."
    ),
}

FALLBACK = "Review the cited rule in the jurisdiction reference at /compliance/jurisdictions."


def remediation_for(rule: str) -> str:
    return REMEDIATION.get(rule, FALLBACK)
