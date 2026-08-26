"""
The compliance audit trail must survive a restart.

Our privacy policy tells users we keep this data "to prove compliance with
TCPA, GDPR, CASL, PDPL, and equivalents on request from a regulator or
recipient." The implementation was a Python list with a docstring promising
that production would replace it - which nothing ever did (found 2026-08-26).
A trail that dies on every deploy proves nothing, and we deploy far more often
than a regulator asks.

Same failure shape as the opt-out leak fixed in August: enforcement state held
only in memory, looking correct right up until the process restarts.
"""
from __future__ import annotations

import asyncio

import pytest

from compliance.audit_log import AuditLog, AuditEventType


def test_record_still_returns_and_stores_in_memory():
    log = AuditLog()
    rec = log.record(AuditEventType.AUTHORIZATION_ALLOW, agent_id="a1",
                     decision="allow", channel="sms")
    assert rec.audit_id
    assert log.count() == 1


def test_identifiers_are_hashed_before_storage():
    """The durable copy must never hold a raw phone number or key."""
    log = AuditLog()
    rec = log.record(AuditEventType.AUTHORIZATION_ALLOW,
                     recipient_id="+96894639405", token="sk-secret-value")
    assert rec.recipient_id_hash and rec.recipient_id_hash != "+96894639405"
    assert rec.token_hash and rec.token_hash != "sk-secret-value"
    # and the raw values appear nowhere on the record
    blob = repr(rec)
    assert "+96894639405" not in blob
    assert "sk-secret-value" not in blob


def test_mirror_is_attempted_when_an_event_loop_exists(monkeypatch):
    """The whole point: recording must reach durable storage."""
    written = []

    async def _capture(table, row):
        written.append((table, row))
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _capture)

    async def go():
        log = AuditLog()
        log.record(AuditEventType.COMPLIANCE_VIOLATION, agent_id="a1",
                   decision="block", reason="quiet_hours",
                   recipient_id="+15551234567")
        # the mirror is scheduled, not awaited - let the loop run it
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(go())

    assert written, "audit record must be mirrored durably"
    table, row = written[0]
    assert table == "compliance_audit"
    assert row["decision"] == "block"
    assert row["reason"] == "quiet_hours"
    assert row["recipient_id_hash"] != "+15551234567"
    assert "+15551234567" not in str(row), "raw recipient must never be persisted"


def test_recording_never_raises_when_storage_fails(monkeypatch):
    """Evidence loss is bad; a broken compliance gate is worse."""
    async def _boom(*a, **k):
        raise RuntimeError("supabase down")
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", _boom)

    async def go():
        log = AuditLog()
        rec = log.record(AuditEventType.AUTHORIZATION_ALLOW, agent_id="a1",
                         decision="allow")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return rec, log.count()
    rec, n = asyncio.run(go())
    assert rec.audit_id and n == 1, "the decision itself must still be recorded"


def test_recording_works_without_an_event_loop():
    """Called from sync code (tests, CLI) it must degrade, not explode."""
    log = AuditLog()
    rec = log.record(AuditEventType.AUTHORIZATION_ALLOW, agent_id="a1")
    assert rec.audit_id
    assert log.count() == 1


def test_the_stale_docstring_promise_is_gone():
    """It claimed production replaces this with Postgres. Nothing did, and the
    promise is what stopped anyone noticing."""
    import compliance.audit_log as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "Production replaces with PostgreSQL-backed append-only table" not in src


def test_privacy_policy_no_longer_claims_signed_receipts():
    """Receipt signing (billing/receipt_signer.py) is imported by nothing in
    production, so the privacy policy must not say we produce signed receipts."""
    src = open("web/pages.py", encoding="utf-8").read()
    assert "signed receipts" not in src
