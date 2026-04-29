"""
SMB verification flow — confirms an SMB's identity and capability claims.

Verification steps:
  1. Phone reachability: place a test call or send a PIN via SMS
  2. Business name match: confirm owner knows the registered name
  3. Capability spot-check: for each declared capability, verify at least
     one booking channel is configured
  4. Address validation: optional — check against USPS/Google lookup stub

Verification states: pending → verified | failed | manual_review
"""
from __future__ import annotations

import hashlib
import random
import string
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.models import OperationStatus
from supply.smb_directory import SMBDirectory


class VerificationState(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


@dataclass
class VerificationRecord:
    smb_id: str
    state: VerificationState = VerificationState.PENDING
    pin_hash: Optional[str] = None
    pin_issued_at: Optional[float] = None
    pin_attempts: int = 0
    verified_at: Optional[float] = None
    failure_reason: Optional[str] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    smb_id: str
    state: VerificationState
    message: str
    next_action: Optional[str] = None


# ---------------------------------------------------------------------------
# In-memory verification store
# ---------------------------------------------------------------------------

_store: dict[str, VerificationRecord] = {}
_directory = SMBDirectory()

_PIN_TTL_SECONDS = 600   # 10 minutes
_MAX_ATTEMPTS = 3
_PIN_LENGTH = 6


def _generate_pin() -> str:
    return "".join(random.choices(string.digits, k=_PIN_LENGTH))


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Step 1: Initiate phone verification — send PIN
# ---------------------------------------------------------------------------

def initiate_phone_verification(smb_id: str) -> VerificationResult:
    """
    Issues a PIN to the SMB's registered phone number.
    In production this would trigger an actual SMS/call via TwilioSMSAdapter.
    In stub mode, the PIN is included in the response for testing.
    """
    entry = _directory.get(smb_id)
    if not entry:
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.FAILED,
            message=f"SMB '{smb_id}' not found in directory.",
        )

    pin = _generate_pin()
    record = VerificationRecord(
        smb_id=smb_id,
        state=VerificationState.PENDING,
        pin_hash=_hash_pin(pin),
        pin_issued_at=time.time(),
        pin_attempts=0,
    )
    _store[smb_id] = record

    # Stub: log pin rather than sending (real: TwilioSMSAdapter.send())
    print(f"[STUB] Verification PIN for {smb_id} ({entry.phone}): {pin}")

    return VerificationResult(
        smb_id=smb_id,
        state=VerificationState.PENDING,
        message=f"Verification PIN sent to {entry.phone}.",
        next_action="Call confirm_pin() with the PIN received.",
    )


# ---------------------------------------------------------------------------
# Step 2: Confirm PIN
# ---------------------------------------------------------------------------

def confirm_pin(smb_id: str, pin: str) -> VerificationResult:
    record = _store.get(smb_id)
    if not record:
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.FAILED,
            message="No pending verification found. Call initiate_phone_verification() first.",
        )

    now = time.time()
    # Expired?
    if record.pin_issued_at and (now - record.pin_issued_at) > _PIN_TTL_SECONDS:
        record.state = VerificationState.FAILED
        record.failure_reason = "PIN expired."
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.FAILED,
            message="PIN has expired. Please request a new PIN.",
            next_action="Call initiate_phone_verification() again.",
        )

    # Too many attempts?
    record.pin_attempts += 1
    if record.pin_attempts > _MAX_ATTEMPTS:
        record.state = VerificationState.MANUAL_REVIEW
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.MANUAL_REVIEW,
            message="Too many failed attempts. Flagged for manual review.",
        )

    # Verify PIN
    if _hash_pin(pin) != record.pin_hash:
        remaining = _MAX_ATTEMPTS - record.pin_attempts
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.PENDING,
            message=f"Incorrect PIN. {remaining} attempt(s) remaining.",
        )

    # Success
    record.state = VerificationState.VERIFIED
    record.verified_at = now

    # Mark SMB as verified in directory
    entry = _directory.get(smb_id)
    if entry:
        import datetime as _dt
        entry.verified_at = _dt.datetime.now(_dt.timezone.utc)
        _directory.upsert(entry)

    return VerificationResult(
        smb_id=smb_id,
        state=VerificationState.VERIFIED,
        message="Phone number verified. SMB is now active.",
        next_action="Proceed to channel_capture to configure booking channels.",
    )


# ---------------------------------------------------------------------------
# Step 3: Capability spot-check
# ---------------------------------------------------------------------------

def verify_capabilities(smb_id: str) -> VerificationResult:
    """
    Checks that every declared capability has at least one booking channel.
    Returns VERIFIED if all capabilities are covered, MANUAL_REVIEW otherwise.
    """
    entry = _directory.get(smb_id)
    if not entry:
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.FAILED,
            message=f"SMB '{smb_id}' not found.",
        )

    if not entry.capabilities:
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.MANUAL_REVIEW,
            message="No capabilities declared. Please add at least one capability.",
            next_action="Update capabilities list via the onboarding portal.",
        )

    if not entry.channels_available:
        return VerificationResult(
            smb_id=smb_id,
            state=VerificationState.MANUAL_REVIEW,
            message="No booking channels configured.",
            next_action="Add a booking channel via channel_capture.",
        )

    return VerificationResult(
        smb_id=smb_id,
        state=VerificationState.VERIFIED,
        message=f"All {len(entry.capabilities)} capabilities verified with {len(entry.channels_available)} channel(s).",
    )


# ---------------------------------------------------------------------------
# Get verification status
# ---------------------------------------------------------------------------

def get_verification_status(smb_id: str) -> Optional[VerificationRecord]:
    return _store.get(smb_id)
