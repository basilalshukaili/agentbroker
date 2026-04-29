"""
Immutable compliance audit log.
Every authorization decision and every outbound communication is recorded here.
Records are append-only — no update or delete operations exist.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class AuditEventType(str, Enum):
    AUTHORIZATION_ALLOW = "authorization_allow"
    AUTHORIZATION_DENY = "authorization_deny"
    OUTBOUND_DISPATCHED = "outbound_dispatched"
    CONSENT_RECORDED = "consent_recorded"
    CONSENT_REVOKED = "consent_revoked"
    COMPLIANCE_VIOLATION = "compliance_violation"
    RECORDING_CONSENT_PROMPTED = "recording_consent_prompted"
    RECORDING_CONSENT_CONFIRMED = "recording_consent_confirmed"
    RECORDING_CONSENT_DECLINED = "recording_consent_declined"
    PII_RETENTION_EXPIRY = "pii_retention_expiry"


class AuditRecord(BaseModel):
    audit_id: str
    event_type: AuditEventType
    timestamp: datetime
    agent_id: Optional[str] = None
    principal_kind: Optional[str] = None
    principal_id: Optional[str] = None
    operation: Optional[str] = None
    smb_id: Optional[str] = None
    recipient_id_hash: Optional[str] = None  # SHA-256 of recipient identifier
    channel: Optional[str] = None
    use_case: Optional[str] = None
    jurisdiction: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    token_hash: Optional[str] = None  # SHA-256 of JWT — never the JWT itself
    trace_id: Optional[str] = None
    metadata: dict[str, Any] = {}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuditLog:
    """
    In-memory implementation for tests.
    Production replaces with PostgreSQL-backed append-only table.
    """

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def record(
        self,
        event_type: AuditEventType,
        *,
        agent_id: str | None = None,
        principal_kind: str | None = None,
        principal_id: str | None = None,
        operation: str | None = None,
        smb_id: str | None = None,
        recipient_id: str | None = None,  # will be hashed
        channel: str | None = None,
        use_case: str | None = None,
        jurisdiction: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        token: str | None = None,       # will be hashed
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            audit_id=str(uuid.uuid4()),
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            agent_id=agent_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            operation=operation,
            smb_id=smb_id,
            recipient_id_hash=_hash(recipient_id) if recipient_id else None,
            channel=channel,
            use_case=use_case,
            jurisdiction=jurisdiction,
            decision=decision,
            reason=reason,
            token_hash=_hash(token) if token else None,
            trace_id=trace_id,
            metadata=metadata or {},
        )
        self._records.append(record)
        return record

    def query(
        self,
        agent_id: str | None = None,
        event_type: AuditEventType | None = None,
        since: datetime | None = None,
    ) -> list[AuditRecord]:
        results = self._records
        if agent_id:
            results = [r for r in results if r.agent_id == agent_id]
        if event_type:
            results = [r for r in results if r.event_type == event_type]
        if since:
            results = [r for r in results if r.timestamp >= since]
        return results

    def count(self) -> int:
        return len(self._records)


_audit_log = AuditLog()


def get_audit_log() -> AuditLog:
    return _audit_log
