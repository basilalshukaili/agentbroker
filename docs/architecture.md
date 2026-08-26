# Architecture — SMB Transaction & Communication Broker

**Last updated: 2026-05-05**

---

## 0. Deployment Topology

```
AI agent
   │
   ▼
agent-broker-edge.basil-agent.workers.dev   ← Cloudflare Worker (300+ PoPs)
   │
   ├── GET  /.well-known/*                  → embedded snapshot  (40–70 ms)
   ├── GET  /manifest*, /llms.txt, /openapi.yaml, /supply/platforms, /compliance/jurisdictions
   │                                        → embedded snapshot  (40–70 ms)
   ├── POST /mcp  initialize / tools/list / ping / prompts/* / resources/*
   │                                        → embedded snapshot  (40–65 ms)
   ├── POST /mcp  tools/call               → proxy to origin     (170–190 ms)
   └── POST /ops/*, everything else        → proxy to origin
            │
            ▼
   smb-broker.onrender.com                 ← Python FastAPI on Render free tier
            │
            ├── 13 operation handlers (core/)
            ├── Compliance gate (compliance/pre_check)
            ├── Channel adapters (channels/)
            ├── Billing + outcome store
            └── All .well-known / MCP endpoints (also served from origin, but
                agents should never hit origin directly)

Cron: */2 * * * *  — edge pings origin /health every 2 min (prevents 15 min idle sleep)
```

**The public URL is the edge worker.** The Render origin URL (`smb-broker.onrender.com`)
is an implementation detail; agents should never be given it directly.

---

## 1. Module Map

```
service-root/
├── agent_interface/        # What agents see — manifest, discovery, identity
│   ├── manifest_server.py  # Serves manifest.json, openapi.yaml, mcp_tools.json
│   ├── discovery.py        # /.well-known/* endpoints
│   ├── webhooks.py         # Registration + signed delivery controller
│   ├── identity.py         # Agent-Identity JWT verification + scope enforcement
│   └── self_test.py        # Live capability probe
├── core/                   # Business logic for all 20 operations
│   ├── find_business.py
│   ├── verify_business.py
│   ├── send_message.py
│   ├── capture_lead.py
│   ├── schedule_appointment.py
│   ├── send_transactional_confirmation.py
│   ├── handle_inbound.py
│   ├── escalate_to_human.py
│   ├── status_outcome.py   # get_status + get_outcome
│   ├── preview_cost.py
│   └── models.py           # Shared Pydantic models (OutcomeReceipt, etc.)
├── channels/               # Pluggable channel adapters
│   ├── adapter_interface.py
│   ├── direct_api/
│   │   ├── calcom.py
│   │   ├── square_appointments.py
│   │   ├── booksy.py
│   │   ├── calendly.py
│   │   ├── servicetitan.py
│   │   └── mindbody.py
│   ├── voice_ai/
│   │   ├── vapi.py
│   │   └── bland.py
│   ├── sms_email/
│   │   ├── twilio_sms.py
│   │   └── sendgrid_email.py
│   ├── web_form/
│   │   └── form_submitter.py
│   └── browser_automation/
│       └── playwright_adapter.py
├── supply/                 # SMB network — directory, verification, enrichment
│   ├── smb_directory.py
│   ├── verification.py
│   └── enrichment.py
├── compliance/             # ComplianceAgent module
│   ├── pre_check.py        # Main gate called by every channel adapter
│   ├── consent_store.py
│   ├── content_classifier.py
│   ├── jurisdiction_rules.py
│   ├── recording_consent.py
│   ├── campaign_registry.py
│   ├── retention_policy.py
│   └── audit_log.py
├── storage/                # Persistence layer
│   ├── idempotency_store.py
│   ├── operation_log.py
│   └── outcome_store.py
├── reliability/            # Retry, circuit breaker, fallback, async runner
│   ├── retry_policy.py
│   ├── circuit_breaker.py
│   ├── timeout_manager.py
│   ├── channel_fallback.py
│   ├── async_runner.py
│   ├── webhook_delivery.py
│   └── error_normalizer.py
├── billing/                # Usage metering + pricing
│   ├── meter.py
│   ├── pricer.py
│   ├── budget_guard.py
│   └── receipt_signer.py
├── telemetry/              # Structured logging + tracing
│   ├── tracer.py
│   ├── decision_log.py
│   ├── metrics_emitter.py
│   └── log_redactor.py
├── feedback/               # Failure attribution + outcome evaluation
│   ├── failure_classifier.py
│   ├── attribution_engine.py
│   └── outcome_evaluator.py
├── optimizer/              # Selection + outcome optimization
│   ├── manifest_variants/
│   ├── ab_router.py
│   └── selection_analytics.py
├── onboarding/             # Supply-side SMB onboarding
│   ├── self_serve.py
│   ├── verification_flow.py
│   └── channel_capture.py
├── tests/
├── deploy/
├── .ci/
├── main.py                 # FastAPI application entry point
├── config.py               # Configuration + env vars
├── requirements.txt
└── docker-compose.yml
```

