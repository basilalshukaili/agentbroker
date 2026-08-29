"""
Consent store — per-recipient × channel × use-case opt-in/opt-out records.
Backed by PostgreSQL. All writes are append-only; revocations are new records.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ConsentStatus(str, Enum):
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"
    PENDING = "pending"


class ConsentRecord(BaseModel):
    consent_id: str
    recipient_id: str       # phone number, email, or internal customer_id
    channel: str            # "sms", "email", "voice"
    use_case: str           # "marketing", "transactional", "reminder", etc.
    status: ConsentStatus
    jurisdiction: str
    consent_method: str     # "express_written", "express_verbal", "implied", "opt_in_form"
    consent_source: str     # "web_form", "sms_keyword", "agent_capture", etc.
    recorded_at: datetime
    expires_at: Optional[datetime] = None
    evidence_ref: Optional[str] = None  # URL or ID of consent evidence artifact
    revoked_at: Optional[datetime] = None
    revocation_method: Optional[str] = None


class ConsentStore:
    """
    In-memory implementation for tests; swap for PostgreSQL-backed in production.
    The interface is what matters — tests run against this; production uses the DB adapter.
    """

    def __init__(self) -> None:
        self._records: dict[str, ConsentRecord] = {}
        # Enforcement set of (recipient_id, channel) that have opted out. Kept
        # separate from consent records so a STOP with NO prior opt-in still
        # blocks (the common case), and hydrated from the durable consent_optouts
        # table on startup so an opt-out survives a process restart.
        self._opted_out: set[tuple[str, str]] = set()

    def record_consent(
        self,
        recipient_id: str,
        channel: str,
        use_case: str,
        status: ConsentStatus,
        jurisdiction: str,
        consent_method: str,
        consent_source: str,
        expires_at: datetime | None = None,
        evidence_ref: str | None = None,
    ) -> ConsentRecord:
        record = ConsentRecord(
            consent_id=str(uuid.uuid4()),
            recipient_id=recipient_id,
            channel=channel,
            use_case=use_case,
            status=status,
            jurisdiction=jurisdiction,
            consent_method=consent_method,
            consent_source=consent_source,
            recorded_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            evidence_ref=evidence_ref,
        )
        key = self._key(recipient_id, channel, use_case)
        self._records[key] = record
        return record

    def get_consent(
        self,
        recipient_id: str,
        channel: str,
        use_case: str,
    ) -> ConsentRecord | None:
        return self._records.get(self._key(recipient_id, channel, use_case))

    def has_valid_consent(
        self,
        recipient_id: str,
        channel: str,
        use_case: str,
    ) -> bool:
        record = self.get_consent(recipient_id, channel, use_case)
        if not record:
            return False
        if record.status != ConsentStatus.OPTED_IN:
            return False
        if record.revoked_at:
            return False
        if record.expires_at and record.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def revoke_consent(
        self,
        recipient_id: str,
        channel: str,
        use_case: str,
        revocation_method: str,
    ) -> bool:
        # A STOP/opt-out MUST always register enforcement, even when there is no
        # prior opt-in record (the normal case). Record it in the opted-out set
        # that is_opted_out() consults; also flip any existing consent record.
        self._opted_out.add((recipient_id, channel))
        key = self._key(recipient_id, channel, use_case)
        record = self._records.get(key)
        if record:
            self._records[key] = record.model_copy(update={
                "status": ConsentStatus.OPTED_OUT,
                "revoked_at": datetime.now(timezone.utc),
                "revocation_method": revocation_method,
            })
        return True

    def is_opted_out(self, recipient_id: str, channel: str) -> bool:
        """Has this recipient opted out - on THIS channel or any other?

        OPT-OUT IS SCOPED TO THE PERSON, NOT THE CHANNEL, and it was not.

        A STOP arriving by SMS was recorded only against SMS, so the same
        number could still be sent a WhatsApp message or an autodialed
        marketing call. That is not what "STOP" means to the person who sent
        it, and it is not what regulators or the CTIA guidelines expect: a
        suppression request covers the contact, not the transport it happened
        to arrive on.

        It was also the most likely way for this gate to produce a genuinely
        angry customer - somebody who took the trouble to opt out, and then got
        phoned.

        Found by an external compliance review, 2026-08-29. Fixing it can only
        ever SUPPRESS more messages, never allow one, so it is safe in the
        direction that matters.
        """
        if any(r == recipient_id for r, _ch in self._opted_out):
            return True
        for key, record in self._records.items():
            if (
                record.recipient_id == recipient_id
                and record.channel == channel
                and record.status == ConsentStatus.OPTED_OUT
                and record.revoked_at is not None
            ):
                return True
        return False

    def mark_opted_out(self, recipient_id: str, channel: str) -> None:
        """Directly register an opt-out for enforcement (recipient, channel)."""
        if recipient_id and channel:
            self._opted_out.add((recipient_id, channel))

    def hydrate_opted_out(self, pairs) -> int:
        """Load durable opt-outs into the enforcement set on process start so a
        STOP recorded before a restart still blocks. `pairs` = iterable of
        (recipient_id, channel). Returns how many were loaded."""
        n = 0
        for recipient_id, channel in pairs:
            if recipient_id and channel:
                self._opted_out.add((recipient_id, channel))
                n += 1
        return n

    @staticmethod
    def _key(recipient_id: str, channel: str, use_case: str) -> str:
        return f"{recipient_id}|{channel}|{use_case}"


# Module-level singleton for use outside of tests (replaced by DI in production)
_store = ConsentStore()


def get_consent_store() -> ConsentStore:
    return _store
