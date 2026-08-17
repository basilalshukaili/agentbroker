> **⚠️ HISTORICAL — NOT CURRENT STATE (stamped 2026-08-17).**
> This file is a point-in-time snapshot kept for history. Claims about deploys,
> pricing, endpoints, and status here may be stale or contradicted by later work.
> The authoritative current picture is **AUDIT-2026-08-16.md** plus `git log`.

# SMB Transaction & Communication Broker — Release v0.1.0

**Release date:** 2026-04-27  
**Build status:** All CI gates passing  
**North-star metric:** WinRate (selection_rate × success_rate_when_selected)

---

## What is this?

The SMB Transaction & Communication Broker is an agent-callable service that enables autonomous AI agents to discover, verify, communicate with, schedule appointments with, and transact with long-tail small and mid-sized businesses — through a single, compliance-aware tool surface.

It is not a CRM. It is not a scheduling SaaS. It is not a generic HTTP caller.  
It is a purpose-built **agent-to-SMB execution layer** with compliance, reliability, and WinRate optimization built in from the ground up.

---

## v0.1.0 Scope

### 12 Operations (all live)
| Operation | Profile | Cost | Notes |
|-----------|---------|------|-------|
| `find_business` | sync_fast | $0.01 | Full in-memory directory, 20 seed SMBs |
| `verify_business` | sync_fast | $0.01 | Capability + channel validation |
| `send_message` | sync_fast | $0.05 | SMS/Email/Voice with pre_check gate |
| `capture_lead` | sync_fast | $0.02 | UUID5 dedup by (smb+prospect) |
| `schedule_appointment` | async_by_default | $0.15 | Cal.com fast path + voice fallback |
| `send_transactional_confirmation` | sync_fast | $0.04 | Template-rendered, CAN-SPAM compliant |
| `handle_inbound` | sync_fast | $0.03 | STOP opt-out, intent classification |
| `escalate_to_human` | async_by_default | $0.10 | Ticket creation + queue routing |
| `get_status` | sync | $0.001 | Async job polling |
| `get_outcome` | sync | $0.001 | Final result retrieval |
| `preview_cost` | sync | free | ±5% cost accuracy SLO |
| `self_test` | sync_fast | free | 6-check smoke test, ~<500ms |

### Supply
- **20 seed SMBs** across Atlanta GA and Boston/Cambridge MA
- 3 wedge verticals: personal_services, home_services, professional_services
- Onboarding flow: self_serve → phone verification → channel_capture

### Compliance
- **TCPA**: opt-out respected, marketing consent required for US SMS
- **GDPR**: opt-in required for EU marketing
- **CASL**: express/implied consent enforcement for Canada
- **CAN-SPAM**: physical address + unsubscribe footer on commercial email
- **10DLC**: `is_sms_authorized()` gate for US A2P SMS campaigns
- **Recording consent**: two-party prompt for CA, FL, IL, MD, MA, MT, NV, NH, PA, WA
- **Audit log**: SHA-256 hashed PII, append-only, immutable records

### Reliability
- Channel fallback chain: direct_api → voice_ai → sms → email → web_form → escalate
- Circuit breaker: CLOSED → OPEN → HALF_OPEN per channel
- Idempotency: 24h TTL, scoped per (agent_id, operation, idempotency_key)
- Async jobs: Celery + Redis with exponential backoff retry
- Webhook delivery: up to 10 retries over 24h with HMAC-SHA256 signing

### Agent Interface
- **Manifest server**: GET /manifest, GET /manifest/ops, A/B variant routing
- **Discovery card**: GET /.well-known/agent-service
- **Identity**: Agent-Identity JWT with scope (operations, budget_cap, verticals)
- **Self-test**: 6 smoke checks, runs end-to-end in <500ms

### Optimization Loop (P11)
- Failure classifier: maps exceptions → 8 failure classes
- Attribution engine: routes failures to responsible layer
- Outcome evaluator: scores OutcomeReceipt on correctness/latency/cost/channel/compliance
- Manifest A/B router: deterministic agent bucketing for manifest experiments
- Selection analytics: selection rate, win_rate, persona breakdown
- Weekly report generator: JSON + Markdown reports to `reports/`

---

## Architecture Decision Records

| ADR | Decision |
|-----|----------|
| ADR-0001 | Python 3.11 + FastAPI for async I/O and OpenAPI generation |
| ADR-0002 | Compliance as explicit function call, not middleware |
| ADR-0003 | Celery + Redis for async operations |
| ADR-0004 | Redis primary + PostgreSQL fallback for idempotency |

---

## Known Limitations (v0.1.0)

1. **Directory is in-memory** — SMBs reset on restart. Production: PostgreSQL + PostGIS.
2. **Twilio/SendGrid/Vapi run in stub mode** without real API keys — dispatches are simulated.
3. **Cal.com in stub mode** — synthetic availability slots returned; no real bookings.
4. **Identity signing is HS256 symmetric stub** — production must use PKI / auth service.
5. **No persistent audit log** — AuditLog is in-memory; production requires PostgreSQL persistence.
6. **10DLC campaign registration is stubbed** — `is_sms_authorized()` returns `True` by default.
7. **WinRate baseline not yet measured** against real agent traffic — simulation harness provides estimate.

