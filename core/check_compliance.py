"""
check_compliance — free, read-only pre-flight for the outbound-messaging
compliance gate.

The problem it solves for a calling agent:
    send_message ($0.02-0.22) and call_business ($0.50) both run every outbound
    through a non-bypassable compliance gate (TCPA / GDPR / CASL / CAN-SPAM /
    10DLC across 26 jurisdictions). If the gate rejects, the agent has already
    committed to the paid call and burned a turn discovering the send was never
    legal. Before spending, an agent wants to know CHEAPLY and INSTANTLY whether
    a (recipient, channel, message_type, content) combination would pass.

This tool answers exactly that by running the SAME `compliance.pre_check`
gate the paid path runs — in preview mode, so NO message is sent and NO audit
event is recorded. It is the free/cheap top-of-funnel read that de-risks the
paid send_message / call_business path, mirroring how check_booking_link
de-risks import_booking_url -> schedule_appointment:

    check_compliance(recipient_id, channel, message_type, content)  # <- $0, instant
      -> send_message(...)          # the paid action, gate runs again at send time

Design notes / honesty guarantees:
  * Single source of truth: the legal/illegal verdict comes from the identical
    `pre_check()` function the real dispatch uses — never a re-implementation
    that could drift from the enforced gate. A `true` here means the gate will
    let the send through (barring live state changes like a fresh opt-out
    between preview and send).
  * Side-effect free: preview=True suppresses every audit-log write. A read-only
    tool must not record an OUTBOUND_DISPATCHED "allow" for a send that never
    happened — that would be the same class of lie as the old stub-SUCCESS bug.
  * A "not compliant" answer is a SUCCESSFUL check (the tool did its job and
    told you the truth), not a failure — same convention as check_booking_link
    returning supported=false. Only malformed input is a FAILURE(bad_input).
  * Scope honesty: this checks the OUTBOUND-MESSAGING gate (content, opt-out,
    marketing consent, 10DLC). It does NOT evaluate two-party voice *recording*
    consent — that is decided at call time inside the voice adapter. The result
    says so explicitly so an agent never over-trusts a voice "legal": true.
"""
from __future__ import annotations

import time
import uuid

from core.models import (
    ComplianceViolationError,
    CostRecord,
    OperationStatus,
    OutcomeReceipt,
)

_VALID_CHANNELS = ("sms", "email", "voice")

# Human remediation copy, keyed by the rule identifier pre_check raises. Mirrors
# main._remediation_for (the public HTTP /compliance/check surface). Kept local
# so this read-only tool layer does not import the web app; the rule names are
# stable contract identifiers, so drift risk is low.
_REMEDIATION = {
    "restricted_content": "Reword the message to remove the restricted category, or seek explicit licensing for the regulated content.",
    "recipient_opted_out": "Honor the opt-out — the recipient has unsubscribed. Do not send. Add to suppression list.",
    "TCPA_marketing_consent": "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers, and pass its consent_record_id.",
    "GDPR_marketing_consent": "Obtain GDPR Article 6/7 consent before marketing email to EU/UK residents.",
    "CASL_marketing_consent": "Obtain explicit CASL consent before commercial electronic messages to Canadian recipients.",
    "10DLC_campaign_not_registered": "Register a 10DLC campaign with The Campaign Registry (TCR) before sending US A2P SMS. Required by US carriers since 2023.",
}


def _remediation_for(rule: str) -> str:
    return _REMEDIATION.get(
        rule, "Review the cited rule in the jurisdiction reference at /compliance/jurisdictions."
    )


def _infer_channel(recipient_id: str) -> str:
    """Best-effort channel from the recipient identifier, mirroring send_message
    (an email address is reachable by email; anything else defaults to sms).
    Voice must be requested explicitly."""
    return "email" if "@" in recipient_id else "sms"


async def handle_check_compliance(
    recipient_id: str,
    content: str,
    channel: str | None = None,
    message_type: str = "transactional",
    country_code: str | None = None,
    state_code: str | None = None,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()

    def _bad_input(msg: str) -> OutcomeReceipt:
        return OutcomeReceipt(
            operation_id=str(uuid.uuid4()),
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=msg,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    # --- input validation -------------------------------------------------
    if not recipient_id or not isinstance(recipient_id, str) or not recipient_id.strip():
        return _bad_input(
            "recipient_id is required — the phone number (E.164, e.g. '+14045550100') "
            "or email address the message would go to."
        )
    if not content or not isinstance(content, str) or not content.strip():
        return _bad_input(
            "content is required — the message body you intend to send. The gate "
            "classifies the actual text, so a preview needs the real content."
        )

    recipient_id = recipient_id.strip()
    channel = (channel or _infer_channel(recipient_id)).strip().lower()
    if channel not in _VALID_CHANNELS:
        return _bad_input(
            f"channel must be one of {list(_VALID_CHANNELS)} (got '{channel}'). "
            "Omit it to auto-infer sms/email from the recipient_id."
        )

    # --- run the identical gate, in preview mode (no send, no audit write) --
    from compliance.pre_check import pre_check

    base_result = {
        "channel": channel,
        "message_type": message_type,
        "jurisdiction": (
            f"{country_code}-{state_code}" if state_code else (country_code or "US")
        ),
        "recording_consent_note": (
            "This is the outbound-messaging gate only. Two-party voice recording "
            "consent is evaluated separately at call time."
            if channel == "voice" else None
        ),
        "checked_live": False,
    }

    try:
        pre_check(
            recipient_id=recipient_id,
            channel=channel,
            message_type=message_type,
            content=content,
            country_code=country_code,
            state_code=state_code,
            agent_id=agent_id,
            trace_id=trace_id,
            preview=True,
        )
    except ComplianceViolationError as cve:
        result = {
            **base_result,
            "legal": False,
            "rule": cve.rule,
            "jurisdiction": cve.jurisdiction,
            "human_message": cve.message,
            "remediation": _remediation_for(cve.rule),
        }
        return OutcomeReceipt(
            operation_id=str(uuid.uuid4()),
            status=OperationStatus.SUCCESS,        # a truthful "no" is a successful check
            reason_code="not_compliant",
            human_message=(
                f"Send would be BLOCKED by the compliance gate ({cve.rule}). "
                f"{cve.message} No message was sent — this was a preview."
            ),
            result=result,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
            next_actions=[
                _remediation_for(cve.rule),
                "Re-run check_compliance once the blocker is resolved, then call send_message.",
            ],
        )

    # --- compliant branch -------------------------------------------------
    result = {**base_result, "legal": True, "rule": None}
    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="compliant",
        human_message=(
            f"Send is permitted under the {result['jurisdiction']} rule set for a "
            f"{message_type} {channel} message. The gate runs again at send time, "
            "so honor any opt-out that lands between now and the send."
        ),
        result=result,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
        next_actions=[
            f"Call send_message(recipient={{id_value:'{recipient_id}'}}, "
            f"message_type='{message_type}', content=...).",
        ],
    )
