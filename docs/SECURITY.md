# Security & Production Hardening Checklist

This document is the gate between "v0.1 functional" and "v1.0 in front of paying customers."

Every item is either **DONE** (already implemented in this repo) or **TODO** (must be completed before production traffic).

---

## Identity & Authorization

- [x] **DONE** — Agent-Identity tokens are HMAC-SHA256 signed (`agent_interface/identity.py`).
- [x] **DONE** — Token claims include `scope.operations`, `scope.budget_cap_usd`, `scope.verticals`, expiry.
- [x] **DONE** — Per-token revocation list (JTI-based).
- [ ] **TODO** — Replace HS256 with RS256 / EdDSA using a managed KMS key (AWS KMS, GCP Cloud KMS).
- [ ] **TODO** — Move `AGENT_IDENTITY_SIGNING_SECRET` from env to managed secret store.
- [ ] **TODO** — Token TTL ≤ 1 hour in production (currently 24h dev default).
- [ ] **TODO** — Refresh-token flow + sliding-window expiry.
- [ ] **TODO** — Rate limit per agent_id at the edge (currently only at app level).

## Authentication of channel adapters (outbound credentials)

- [ ] **TODO** — Twilio, SendGrid, Vapi, Cal.com credentials must come from a secret manager, never env vars in the deployment manifest.
- [ ] **TODO** — Per-environment credential rotation playbook (90-day cadence).
- [ ] **TODO** — Audit log entry on every credential read.

## Webhook delivery

- [x] **DONE** — Webhooks are signed with HMAC-SHA256 (`reliability/webhook_delivery.sign_payload`).
- [x] **DONE** — Per-agent secret, returned only at webhook registration time.
- [ ] **TODO** — Add request timestamp to signature payload to prevent replay.
- [ ] **TODO** — Strip query parameters from `callback_url` and validate scheme is HTTPS only in production.

## Compliance gate

- [x] **DONE** — `compliance/pre_check` is the only path to outbound channels.
- [x] **DONE** — `ComplianceViolationError` is non-bypassable at the channel level.
- [x] **DONE** — Audit log records every gate decision (`compliance/audit_log`).
- [x] **DONE** — PII in audit log stored as SHA-256 hashes only (`recipient_id_hash`).
- [x] **DONE** — JWT tokens stored as SHA-256 hashes in audit log.
- [ ] **TODO** — Migrate audit log from in-memory to append-only PostgreSQL table with no UPDATE/DELETE permissions for app role.
- [ ] **TODO** — Quarterly external compliance audit (TCPA, GDPR DPIA, CASL).
- [ ] **TODO** — DPO designation for GDPR.

## Data protection

- [ ] **TODO** — All PII fields encrypted at rest (Postgres column-level encryption).
- [ ] **TODO** — TLS 1.3 minimum on all internal & external connections.
- [ ] **TODO** — Data residency: EU-resident PII stored only in EU region.
- [ ] **TODO** — Right-to-erasure endpoint for GDPR Article 17 requests.
- [ ] **TODO** — Right-to-portability endpoint for GDPR Article 20.
- [ ] **TODO** — 72-hour breach-notification runbook.

## Input validation

- [x] **DONE** — All requests validated by Pydantic models in `core/models.py`.
- [x] **DONE** — `ComplianceViolationError` raised for restricted-content categories.
- [x] **DONE** — Phone-number / email format validation on onboarding.
- [ ] **TODO** — Add request-size limit middleware (e.g., 1MB default).
- [ ] **TODO** — Strict `Content-Type` enforcement (only `application/json`).

## Idempotency & replay protection

- [x] **DONE** — Idempotency keys scoped per `(agent_id, operation, key)` with 24h TTL.
- [x] **DONE** — Different agents using the same key do not collide (`test_different_agent_same_key_does_not_collide`).
- [ ] **TODO** — Migrate idempotency store from in-memory to Redis with persistence.

## Rate limiting & abuse

- [ ] **TODO** — Per-agent rate limit (e.g., 10 requests/sec for Free, 100/sec for Business).
- [ ] **TODO** — Per-IP rate limit on auth endpoints.
- [ ] **TODO** — Anomaly detection on bursts of `compliance_violation` events from a single agent.
- [ ] **TODO** — Auto-suspend agents that breach abuse thresholds.

## Observability

- [x] **DONE** — Structured spans via `telemetry/tracer.py` with all §2.7 attributes.
- [x] **DONE** — PII redaction in logs (`telemetry/log_redactor.py`).
- [x] **DONE** — Counters per operation in `telemetry/metrics_emitter.py`.
- [ ] **TODO** — OpenTelemetry exporter wired to a hosted collector (Datadog, Honeycomb, Tempo).
- [ ] **TODO** — Production alerting on: compliance gate failures, circuit breaker open events, p99 latency > SLO, 5xx rate > 0.1%.

## Reliability

- [x] **DONE** — Circuit breakers per channel adapter (`reliability/circuit_breaker.py`).
- [x] **DONE** — Exponential backoff retry policy (`reliability/retry_policy.py`).
- [x] **DONE** — Channel fallback chain (`reliability/channel_fallback.py`).
- [x] **DONE** — Webhook retry with backoff up to 24h (`reliability/webhook_delivery`).
- [ ] **TODO** — Multi-region active-active deployment with automated failover.
- [ ] **TODO** — Chaos engineering tests (kill a worker, kill Redis, kill PostgreSQL primary).
- [ ] **TODO** — Defined RTO < 30 min, RPO < 5 min for production.

## Supply chain

- [ ] **TODO** — Pin all dependencies via lockfile (currently `requirements.txt` uses `==` for top-level only).
- [ ] **TODO** — Run `pip-audit` and `safety check` in CI on every commit.
- [ ] **TODO** — SBOM (CycloneDX) generated and signed for every release.
- [ ] **TODO** — Container image signed with Cosign.

## Build & deploy

- [x] **DONE** — Multi-stage Dockerfile with non-root user.
- [x] **DONE** — Docker compose for local dev (api, worker, postgres, redis).
- [ ] **TODO** — CI runs full test suite + simulation harness on every PR.
- [ ] **TODO** — Production deploy gated on: tests green AND simulation WinRate ≥ 0.75 AND self_test 100%.
- [ ] **TODO** — Blue-green deployment with automated rollback on health-check failure.

## Audit & compliance certifications

- [ ] **TODO** — SOC 2 Type 1 (target: 6 months from launch)
- [ ] **TODO** — SOC 2 Type 2 (target: 12 months from launch)
- [ ] **TODO** — ISO 27001 (target: 18 months from launch)
- [ ] **TODO** — HIPAA BAA capability (gated on having a healthcare vertical opt-in)

## Incident response

- [ ] **TODO** — On-call rotation with PagerDuty / Opsgenie integration.
- [ ] **TODO** — Public status page (UptimeRobot free tier, or `status.hatchloop.dev` once custom domain is set).
- [ ] **TODO** — Post-incident review template + 5-business-day commitment.
- [ ] **TODO** — Notify-affected-agents pipeline gated on compliance officer approval.

---

## Production readiness gate

Do **not** deploy to production with paying customers until:

1. All **TODO**s in **Identity & Authorization**, **Authentication of channel adapters**, **Compliance gate**, and **Data protection** are done.
2. SOC 2 Type 1 audit is in flight (auditor engaged).
3. Public TCPA / GDPR / CASL compliance attestation is signed by counsel.
4. Status page + on-call rotation are live.

Until those are done: pilot only, with agreements explicitly stating dev-grade SLA.
