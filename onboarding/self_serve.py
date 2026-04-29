"""
Self-serve SMB onboarding — collects business profile, verifies it,
and registers the SMB in the directory.

Flow:
  1. SMB submits profile via OnboardingRequest
  2. Validation: required fields, phone format, vertical check
  3. Dedup check: phone or email already registered?
  4. Directory upsert
  5. Channel capture triggered (separate module)
  6. Returns OnboardingReceipt with smb_id and next steps
"""
from __future__ import annotations

import re
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

import datetime as _dt
from core.models import Vertical, OperationStatus
from supply.smb_directory import SMBDirectory, SMBEntry


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

@dataclass
class OnboardingRequest:
    business_name: str
    vertical: Vertical
    owner_name: str
    phone: str                          # E.164 preferred
    email: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)
    booking_channels: list[str] = field(default_factory=list)
    notes: Optional[str] = None


@dataclass
class OnboardingReceipt:
    status: OperationStatus
    smb_id: Optional[str] = None
    message: str = ""
    next_steps: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_request(req: OnboardingRequest) -> list[str]:
    errors: list[str] = []
    if not req.business_name.strip():
        errors.append("business_name is required.")
    if not req.owner_name.strip():
        errors.append("owner_name is required.")
    if not _PHONE_RE.match(req.phone.replace(" ", "").replace("-", "")):
        errors.append(f"phone '{req.phone}' does not appear to be a valid phone number.")
    if req.email and not _EMAIL_RE.match(req.email):
        errors.append(f"email '{req.email}' is not valid.")
    if not req.capabilities:
        errors.append("At least one capability is required (e.g. 'haircut', 'plumbing').")
    return errors


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _generate_smb_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "_", name.lower())[:20]
    short_uid = uuid.uuid4().hex[:6]
    return f"smb_{slug}_{short_uid}"


# ---------------------------------------------------------------------------
# Main onboarding handler
# ---------------------------------------------------------------------------

_directory = SMBDirectory()


def _get_all_smb_ids() -> list[str]:
    """Return all known SMB IDs from the directory's backing store."""
    from supply.smb_directory import _DIRECTORY
    return list(_DIRECTORY.keys())


def onboard_smb(req: OnboardingRequest) -> OnboardingReceipt:
    """
    Validate and register a new SMB. Idempotent by phone number:
    if the phone is already registered, returns the existing smb_id.
    """
    errors = _validate_request(req)
    if errors:
        return OnboardingReceipt(
            status=OperationStatus.FAILURE,
            message="Validation failed.",
            validation_errors=errors,
        )

    normalized_phone = _normalize_phone(req.phone)

    # Dedup check by phone — scan all active SMBs for matching phone
    existing = [e for e in [_directory.get(smb_id) for smb_id in _get_all_smb_ids()] if e]
    for biz in existing:
        if biz.phone == normalized_phone:
            return OnboardingReceipt(
                status=OperationStatus.SUCCESS,
                smb_id=biz.smb_id,
                message=f"Business already registered as '{biz.name}'.",
                next_steps=["Contact support to update your profile."],
            )

    smb_id = _generate_smb_id(req.business_name)
    entry = SMBEntry(
        smb_id=smb_id,
        name=req.business_name,
        vertical=req.vertical,
        phone=normalized_phone,
        email=req.email or "",
        website=req.website or "",
        address=req.address or "",
        city=req.city or "",
        state=req.state or "",
        zip_code=req.zip_code or "",
        capabilities=req.capabilities,
        channels_available=req.booking_channels or ["phone"],
        active=True,
    )
    _directory.upsert(entry)

    next_steps = [
        "Complete channel verification to enable automated booking.",
        "Add your Cal.com username to enable direct scheduling.",
        "Review the capability list and add any missing services.",
    ]
    if not req.email:
        next_steps.insert(0, "Add an email address for transactional confirmations.")

    return OnboardingReceipt(
        status=OperationStatus.SUCCESS,
        smb_id=smb_id,
        message=f"'{req.business_name}' successfully onboarded.",
        next_steps=next_steps,
    )
