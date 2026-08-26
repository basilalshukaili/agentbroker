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
    Compliance audit trail: in-memory for fast reads, DURABLY MIRRORED so it
    survives a restart.

    It used to be the list alone, with a docstring promising that "production
    replaces with PostgreSQL-backed append-only table" - which nothing ever
    did. Meanwhile the privacy policy (web/pages.py) tells users we keep this
    data "to prove compliance with TCPA, GDPR, CASL, PDPL, and equivalents on
    request from a regulator or recipient". A trail that dies on every deploy
    proves nothing, and the deploy happens far more often than the regulator
    asks (found 2026-08-26).

    Mirrors to the `compliance_audit` table, same shape as the durable opt-out
    fix: memory first so a slow database never delays a compliance DECISION,
    the durable write fire-and-forget behind it. Losing the mirror degrades
    evidence; blocking on it would degrade the gate itself, which is worse.

    Recipient ids and tokens are hashed BEFORE they reach either store, so the
    durable copy never holds a raw phone number, email or key.
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
        _mirror_durably(record)
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


# ---------------------------------------------------------------------------
# Durable mirror
# ---------------------------------------------------------------------------

_MIRROR_TABLE = "compliance_audit"


def _mirror_durably(record: "AuditRecord") -> None:
    """Fire-and-forget write of one audit record. NEVER raises, never blocks.

    `record()` is called from inside compliance/pre_check.py on the dispatch
    path, which is synchronous. Awaiting a database round-trip there would put
    Supabase latency in front of every send, so the write is scheduled on the
    running event loop when there is one and skipped when there is not (tests,
    scripts). A dropped mirror costs evidence; a blocked gate costs delivery.
    """
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                      # no loop (tests/CLI) - memory only
        loop.create_task(_write_row(record))
    except Exception:  # noqa: BLE001
        pass


async def _write_row(record: "AuditRecord") -> None:
    try:
        from storage.supabase_client import insert_row
        await asyncio.wait_for(insert_row(_MIRROR_TABLE, {
            "audit_id": record.audit_id,
            "event_type": getattr(record.event_type, "value", str(record.event_type)),
            "ts": record.timestamp.isoformat(),
            "agent_id": record.agent_id,
            "principal_kind": record.principal_kind,
            "principal_id": record.principal_id,
            "operation": record.operation,
            "smb_id": record.smb_id,
            # already hashed by record() - no raw identifier ever lands here
            "recipient_id_hash": record.recipient_id_hash,
            "channel": record.channel,
            "use_case": record.use_case,
            "jurisdiction": record.jurisdiction,
            "decision": record.decision,
            "reason": record.reason,
            "token_hash": record.token_hash,
            "trace_id": record.trace_id,
            "metadata": record.metadata or {},
        }), timeout=3.0)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("smb_broker.audit").warning(
            "audit_mirror_failed id=%s err=%s", record.audit_id, exc)


import asyncio  # noqa: E402  (used by _write_row's timeout)