---

## 2. Dependency Graph

```
main.py (FastAPI app)
  └── agent_interface/
        ├── identity.py ─────────────────────────┐
        ├── manifest_server.py ──→ manifest/      │
        ├── discovery.py                          │
        ├── webhooks.py ──→ reliability/          │
        └── self_test.py ──→ core/                │
                                                  │
  └── core/* ────────────────────────────────────┤
        ├── channels/adapter_interface.py ────────┤
        │     ├── direct_api/*                    │
        │     ├── voice_ai/*                      │
        │     ├── sms_email/*                     │
        │     ├── web_form/*                      │
        │     └── browser_automation/*            │
        ├── supply/smb_directory.py               │
        ├── compliance/pre_check.py ──────────────┤  (all outbound must pass)
        ├── reliability/* ─────────────────────── ┤
        ├── billing/meter.py                      │
        ├── billing/budget_guard.py               │
        ├── storage/*                             │
        └── telemetry/*                           │
                                                  │
  └── agent_interface/identity.py ────────────────┘
        └── verifies JWT, injects principal + scope

External dependencies:
  PostgreSQL ──← storage/, compliance/, supply/, billing/
  Redis ──────← storage/idempotency_store, reliability/async_runner
  Celery ─────← reliability/async_runner (worker)
  Twilio ─────← channels/sms_email/twilio_sms
  SendGrid ───← channels/sms_email/sendgrid_email
  Vapi/Bland ─← channels/voice_ai/*
  Cal.com ────← channels/direct_api/calcom
  Square ─────← channels/direct_api/square_appointments
  Playwright ─← channels/browser_automation/playwright_adapter
```

---

## 3. Data Flow — Representative Operation: `schedule_appointment`

```
Agent (LLM tool call)
  │
  ▼
POST /v1/schedule_appointment
  │
  ├─[1] identity.py: verify Agent-Identity JWT, extract principal + scope
  │      └─ reject → 401 policy_blocked:out_of_scope
  │
  ├─[2] billing/budget_guard.py: check Budget-Cap header
  │      └─ reject → 402 budget_exceeded
  │
  ├─[3] storage/idempotency_store.py: check Idempotency-Key
  │      └─ duplicate → return cached OutcomeReceipt (200)
  │
  ├─[4] core/schedule_appointment.py: validate request schema
  │      └─ bad schema → 400 bad_input
  │
  ├─[5] supply/smb_directory.py: resolve target SMB, get channel capabilities
  │      └─ not found → 404 supply_unreachable
  │
  ├─[6] compliance/pre_check.py: check consent, jurisdiction, content
  │      └─ fail → 422 compliance_violation (structured)
  │
  ├─[7] reliability/channel_fallback.py: determine channel chain
  │      e.g., direct_api:calcom → voice_ai:vapi → escalate_to_human
  │
  ├─[8] channels/direct_api/calcom.py (or fallback): execute booking
  │      └─ upstream failure → try next channel in chain
  │
  ├─[9] billing/meter.py: record usage + compute cost
  │
  ├─[10] storage/operation_log.py + outcome_store.py: persist
  │
  ├─[11] telemetry/tracer.py: emit all required spans
  │
  └─[12] Return OutcomeReceipt
           status: "pending_async" (async-by-default operation)
           operation_id: <uuid>
           estimated_completion_time: <p95 estimate>

  [async path]
  └─ reliability/async_runner.py (Celery worker)
       └─ polls/executes booking
       └─ reliability/webhook_delivery.py → signed POST to Webhook-URL
```

