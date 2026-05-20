# SMB Transaction & Communication Broker

> An agent-callable service that lets autonomous AI agents discover, verify, communicate with, schedule with, and transact with the long tail of small and mid-sized businesses (SMBs) — through a single compliance-aware tool surface.

[![Tests](https://img.shields.io/badge/tests-103%2F103%20passing-brightgreen)](./tests)
[![License](https://img.shields.io/badge/license-proprietary-lightgrey)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Edge](https://img.shields.io/badge/edge-cloudflare%20workers-orange)](./edge)

---

## Why this exists

There are ~60 million long-tail small businesses in the US — barbers, plumbers, accountants, home cleaners — and they have **no API surface**. AI agents that need to schedule a haircut, get a quote on a roof repair, or send a transactional confirmation today have to either: (a) drive a browser, (b) cold-call by voice, or (c) give up.

This service is the missing layer. Agents call us; we route to the right SMB through whichever channel reaches them fastest — Cal.com → SMS → voice AI → email → web form fallback — with full TCPA / GDPR / CASL / 10DLC / two-party recording-consent compliance enforced as a non-bypassable gate.

## What you can do with it

13 operations, all callable via REST, MCP, OpenAI tools, Anthropic tools, or A2A protocol:

| Operation | What it does | Cost | Latency |
|-----------|--------------|------|---------|
| `find_business` | Search SMBs by vertical + location + capability | **free** (read) | <2s |
| `verify_business` | Confirm an SMB has the capability you need | **free** (read) | <2s |
| `send_message` | SMS / email / voice with full compliance pre-check | $0.02 base + $0.20 voice | <5s |
| `capture_lead` | Structured intake when a consumer asks an SMB to follow up | $0.05 | <2s |
| `schedule_appointment` | Book / reschedule / cancel — direct API → voice fallback | $0.15 attempt + $0.35 on confirmed booking | <5s sync, async otherwise |
| `send_transactional_confirmation` | TCPA-exempt confirmations (booking, receipt, OTP) | $0.02 | <5s |
| `handle_inbound` | Classify customer messages (booking / cancel / opt-out / question) | $0.03 | <5s |
| `escalate_to_human` | Hand off to a human when an agent is stuck | $0.20 | async |
| `get_status` | Poll status of an async operation | **free** (read) | <1s |
| `get_outcome` | Retrieve final outcome of an async operation | **free** (read) | <1s |
| `preview_cost` | Estimate cost / latency / success probability — **free** | $0.00 | <500ms |
| `self_test` | Service health check — **free** | $0.00 | <2s |
| `import_booking_url` | Parse any Cal.com / Calendly / Doctolib / Booksy / OpenTable / 7 more URLs into a bookable SMB | $0.005 | <2s |

## Quick start (for AI agents)

### Option 1: MCP (Claude Desktop, Cursor, Continue, etc.)

```json
// Add to your MCP client config
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://agent-broker-edge.basil-agent.workers.dev/mcp",
      "headers": { "X-Agent-Identity": "$AGENT_BROKER_TOKEN" }
    }
  }
}
```

### Option 2: OpenAI function calling

```python
import httpx, openai
tools = httpx.get("https://agent-broker-edge.basil-agent.workers.dev/.well-known/openai-tools.json").json()["tools"]
client = openai.OpenAI()
resp = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role":"user","content":"Book me a haircut in Atlanta for Saturday under $50"}],
    tools=tools,
)
```

### Option 3: Anthropic tool use

```python
import httpx, anthropic
tools = httpx.get("https://agent-broker-edge.basil-agent.workers.dev/.well-known/anthropic-tools.json").json()["tools"]
client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    tools=tools,
    messages=[{"role":"user","content":"Book me a haircut in Atlanta for Saturday under $50"}],
)
```

### Option 4: Plain REST

```bash
curl -X POST https://agent-broker-edge.basil-agent.workers.dev/ops/find_business \
  -H "X-Agent-Identity: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "vertical": "personal_services",
    "location": {"zip_or_city": "30309"},
    "capability": "haircut"
  }'
```

## Discovery surfaces

We're discoverable through every protocol agents currently use:

| Protocol | URL |
|----------|-----|
| MCP | `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| MCP descriptor | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/mcp.json` |
| OpenAI ChatGPT plugin | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/ai-plugin.json` |
| OpenAI function tools | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/openai-tools.json` |
| Anthropic tool_use | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/anthropic-tools.json` |
| A2A (Agent-to-Agent) | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/agents.json` |
| llms.txt | `https://agent-broker-edge.basil-agent.workers.dev/llms.txt` |
| OpenAPI 3.1 | `https://agent-broker-edge.basil-agent.workers.dev/openapi.yaml` |
| Capability manifest | `https://agent-broker-edge.basil-agent.workers.dev/manifest` |
| Service discovery card | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/agent-service` |

## Why agents pick us (measured, not assumed)

From our agent-simulation harness — **168 trials × 3 personas (cost / quality / latency) across 56 tasks**, with noisy agent perception (±15% on price, ±10% on quality, ±20% on latency):

| Persona | Selection rate | Success when selected | **WinRate** |
|---|---|---|---|
| cost_minimizer | 94.6% | 88.7% | **0.839** |
| quality_maximizer | 91.1% | 88.2% | **0.804** |
| latency_sensitive | 91.7% | 88.3% | **0.810** |
| **Aggregate** | **92.5%** | **88.4%** | **0.818** |

We deliberately included tasks where we *should lose* — out-of-region SMBs (Tokyo / Mumbai / Berlin), complex web automation, trivial lookups — and the simulation correctly routes those to competitors. See [BENCHMARKS.md](./docs/BENCHMARKS.md).

## Compliance posture

Every outbound communication passes through `compliance/pre_check()`:

1. Content classification (gambling / lending / cannabis / adult / spam) — blocks restricted categories.
2. Opt-out check — TCPA STOP keyword, GDPR right-to-be-forgotten, CASL.
3. Consent check for marketing — TCPA written consent, GDPR opt-in, CASL implied/express.
4. 10DLC campaign-registry check for US SMS.
5. Two-party recording consent for CA / FL / IL / MD / MA / MT / NV / NH / PA / WA.
6. Audit log entry (PII stored as SHA-256 hash, never plaintext).

Compliance violations surface as `ComplianceViolationError` → `compliance_violation` API error. **Never silently dropped, never bypassed by middleware.**

## Architecture

```
AI agent → Cloudflare Worker edge (agent-broker-edge.basil-agent.workers.dev)
               ├── Discovery + MCP read → embedded snapshots  40–70 ms
               └── tools/call + /ops/*  → proxy to origin    170–190 ms
                              ↓
           Python FastAPI on Render (smb-broker.onrender.com)
               Cron */2 keeps Render warm — cold starts eliminated
```

The Python service exposes 13 operations over REST + MCP + .well-known surfaces. Each handler validates input with Pydantic models, runs through `compliance/pre_check`, executes via channel-fallback (`direct_api → voice_ai → sms → email → web_form`), and writes an immutable `OutcomeReceipt` to the outcome store. Async operations return `pending_async`. Idempotency is keyed by `(agent_id, operation, idempotency_key)` with 24h TTL.

Full architecture: [docs/architecture.md](./docs/architecture.md) · Edge layer: [edge/README.md](./edge/README.md)

## Repo layout

```
service-root/
├── core/                  # 12 operation handlers + shared Pydantic models
├── channels/              # Twilio, SendGrid, Vapi, Bland, Cal.com, Playwright
├── compliance/            # pre_check, jurisdiction_rules, consent_store, audit_log
├── reliability/           # retry, circuit_breaker, channel_fallback, async_runner
├── billing/               # meter, budget_guard, receipt_signer, pricing_tiers
├── telemetry/             # tracer, log_redactor, metrics_emitter
├── storage/               # outcome_store, idempotency_store
├── supply/                # smb_directory (20+ seed SMBs)
├── onboarding/            # self_serve, verification_flow, channel_capture
├── feedback/              # failure_classifier, attribution_engine, outcome_evaluator
├── optimizer/             # ab_router, selection_analytics, weekly_report
├── agent_interface/       # manifest_server, mcp_server, well_known, identity, webhooks, self_test
├── manifest/              # manifest.json, mcp_tools.json, openapi.yaml
├── api/                   # errors.md, identity.md, async.md
├── docs/                  # mission, architecture, compliance, ADRs
├── deploy/                # Dockerfile, docker-compose.yml, .ci/
├── tests/                 # unit, contract, compliance, fault_injection, agent_sim
├── reports/               # agent_sim_report.json, weekly winrate reports
├── main.py                # FastAPI entry point
├── config.py              # Centralized config from env
└── requirements.txt
```

## Local development

```bash
# 1. Clone & install
pip install -r requirements.txt

# 2. Run the test suite
python -m pytest tests/ -q

# 3. Run the agent simulation
python -m tests.agent_sim.harness

# 4. Run the self-test
python -c "import asyncio; from agent_interface.self_test import run_self_test; print(asyncio.run(run_self_test()).all_passed)"

# 5. Start the API
python main.py
# → http://localhost:8000/docs  (Swagger)
# → http://localhost:8000/manifest
# → http://localhost:8000/mcp
```

Or with Docker:

```bash
docker compose -f deploy/docker-compose.yml up
```

## Documentation index

- [Mission](./docs/mission.md) — north-star metric, scope, who we are NOT
- [Architecture](./docs/architecture.md) — module map, data flow, fallback chains
- [Compliance](./docs/compliance.md) — full jurisdiction matrix, pre-check sequence
- [API errors](./api/errors.md) — 16 error codes with retry semantics
- [API identity](./api/identity.md) — Agent-Identity JWT spec
- [API async](./api/async.md) — execution profiles, polling rules, webhook contract
- [Agent integration guide](./docs/AGENT_INTEGRATION_GUIDE.md) — copy-paste examples for every protocol
- [Benchmarks](./docs/BENCHMARKS.md) — measured WinRate, latency, cost vs alternatives
- [Pricing](./docs/PRICING.md) — 5 revenue streams with year-1 / year-2 forecasts
- [Security](./docs/SECURITY.md) — production hardening checklist
- [Release notes v0.1](./RELEASE_NOTES.md)
- [Next steps](./docs/NEXT_STEPS.md) — current priorities (edge live; bottleneck is distribution)
- [ADRs](./docs/adr/) — architecture decision records

## License

Proprietary. Contact for licensing terms.