---

## P14 Gates — All Passing

- [x] All 12 operations implemented with OutcomeReceipt response schema
- [x] Manifest covers all 12 ops with input_schema, output_schema, examples, failure_modes
- [x] Compliance pre_check gate enforced on all outbound dispatches
- [x] Recording consent gate live for two-party states
- [x] Audit log stores hashed PII (never plaintext)
- [x] Circuit breaker, retry policy, channel fallback all wired
- [x] Idempotency store with 24h TTL and agent-scoped keys
- [x] Agent-Identity JWT issuance + validation
- [x] Self-test passes all 6 checks
- [x] 20 seed SMBs seeded across 3 verticals and 2 cities
- [x] Agent simulation harness: 50-task corpus, 3 personas, WinRate computed
- [x] Optimization loop: failure classifier, attribution, outcome evaluator, weekly report
- [x] Dockerfile + docker-compose + GitHub Actions CI pipeline
- [x] FastAPI entry point with all 12 operation endpoints
- [x] requirements.txt pinned
- [x] ComplianceAgent sign-off (see docs/compliance.md §P14 checklist)

---

## Upgrade Path

v0.2 will focus on:
1. Connecting real Twilio / SendGrid / Vapi credentials
2. PostgreSQL-backed directory with PostGIS geo search
3. Real Cal.com API integration with live availability
4. 10DLC campaign registration automation
5. Expanding to 100+ seed SMBs in 5 additional cities
6. Production auth with short-lived JWTs from auth service
7. First measured WinRate from real agent traffic

---

*Built by 13 agent roles as specified in MASTER_BUILD_PROMPT_v3.md.*
*WinRate north-star: P(selected | in candidate set) × P(task succeeds | selected)*

---

## Addendum (2026-04-28) — Discovery and validation

After the initial v0.1.0 build, an additional pass added the discovery layer required for AI agents to find and select us, and replaced assumed metrics with measured ones.

### Multi-protocol discovery (NEW)

7 discovery surfaces now live, generated from one source of truth (`manifest.json`):

| Endpoint | Format |
|----------|--------|
| `POST /mcp` | Model Context Protocol JSON-RPC 2.0 (Claude Desktop, Cursor, Continue) |
| `GET /.well-known/mcp.json` | MCP descriptor |
| `GET /.well-known/ai-plugin.json` | OpenAI ChatGPT plugin |
| `GET /.well-known/openai-tools.json` | OpenAI function calling tools array |
| `GET /.well-known/anthropic-tools.json` | Claude tool_use array |
| `GET /.well-known/agents.json` | A2A (Agent-to-Agent) protocol |
| `GET /llms.txt`, `/llms-full.txt` | LLMs site map (https://llmstxt.org) |

### Pricing model (NEW)

`billing/pricing_tiers.py` defines 5 revenue streams: PAYG, Subscriptions (Free/Dev/Business/Enterprise), outcome-based premium, SMB-side listing tiers (Verified/Featured/Exclusive), analytics resale. `forecast_revenue()` computes:
- Year 1 conservative: **$437,400 ARR**
- Year 2 scaling: **$2,980,560 ARR**

### Measured metrics (replaces "TBD" from initial build)

| Metric | Method | Result |
|--------|--------|--------|
| Test suite | `pytest tests/ -q` | **81 / 81 passing in 0.40s** |
| Self-test | `agent_interface.self_test.run_self_test` | **6 / 6 in 318 ms** |
| WinRate (sim) | 504 trials × 3 personas × 56 tasks with adversarial cases | **0.818 aggregate** |
| MCP server | `tools/list`, `tools/call`, `resources/list`, `prompts/list` | **All operational** |

### Honest losses (also measured)

The simulation includes adversarial tasks where we should lose. We do:
- 17 / 504 lost to browser_automation (complex web — expected)
- 14 / 504 lost to calendar_saas (when SMB is on the platform — expected)
- 7 / 504 lost to voice_only (latency-tolerant booking — expected)

These are the expected losses, not bugs. Documented in `docs/BENCHMARKS.md`.

### Documentation (NEW)

- `README.md` — master entry point, quick start for all 4 integration patterns
- `docs/AGENT_INTEGRATION_GUIDE.md` — copy-paste integration for MCP / OpenAI / Anthropic / REST
- `docs/BENCHMARKS.md` — every measured number with reproduction commands
- `docs/PRICING.md` — full revenue model with year-1 / year-2 forecasts
- `docs/SECURITY.md` — production hardening checklist
- `docs/NEXT_STEPS.md` — ordered work list to go live, with gates

The product is now ready to be called by AI agents. See `docs/NEXT_STEPS.md` for the ~12-week path to production paying customers.
