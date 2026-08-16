# Agent Broker — SMB Transaction & Communication MCP Server

> **An agent-callable MCP server** that lets autonomous AI agents find, verify, message, schedule with, and transact with small and mid-sized businesses (SMBs) through a single compliance-enforced tool surface.

[![MCP](https://img.shields.io/badge/MCP-streamable--http-blue)](https://agent-broker-edge.basil-agent.workers.dev/mcp)
[![License](https://img.shields.io/badge/license-proprietary-lightgrey)](#license)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Edge](https://img.shields.io/badge/edge-cloudflare%20workers-orange)](./edge)
[![Registry](https://img.shields.io/badge/MCP%20Registry-listed-green)](https://github.com/modelcontextprotocol/servers)
[![Tests](https://img.shields.io/badge/tests-103%2F103%20passing-brightgreen)](./tests)

**Live endpoint:** `https://agent-broker-edge.basil-agent.workers.dev/mcp` (streamable-http, always-on Cloudflare edge)

---

## Why this exists

There are ~60 million long-tail small businesses in the US — barbers, plumbers, accountants, home cleaners — and they have **no API surface**. AI agents that need to schedule a haircut, get a quote, or send a confirmation today must either drive a browser, cold-call by voice, or give up.

This server is the missing middle layer. Agents call us; we route to the right SMB through whichever channel reaches them fastest — Cal.com → SMS → voice AI → email — with full TCPA / GDPR / CASL / 10DLC compliance enforced as a non-bypassable gate.

---

## Current status (honest)

| Capability | Status |
|---|---|
| MCP endpoint (streamable-http) | **Live** — `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| 14 MCP tools | **Live** (callable today) |
| Compliance gate (TCPA/GDPR/CASL) | **Live** |
| REST + A2A + OpenAI/Anthropic tool surfaces | **Live** |
| SMB supply network | **Demo** — 20+ seed SMBs; demo bookings return `demo_smb_no_live_booking` |
| Billing | **Live** — 6 read tools free; 8 write tools require authentication (`X-Agent-Identity` bearer token). Free tier: 100 ops/IP/day; upgrade at $49/mo via Polar. |
| Production SMB onboarding | **Planned** — real businesses not yet enrolled |

> The MCP server is live and callable right now. Bookings hit demo data. Rate-limited free access is active; write tools require a Polar subscription key. x402/USDC per-call billing is no longer the planned payment model.

---

## 14 MCP Tools

All tools are callable via MCP, REST, OpenAI function calling, Anthropic tool_use, or A2A protocol.

| # | Tool | What it does |
|---|---|---|
| 1 | `find_business` | Search SMBs by vertical, location, and capability — **free** |
| 2 | `verify_business` | Confirm an SMB is real, operating, and capable of the requested service — **free** |
| 3 | `send_message` | Send SMS, email, or voice with full compliance pre-check (TCPA/GDPR/CASL) |
| 4 | `capture_lead` | Structured intake of a prospect into an SMB pipeline with CRM integration |
| 5 | `schedule_appointment` | Book, reschedule, or cancel — tries direct booking API, falls back to voice AI |
| 6 | `send_transactional_confirmation` | TCPA-exempt OTPs, booking confirmations, receipts |
| 7 | `handle_inbound` | Classify inbound messages: booking / cancel / opt-out / question / complaint |
| 8 | `escalate_to_human` | Hand off a stuck or ambiguous task to a human operator with full context |
| 9 | `get_status` | Poll the current state of an async operation — **free** |
| 10 | `get_outcome` | Retrieve the final `OutcomeReceipt` (with cost and reason codes) — **free** |
| 11 | `preview_cost` | Estimate cost, latency, and success probability before committing — **free** |
| 12 | `self_test` | Verify service health and all claimed capabilities are responding — **free** |
| 13 | `import_booking_url` | Turn any Cal.com, Calendly, Doctolib, Booksy, OpenTable, Square, Acuity, or Fresha URL into a bookable SMB record |
| 14 | `call_business` | Place a conversational voice-AI phone call to a business on behalf of a consumer |

---

## Quick start

### Connect via MCP (Claude Desktop, Cursor, Continue, etc.)

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://agent-broker-edge.basil-agent.workers.dev/mcp"
    }
  }
}
```

No API key required for read-only tools. State-changing tools accept an optional `X-Agent-Identity` bearer token.

**Free tier:** 100 `tools/call` operations per IP per day. Exceeding the limit returns HTTP 429 with an upgrade link. Upgrade to unlimited at $49/mo: https://buy.polar.sh/polar_cl_zRn6I67zMjFuenkjDme5RCnDYmA3vefHqX1zG3A5Phh

### Discover tools (JSON-RPC)

```bash
curl -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### Call a tool (JSON-RPC)

```bash
curl -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "find_business",
      "arguments": {
        "vertical": "personal_services",
        "location": {"zip_or_city": "30309"},
        "capability": "haircut"
      }
    }
  }'
```

### OpenAI function calling

```python
import httpx, openai
tools = httpx.get(
    "https://agent-broker-edge.basil-agent.workers.dev/.well-known/openai-tools.json"
).json()["tools"]
client = openai.OpenAI()
resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Book a haircut in Atlanta Saturday under $50"}],
    tools=tools,
)
```

### Anthropic tool use

```python
import httpx, anthropic
tools = httpx.get(
    "https://agent-broker-edge.basil-agent.workers.dev/.well-known/anthropic-tools.json"
).json()["tools"]
client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    tools=tools,
    messages=[{"role": "user", "content": "Book a haircut in Atlanta Saturday under $50"}],
)
```

### Plain REST

```bash
curl -X POST https://agent-broker-edge.basil-agent.workers.dev/ops/find_business \
  -H "Content-Type: application/json" \
  -d '{"vertical":"personal_services","location":{"zip_or_city":"30309"},"capability":"haircut"}'
```

---

## Discovery surfaces

| Surface | URL |
|---|---|
| **MCP (streamable-http)** | `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| MCP descriptor | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/mcp.json` |
| OpenAI function tools | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/openai-tools.json` |
| Anthropic tool_use | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/anthropic-tools.json` |
| A2A (Agent-to-Agent) | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/agents.json` |
| OpenAI ChatGPT plugin | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/ai-plugin.json` |
| llms.txt | `https://agent-broker-edge.basil-agent.workers.dev/llms.txt` |
| OpenAPI 3.1 | `https://agent-broker-edge.basil-agent.workers.dev/openapi.yaml` |
| Glama MCP Registry | Listed via [`glama.json`](./glama.json) |
| MCP Registry | Listed via [`server.json`](./server.json) |

---

## Architecture

```
AI agent
   |
   v  MCP / REST / A2A
Cloudflare Worker edge  (agent-broker-edge.basil-agent.workers.dev)
   |  300+ PoPs globally -- discovery served from edge bundle in 40-70 ms
   |
   +-- GET /.well-known/* /manifest /llms.txt  --> embedded snapshot (40-70 ms)
   +-- POST /mcp  initialize / tools/list      --> embedded snapshot (40-65 ms)
   +-- POST /mcp  tools/call  /ops/*           --> proxy to origin  (170-190 ms)
                |
                v
        Python FastAPI  (smb-broker.onrender.com)
                |  Cron keep-alive every 2 min (eliminates Render cold starts)
                |
                +-- 14 operation handlers  (core/)
                +-- Compliance gate        (compliance/pre_check)
                +-- Channel adapters       (channels/ -- Twilio, Cal.com, Vapi, SendGrid)
                +-- Billing + outcome store
                +-- All .well-known / MCP endpoints (also served from edge bundle)
```

The edge worker can outlive the origin: discovery still works even if the origin is down. Idempotency is keyed by `(agent_id, operation, idempotency_key)` with 24h TTL. Async operations return `pending_async`; poll with `get_status` / `get_outcome`.

---

## Compliance

Every outbound communication passes through `compliance/pre_check()`:

1. **Content classification** — blocks restricted categories (gambling, adult, cannabis, spam)
2. **Opt-out check** — TCPA STOP keyword, GDPR right-to-be-forgotten, CASL
3. **Consent check** — TCPA written consent, GDPR opt-in, CASL implied/express
4. **10DLC registry check** — US SMS campaign compliance
5. **Two-party recording consent** — CA, FL, IL, MD, MA, MT, NV, NH, PA, WA
6. **Audit log** — PII stored as SHA-256 hash, never plaintext

Violations surface as `ComplianceViolationError` and are never silently bypassed.

---

## Repo layout

```
agentbroker/
+-- core/                  # 14 operation handlers + shared Pydantic models
+-- channels/              # Twilio, SendGrid, Vapi, Bland, Cal.com, Playwright
+-- compliance/            # pre_check, jurisdiction_rules, consent_store, audit_log
+-- reliability/           # retry, circuit_breaker, channel_fallback, async_runner
+-- billing/               # meter, budget_guard, receipt_signer, pricing_tiers
+-- telemetry/             # tracer, log_redactor, metrics_emitter
+-- storage/               # outcome_store, idempotency_store
+-- supply/                # smb_directory (20+ seed/demo SMBs)
+-- onboarding/            # self_serve, verification_flow, channel_capture
+-- feedback/              # failure_classifier, attribution_engine, outcome_evaluator
+-- optimizer/             # ab_router, selection_analytics, weekly_report
+-- agent_interface/       # manifest_server, mcp_server, well_known, identity, webhooks
+-- manifest/              # manifest.json, mcp_tools.json, openapi.yaml
+-- api/                   # errors.md, identity.md, async.md
+-- docs/                  # mission, architecture, compliance, ADRs
+-- edge/                  # Cloudflare Worker (TypeScript/Hono)
+-- deploy/                # Dockerfile, docker-compose.yml
+-- tests/                 # unit, contract, compliance, fault_injection, agent_sim
+-- main.py                # FastAPI entry point
+-- config.py              # Centralized config from env
+-- requirements.txt
```

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests (103 passing)
python -m pytest tests/ -q

# Start the API
python main.py
# --> http://localhost:8000/docs      (Swagger UI)
# --> http://localhost:8000/mcp       (MCP endpoint)
# --> http://localhost:8000/manifest  (capability manifest)

# Run the agent simulation harness
python -m tests.agent_sim.harness

# Self-test
python -c "import asyncio; from agent_interface.self_test import run_self_test; print(asyncio.run(run_self_test()).all_passed)"
```

Or with Docker:

```bash
docker compose -f deploy/docker-compose.yml up
```

---

## Documentation

- [Architecture](./docs/architecture.md) — module map, data flow, fallback chains
- [Compliance](./docs/compliance.md) — full jurisdiction matrix, pre-check sequence
- [Agent integration guide](./docs/AGENT_INTEGRATION_GUIDE.md) — copy-paste examples for every protocol
- [API errors](./api/errors.md) — 16 error codes with retry semantics
- [API identity](./api/identity.md) — Agent-Identity JWT spec
- [API async](./api/async.md) — execution profiles, polling rules, webhook contract
- [Benchmarks](./docs/BENCHMARKS.md) — measured WinRate, latency, cost vs alternatives
- [Mission](./docs/mission.md) — north-star metric and scope

---

## Contributing

This is a proprietary project. Issues and discussion are welcome — open a GitHub issue to report bugs or suggest features. Pull requests require prior discussion with the maintainer.

---

## License

Proprietary — contact for licensing terms.

---

*Built by [Basil Al-Shukaili](https://github.com/basilalshukaili). Listed on the [MCP Registry](https://github.com/modelcontextprotocol/servers) and [Glama](https://glama.ai/mcp/servers).*
