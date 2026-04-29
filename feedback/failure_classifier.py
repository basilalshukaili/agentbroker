"""
Failure classifier — maps raw exceptions and reason_codes into the 8-class taxonomy.

Failure classes (from api/errors.md):
  discovery_miss      — agent couldn't find the right business
  selection_miss      — agent chose wrong service/business for the task
  param_misuse        — bad inputs, missing required fields
  execution_failure   — operation attempted but failed mid-flight
  outcome_rejected    — SMB or end-user rejected the outcome
  supply_unreachable  — target SMB could not be contacted
  compliance_violation — pre_check gate triggered
  environmental       — infrastructure, rate limit, timeout
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.models import ErrorCode, FailureClass, ComplianceViolationError


# ---------------------------------------------------------------------------
# Signal maps
# ---------------------------------------------------------------------------

_REASON_CODE_MAP: dict[str, FailureClass] = {
    # Discovery
    "no_results": FailureClass.DISCOVERY_MISS,
    "out_of_supply_network": FailureClass.DISCOVERY_MISS,
    # Selection
    "wrong_vertical": FailureClass.SELECTION_MISS,
    "capability_mismatch": FailureClass.SELECTION_MISS,
    "low_confidence_match": FailureClass.SELECTION_MISS,
    # Params
    "bad_input": FailureClass.PARAM_MISUSE,
    "missing_required_field": FailureClass.PARAM_MISUSE,
    "invalid_schema": FailureClass.PARAM_MISUSE,
    # Execution
    "booking_failed": FailureClass.EXECUTION_FAILURE,
    "dispatch_failed": FailureClass.EXECUTION_FAILURE,
    "channel_exhausted": FailureClass.EXECUTION_FAILURE,
    "timeout": FailureClass.EXECUTION_FAILURE,
    # Outcome
    "smb_declined": FailureClass.OUTCOME_REJECTED,
    "user_cancelled": FailureClass.OUTCOME_REJECTED,
    "duplicate_rejected": FailureClass.OUTCOME_REJECTED,
    # Supply
    "supply_unreachable": FailureClass.SUPPLY_UNREACHABLE,
    "not_found": FailureClass.SUPPLY_UNREACHABLE,
    "smb_not_found": FailureClass.SUPPLY_UNREACHABLE,
    # Compliance
    "compliance_violation": FailureClass.COMPLIANCE_VIOLATION,
    "opted_out": FailureClass.COMPLIANCE_VIOLATION,
    "consent_required": FailureClass.COMPLIANCE_VIOLATION,
    # Environmental
    "rate_limited": FailureClass.ENVIRONMENTAL,
    "upstream_error": FailureClass.ENVIRONMENTAL,
    "storage_timeout": FailureClass.ENVIRONMENTAL,
    "service_unavailable": FailureClass.ENVIRONMENTAL,
}

_ERROR_CODE_MAP: dict[ErrorCode, FailureClass] = {
    ErrorCode.NOT_FOUND: FailureClass.SUPPLY_UNREACHABLE,
    ErrorCode.BAD_INPUT: FailureClass.PARAM_MISUSE,
    ErrorCode.COMPLIANCE_VIOLATION: FailureClass.COMPLIANCE_VIOLATION,
    ErrorCode.SUPPLY_UNREACHABLE: FailureClass.SUPPLY_UNREACHABLE,
    ErrorCode.EXECUTION_FAILED: FailureClass.EXECUTION_FAILURE,
    ErrorCode.TRANSIENT: FailureClass.ENVIRONMENTAL,
    ErrorCode.RATE_LIMITED: FailureClass.ENVIRONMENTAL,
    ErrorCode.UPSTREAM_ERROR: FailureClass.ENVIRONMENTAL,
    ErrorCode.TIMEOUT: FailureClass.ENVIRONMENTAL,
    ErrorCode.CHANNEL_EXHAUSTED: FailureClass.EXECUTION_FAILURE,
    ErrorCode.OUTCOME_REJECTED: FailureClass.OUTCOME_REJECTED,
}

# Keyword patterns for classifying raw exception messages
_MESSAGE_PATTERNS: list[tuple[re.Pattern, FailureClass]] = [
    (re.compile(r"timed? out|timeout|deadline", re.I), FailureClass.ENVIRONMENTAL),
    (re.compile(r"rate.?limit|429|too many requests", re.I), FailureClass.ENVIRONMENTAL),
    (re.compile(r"503|502|504|service unavailable|upstream", re.I), FailureClass.ENVIRONMENTAL),
    (re.compile(r"compliance|tcpa|gdpr|casl|opted.?out|consent", re.I), FailureClass.COMPLIANCE_VIOLATION),
    (re.compile(r"not found|unknown smb|no such", re.I), FailureClass.SUPPLY_UNREACHABLE),
    (re.compile(r"validation|invalid|missing.?field|required", re.I), FailureClass.PARAM_MISUSE),
    (re.compile(r"declined|rejected|cancelled|cancel", re.I), FailureClass.OUTCOME_REJECTED),
    (re.compile(r"booking failed|dispatch failed|channel exhausted", re.I), FailureClass.EXECUTION_FAILURE),
]


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class ClassifiedFailure:
    failure_class: FailureClass
    reason_code: str
    operation: str
    confidence: float          # 0.0 – 1.0
    details: str = ""
    retriable: bool = False
    suggested_fix: str = ""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_failure(
    *,
    reason_code: Optional[str] = None,
    error_code: Optional[ErrorCode] = None,
    exception: Optional[Exception] = None,
    operation: str = "unknown",
) -> ClassifiedFailure:
    """
    Classify a failure into one of the 8 FailureClass buckets.

    Priority order:
      1. ComplianceViolationError (always compliance_violation)
      2. error_code map
      3. reason_code map
      4. Exception message pattern matching
      5. Default: environmental
    """
    # 1. Explicit compliance exception
    if isinstance(exception, ComplianceViolationError):
        return ClassifiedFailure(
            failure_class=FailureClass.COMPLIANCE_VIOLATION,
            reason_code="compliance_violation",
            operation=operation,
            confidence=1.0,
            details=str(exception),
            retriable=False,
            suggested_fix="Check consent status and jurisdiction rules before retrying.",
        )

    # 2. error_code map
    if error_code and error_code in _ERROR_CODE_MAP:
        fc = _ERROR_CODE_MAP[error_code]
        return ClassifiedFailure(
            failure_class=fc,
            reason_code=reason_code or error_code.value,
            operation=operation,
            confidence=0.95,
            retriable=_is_retriable(fc),
            suggested_fix=_suggest_fix(fc),
        )

    # 3. reason_code map
    if reason_code and reason_code in _REASON_CODE_MAP:
        fc = _REASON_CODE_MAP[reason_code]
        return ClassifiedFailure(
            failure_class=fc,
            reason_code=reason_code,
            operation=operation,
            confidence=0.90,
            retriable=_is_retriable(fc),
            suggested_fix=_suggest_fix(fc),
        )

    # 4. Exception message patterns
    if exception:
        msg = str(exception)
        for pattern, fc in _MESSAGE_PATTERNS:
            if pattern.search(msg):
                return ClassifiedFailure(
                    failure_class=fc,
                    reason_code=reason_code or "unknown",
                    operation=operation,
                    confidence=0.70,
                    details=msg[:200],
                    retriable=_is_retriable(fc),
                    suggested_fix=_suggest_fix(fc),
                )

    # 5. Default
    return ClassifiedFailure(
        failure_class=FailureClass.ENVIRONMENTAL,
        reason_code=reason_code or "unknown",
        operation=operation,
        confidence=0.40,
        retriable=True,
        suggested_fix="Wait and retry. If persistent, check upstream service health.",
    )


def _is_retriable(fc: FailureClass) -> bool:
    return fc in {
        FailureClass.ENVIRONMENTAL,
        FailureClass.EXECUTION_FAILURE,
        FailureClass.SUPPLY_UNREACHABLE,
    }


def _suggest_fix(fc: FailureClass) -> str:
    return {
        FailureClass.DISCOVERY_MISS: "Broaden search location or try adjacent vertical.",
        FailureClass.SELECTION_MISS: "Re-run find_business with more specific capability filter.",
        FailureClass.PARAM_MISUSE: "Validate input schema against manifest before calling.",
        FailureClass.EXECUTION_FAILURE: "Retry with exponential backoff; check channel health.",
        FailureClass.OUTCOME_REJECTED: "Contact SMB directly or escalate to human agent.",
        FailureClass.SUPPLY_UNREACHABLE: "Verify smb_id via verify_business; try nearby SMBs.",
        FailureClass.COMPLIANCE_VIOLATION: "Obtain consent / check opt-out status; do not retry until resolved.",
        FailureClass.ENVIRONMENTAL: "Wait and retry. If persistent, check upstream service health.",
    }.get(fc, "Review error details.")
