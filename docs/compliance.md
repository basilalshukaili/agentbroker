# Compliance Module Documentation

**Phase P4 Artifact | ComplianceAgent**

---

## Overview

The `/compliance/` module is the only module authorized to approve outbound communications.
No channel adapter may dispatch a message, call, or form submission without first passing
through `compliance.pre_check.pre_check(...)`.

---

## Pre-check Gate

`compliance/pre_check.py` → `pre_check()` performs, in order:
1. **Content classification** — restricted categories (gambling, lending, cannabis, adult)
2. **Opt-out check** — recipient opted out of this channel
3. **Consent check** — marketing messages require opt-in per TCPA / GDPR / CASL
4. **10DLC campaign check** — US SMS requires registered campaign per use case
5. **Audit log write** — every decision (allow or deny) is written immutably

On failure: raises `ComplianceViolationError` → translated to `compliance_violation` API error.
On success: returns `None` and writes an `OUTBOUND_DISPATCHED` audit record.

---

## Jurisdiction Rules

`compliance/jurisdiction_rules.py` — rules applied per country + state:

| Jurisdiction | SMS marketing | Voice consent | Recording | GDPR | CASL |
|---|---|---|---|---|---|
| US (default) | PEWC required | Prior express | One-party | No | No |
| US-CA, FL, IL, MD, MA, MT, NV, NH, PA, WA | PEWC required | Prior express | **Two-party** | No | No |
| EU/UK | Opt-in required | Prior express | Two-party | **Yes** | No |
| Canada | Express consent | Prior express | One-party | No | **Yes** |

---

## Consent Store

`compliance/consent_store.py` — append-only consent records:
- Key: `recipient_id | channel | use_case`
- `has_valid_consent(recipient_id, channel, use_case)` → bool
- `is_opted_out(recipient_id, channel)` → bool (any use-case opt-out on this channel)
- `revoke_consent(...)` — marks opted-out; all future messages on this channel blocked

---

## 10DLC Campaign Registry

`compliance/campaign_registry.py` — scaffold for The Campaign Registry integration:
- `is_sms_authorized(use_case)` → bool
- Campaign status must be `REGISTERED` for SMS to be authorized
- Use cases: MARKETING, APPOINTMENT_REMINDER, ACCOUNT_NOTIFICATION, TWO_FACTOR_AUTHENTICATION, etc.

**v0.1 note:** The registry is seeded with test campaign records for dev/staging.
Production requires real TCR registration before live US SMS traffic.

---

## Voice Recording Consent

`compliance/recording_consent.py`:
- Checks if two-party consent required per jurisdiction
- `check_recording_consent_required(call_id, country, state)` — call before recording
- `confirm_recording_consent(call_id, confirmed)` — record outcome of consent prompt
- `is_recording_allowed(call_id)` — gate called before actually recording

---

## Audit Log

`compliance/audit_log.py` — immutable append-only log:
- Every `pre_check()` call writes either `OUTBOUND_DISPATCHED` or `COMPLIANCE_VIOLATION`
- Every consent change writes `CONSENT_RECORDED` or `CONSENT_REVOKED`
- Every recording consent prompt/confirmation writes appropriate event
- PII (recipient phone/email) stored as SHA-256 hash only — never in plaintext

---

## ComplianceAgent Sign-off Checklist (for P14)

- [ ] All US SMS campaigns registered in TCR (MARKETING, APPOINTMENT_REMINDER, ACCOUNT_NOTIFICATION, 2FA)
- [ ] Twilio Messaging Services configured with registered campaigns
- [ ] DNC list integration live
- [ ] Opt-out keyword handling live (STOP → immediate opt-out)
- [ ] Recording consent prompt wired into voice AI adapter
- [ ] GDPR data subject deletion workflow tested
- [ ] Audit log verified to contain 1:1 records with outbound events
- [ ] Content classifier validated against restricted-category test set
- [ ] Retention policies configured per jurisdiction
- [ ] Target jurisdictions confirmed: US (50 states), Canada (EN)
