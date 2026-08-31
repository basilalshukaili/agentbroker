> WARNING: HISTORICAL ARCHIVE (2026-04-27) — compliance sign-off for v0.1.0 development/staging release. Current version is v0.2.11. Do not update.

# ComplianceAgent P14 Sign-Off — SMB Broker v0.1.0

**Date:** 2026-04-27  
**Signed off by:** ComplianceAgent (role §2.3 of MASTER_BUILD_PROMPT_v3.md)  
**Release:** v0.1.0

---

## §1. Pre-Check Gate — VERIFIED

Every outbound channel adapter calls `compliance/pre_check.py` before dispatching.
The gate is an explicit function call — not middleware, not optional, not bypassable.

| Channel | pre_check called | ComplianceViolationError propagated |
|---------|-----------------|-------------------------------------|
| TwilioSMSAdapter | ✅ | ✅ (never silently dropped) |
| SendGridEmailAdapter | ✅ | ✅ |
| VapiVoiceAdapter | ✅ | ✅ |

`send_message.py` explicitly does NOT fall back to another channel on compliance failure.
This is correct per §9.16: "compliance_violation" is returned to the agent as a structured error.

---

## §2. Opt-Out Enforcement — VERIFIED

- `ConsentStore.revoke_consent()` marks `(recipient, channel, use_case)` as OPTED_OUT.
- `ConsentStore.is_opted_out()` is checked in `pre_check.py` before content classification.
- STOP keyword inbound handler: `handle_inbound.py` detects "STOP" → calls `revoke_consent()` immediately.
- Test coverage: `TestOptOutRespected`, `test_stop_keyword_triggers_opt_out`.

---

## §3. Recording Consent — VERIFIED

Two-party recording consent required for: CA, FL, IL, MD, MA, MT, NV, NH, PA, WA.

- `check_recording_consent_required()` detects jurisdiction from `jurisdiction_rules.py`.
- `CONSENT_PROMPT_TEXT` prepended to all voice calls in two-party states.
- `is_recording_allowed()` gate enforced before recording begins.
- One-party states: recording allowed without prompt.
- Test coverage: `TestRecordingConsent` (5 tests including CA, TX, GA cases).

---

## §4. Content Classification — VERIFIED

Restricted categories blocked:
- Gambling, casino, lottery
- Cannabis, dispensary
- Adult/explicit content
- High-risk lending (APR >36%, payday)
- Spam signals

Gate: `content_classifier.py` → raises `ComplianceViolationError(rule="restricted_content")`.
Test coverage: `TestRestrictedContentFiltering` (3 tests).

---

## §5. Jurisdiction Rules — VERIFIED

| Jurisdiction | Marketing consent | Transactional | Recording |
|-------------|------------------|---------------|-----------|
| US (general) | TCPA opt-in required | No consent | One-party |
| US (two-party states) | TCPA opt-in required | No consent | Two-party prompt |
| EU | GDPR express opt-in | No consent | N/A |
| GB | GDPR express opt-in | No consent | N/A |
| CA | CASL express/implied | No consent | N/A |

Test coverage: `TestJurisdictionRulesApplied`.

---

## §6. 10DLC Campaign Gate — VERIFIED (stub)

`CampaignRegistry.is_sms_authorized(use_case)` checked for US SMS marketing/notifications.
Stub returns `True` by default for v0.1.0 — production must register brand + campaign with TCR.
Note: In production, transactional messages require Campaign Service Provider vetting.

---

## §7. Audit Log — VERIFIED

- All outbound dispatches: `AuditEventType.OUTBOUND_DISPATCHED` logged.
- All compliance violations: `AuditEventType.COMPLIANCE_VIOLATION` logged.
- PII storage: SHA-256(`recipient_id`) stored — never plaintext phone/email.
- JWT tokens: SHA-256(`token`) stored — never raw credential.
- Test coverage: `TestAuditLogIntegrity` (3 tests including PII hash verification).

---

## §8. GDPR / Data Retention — VERIFIED

- `retention_policy.py`: `gdpr_applies()`, `casl_applies()`, `retention_expiry_for()`.
- PII in audit log is hashed at write time — immutable anonymization.
- Data retention configuration: `AUDIT_LOG_RETENTION_DAYS` (default 365).

---

## §9. Known Compliance Gaps (v0.1.0 — acceptable for development)

1. **10DLC actual registration**: stubbed. Must be real in production before US SMS launch.
2. **Audit log persistence**: in-memory. Production requires PostgreSQL with WORM storage.
3. **GDPR right-to-erasure**: not implemented. Requires PII lookup + deletion workflow in v0.2.
4. **CASL 2-year renewal**: consent expiry tracking not yet automated.
5. **CAN-SPAM physical address**: footer added by SendGrid adapter — physical address is a placeholder.

---

## §10. Compliance Sign-Off Statement

> All outbound dispatch paths in SMB Broker v0.1.0 pass through the compliance pre_check gate.
> No channel adapter can dispatch without consent validation, content classification, and opt-out check.
> ComplianceViolationError is never silently dropped — it propagates to the calling agent as a structured error.
> All PII in the audit log is stored as SHA-256 hashes, never plaintext.
> The system is cleared for development and staging environments.
> Production launch requires 10DLC registration, PostgreSQL audit log persistence, and real API credentials.

**Status: CLEARED for v0.1.0 development/staging release.**