---

## 4. Channel-Fallback Chains (per operation)

| Operation | Primary | Secondary | Tertiary | Last resort |
|---|---|---|---|---|
| schedule_appointment | direct_api (vertical SaaS) | voice_ai (Vapi/Bland) | web_form | escalate_to_human |
| send_message | sms (Twilio) | email (SendGrid) | voice_ai | web_form |
| verify_business | direct_api | web scrape | phone call (voice_ai) | — |
| capture_lead | direct_api (CRM hook) | email | sms | web_form |
| send_transactional_confirmation | sms | email | — | — |
| handle_inbound | direct_api (webhook) | email polling | — | escalate_to_human |
| escalate_to_human | direct_api (ticketing) | email | sms | — |

Channel fallback chain is:
1. declared in manifest per operation
2. executed at runtime by `reliability/channel_fallback.py`
3. logged in `channel_fallback_chain[]` field of OutcomeReceipt

---

## 5. Sync/Async Execution Profiles

| Profile | Behavior | Operations |
|---|---|---|
| **sync** | Returns terminal status within ~2s | preview_cost, self_test, find_business, verify_business, get_status, get_outcome |
| **sync_fast** | Terminal within ~5s; may return pending_async if upstream slow | send_message (text), send_transactional_confirmation, capture_lead |
| **async_by_default** | Always returns pending_async; outcome via webhook + get_status | schedule_appointment, handle_inbound, escalate_to_human, send_message (voice) |

Async job lifecycle:
```
pending_async → [Celery worker picks up] → executing → success | failure | partial
                                        ↓
                              webhook_delivery sends signed OutcomeReceipt
                              outcome_store persists terminal state
                              get_status becomes queryable at any point
```

---

## 6. Non-Goals (v0.1)

- No consumer-facing UI or admin dashboard
- No payment processing (we route to payment rails only)
- No healthcare vertical (HIPAA deferred)
- No restaurant, travel, retail verticals
- No CRM functionality
- No mobile SDKs
- No real-time voice streaming / full call handling
- No multi-tenant SaaS billing console

---

## 7. Technology Choices

| Concern | Choice | Rationale |
|---|---|---|
| Web framework | FastAPI (Python 3.11+) | Async-native, Pydantic-native schema validation, OpenAPI generation |
| Data validation | Pydantic v2 | Fast, strict, integrates with FastAPI |
| Primary DB | PostgreSQL 15 | Reliable ACID, JSON columns for flexible payloads, widely supported |
| Cache + idempotency | Redis 7 | Sub-ms idempotency checks, pub/sub for async status updates |
| Async task runner | Celery + Redis broker | Mature, supports retries/backoff/ETA, separates web from worker |
| Voice AI | Vapi (primary), Bland (fallback) | Vapi has richest scheduling hooks; Bland as hot standby |
| SMS | Twilio | 10DLC campaign registration support, global reach |
| Email | SendGrid | Deliverability tooling, unsubscribe management (CAN-SPAM) |
| Calendar direct | Cal.com API | Open-source, SMB-friendly, REST API, self-hostable |
| Payments rail | Stripe (future) | Not in v0.1 scope; stub interface only |
| Browser automation | Playwright | Cross-browser, Python-native, async |
| Containerization | Docker + docker-compose | Standard, CI-friendly |
| CI | GitHub Actions | Free for open builds, parallel job support |
| Observability | OpenTelemetry + structlog | Vendor-agnostic traces, structured JSON logs |

---

*P1 gate passed. Architecture reviewed. Module skeleton scaffolded. Advancing to P2.*
*Handoff to: ManifestAgent*
