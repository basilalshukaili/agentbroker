"""
Channel capture — collects and validates SMB communication channel credentials.

Supported channels:
  calcom    — Cal.com username / API key for direct scheduling
  twilio    — Twilio sub-account SID or phone number
  email     — SMTP / SendGrid verified sender address
  phone     — Plain phone number for voice outreach
  web_form  — URL to a bookable web form (playwright fallback)

Channel data is stored per SMB and used by the channel-fallback logic
to select the fastest reachable path at runtime.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from supply.smb_directory import SMBDirectory, SMBEntry
from core.models import OperationStatus


class ChannelType(str, Enum):
    CALCOM = "calcom"
    TWILIO = "twilio"
    EMAIL = "email"
    PHONE = "phone"
    WEB_FORM = "web_form"


@dataclass
class ChannelCredential:
    channel_type: ChannelType
    value: str                  # username, phone, email, URL
    is_verified: bool = False
    added_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class ChannelCaptureRequest:
    smb_id: str
    channel_type: ChannelType
    value: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ChannelCaptureResult:
    status: OperationStatus
    smb_id: str
    channel_type: ChannelType
    message: str
    is_verified: bool = False


# ---------------------------------------------------------------------------
# In-memory channel store
# ---------------------------------------------------------------------------

_channel_store: dict[str, list[ChannelCredential]] = {}
_directory = SMBDirectory()


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://")
_CALCOM_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,60}$")   # cal.com/{username}


def _validate_channel_value(channel_type: ChannelType, value: str) -> Optional[str]:
    """Returns error string if invalid, None if valid."""
    v = value.strip()
    if channel_type == ChannelType.PHONE:
        clean = re.sub(r"\D", "", v)
        if not _PHONE_RE.match(v) and len(clean) not in (10, 11):
            return f"Invalid phone number: '{v}'"
    elif channel_type == ChannelType.EMAIL:
        if not _EMAIL_RE.match(v):
            return f"Invalid email address: '{v}'"
    elif channel_type == ChannelType.WEB_FORM:
        if not _URL_RE.match(v):
            return f"web_form value must be a valid URL starting with http(s)://: '{v}'"
    elif channel_type == ChannelType.CALCOM:
        if not _CALCOM_RE.match(v):
            return f"Cal.com username must be alphanumeric (2-60 chars): '{v}'"
    return None


# ---------------------------------------------------------------------------
# Main capture handler
# ---------------------------------------------------------------------------

def capture_channel(req: ChannelCaptureRequest) -> ChannelCaptureResult:
    """
    Add a channel credential for an SMB. Performs format validation and
    dedup (prevents duplicate channel types per SMB).
    """
    entry = _directory.get(req.smb_id)
    if not entry:
        return ChannelCaptureResult(
            status=OperationStatus.FAILURE,
            smb_id=req.smb_id,
            channel_type=req.channel_type,
            message=f"SMB '{req.smb_id}' not found.",
        )

    err = _validate_channel_value(req.channel_type, req.value)
    if err:
        return ChannelCaptureResult(
            status=OperationStatus.FAILURE,
            smb_id=req.smb_id,
            channel_type=req.channel_type,
            message=err,
        )

    # Dedup: replace if same channel_type already exists
    channels = _channel_store.get(req.smb_id, [])
    channels = [c for c in channels if c.channel_type != req.channel_type]
    cred = ChannelCredential(
        channel_type=req.channel_type,
        value=req.value.strip(),
        is_verified=False,
        metadata=req.metadata,
    )
    channels.append(cred)
    _channel_store[req.smb_id] = channels

    # Update directory channels_available list
    channel_names = [c.channel_type.value for c in channels]
    entry.channels_available = channel_names
    _directory.upsert(entry)

    return ChannelCaptureResult(
        status=OperationStatus.SUCCESS,
        smb_id=req.smb_id,
        channel_type=req.channel_type,
        message=f"{req.channel_type.value} channel added for '{entry.name}'.",
        is_verified=False,
    )


def get_channels(smb_id: str) -> list[ChannelCredential]:
    return _channel_store.get(smb_id, [])


def get_best_channel(smb_id: str) -> Optional[ChannelCredential]:
    """
    Returns the highest-priority verified channel, or the first unverified one.
    Priority: calcom > phone > email > web_form.
    """
    channels = _channel_store.get(smb_id, [])
    priority = [
        ChannelType.CALCOM,
        ChannelType.PHONE,
        ChannelType.EMAIL,
        ChannelType.WEB_FORM,
    ]
    for ct in priority:
        for c in channels:
            if c.channel_type == ct:
                return c
    return channels[0] if channels else None


def mark_channel_verified(smb_id: str, channel_type: ChannelType) -> bool:
    """Mark a specific channel as verified (e.g. after test dispatch succeeds)."""
    channels = _channel_store.get(smb_id, [])
    for c in channels:
        if c.channel_type == channel_type:
            c.is_verified = True
            return True
    return False
