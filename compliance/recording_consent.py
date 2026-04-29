"""
Recording consent management for voice calls.
Determines whether recording consent must be obtained and tracks confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from compliance.jurisdiction_rules import requires_two_party_recording_consent
from compliance.audit_log import AuditEventType, get_audit_log


@dataclass
class RecordingConsentStatus:
    required: bool
    confirmed: bool
    jurisdiction: str
    consent_type: str  # "one_party" or "two_party"
    confirmed_at: Optional[datetime] = None


_confirmed: dict[str, RecordingConsentStatus] = {}


def _call_key(call_id: str) -> str:
    return call_id


def check_recording_consent_required(
    call_id: str,
    country_code: str,
    state_code: Optional[str],
    agent_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> RecordingConsentStatus:
    """
    Determine if recording consent must be obtained for this call.
    Called before initiating a voice recording.
    """
    two_party = requires_two_party_recording_consent(country_code, state_code)
    jurisdiction = f"{country_code}-{state_code}" if state_code else country_code

    status = RecordingConsentStatus(
        required=two_party,
        confirmed=False,
        jurisdiction=jurisdiction,
        consent_type="two_party" if two_party else "one_party",
    )
    _confirmed[_call_key(call_id)] = status

    if two_party:
        get_audit_log().record(
            event_type=AuditEventType.RECORDING_CONSENT_PROMPTED,
            agent_id=agent_id,
            channel="voice",
            jurisdiction=jurisdiction,
            trace_id=trace_id,
            metadata={"call_id": call_id},
        )

    return status


def confirm_recording_consent(
    call_id: str,
    confirmed: bool,
    agent_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> RecordingConsentStatus:
    """Record the outcome of a recording consent prompt."""
    status = _confirmed.get(_call_key(call_id))
    if not status:
        raise ValueError(f"No recording consent check found for call {call_id}")

    event = (
        AuditEventType.RECORDING_CONSENT_CONFIRMED
        if confirmed
        else AuditEventType.RECORDING_CONSENT_DECLINED
    )
    updated = RecordingConsentStatus(
        required=status.required,
        confirmed=confirmed,
        jurisdiction=status.jurisdiction,
        consent_type=status.consent_type,
        confirmed_at=datetime.now(timezone.utc) if confirmed else None,
    )
    _confirmed[_call_key(call_id)] = updated

    get_audit_log().record(
        event_type=event,
        agent_id=agent_id,
        channel="voice",
        jurisdiction=status.jurisdiction,
        trace_id=trace_id,
        metadata={"call_id": call_id, "confirmed": confirmed},
    )
    return updated


def is_recording_allowed(call_id: str) -> bool:
    """Returns True if recording is allowed for this call."""
    status = _confirmed.get(_call_key(call_id))
    if not status:
        return False
    if not status.required:
        return True  # one-party state — recording allowed without explicit consent
    return status.confirmed


CONSENT_PROMPT_TEXT = (
    "This call may be recorded for quality and training purposes. "
    "By remaining on the line, you consent to being recorded."
)
