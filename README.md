# Agent Broker  -  SMB Transaction & Communication MCP Server

> **An agent-callable MCP server** that lets autonomous AI agents find, verify, message, schedule with, and transact with small and mid-sized businesses (SMBs) through a single compliance-enforced tool surface.

[![MCP](https://img.shields.io/badge/MCP-streamable--http-blue)](https://hatchloop.dev/mcp/agent-broker)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Edge](https://img.shields.io/badge/edge-cloudflare%20workers-orange)](./edge)
[![Registry](https://img.shields.io/badge/MCP%20Registry-listed-green)](https://github.com/modelcontextprotocol/servers)
[![Tests](https://img.shields.io/badge/tests-103%2F103%20passing-brightgreen)](./tests)

**Live endpoint:** `https://hatchloop.dev/mcp/agent-broker` (streamable-http, always-on Cloudflare edge)

---

## Why this exists

There are ~60 million long-tail small businesses in the US  -  barbers, plumbers, accountants, home cleaners  -  and they have **no API surface**. AI agents that need to schedule a haircut, get a quote, or send a confirmation today must either drive a browser, cold-call by voice, or give up.

This server is the missing middle layer. Agents call us; we route to the right SMB through whichever channel reaches them fastest  -  Cal.com -> SMS -> voice AI -> email  -  with full TCPA / GDPR / CASL / 10DLC compliance enforced as a non-bypassable gate.

---

## Current status (honest)

| Capability | Status |
|---|---|
| MCP endpoint (streamable-http) | **Live**  -  `https://hatchloop.dev/mcp/agent-broker` |
| 19 MCP tools | **Live** (callable today) |
| Compliance gate (TCPA/GDPR/CASL) | **Live** |
| REST + A2A + OpenAI/Anthropic tool surfaces | **Live** |
| SMB supply network | **Demo**  -  20+ seed SMBs; demo bookings return `demo_smb_no_live_booking` |
| Billing | **Live**  -  8 utility tools free (no key, unmetered). Premium data tools (company verification, sanctions, trade screening): free up to a daily limit (50/day with free key, 20/day anonymous), then $0.02/call via credits or x402. Write tools: free email-verified key (50 ops/day) at hatchloop.dev/agent-broker; credit packages from $9/1,000 credits at hatchloop.dev/pricing; or agents pay per-call via x402. |
| x402 payment rail | **Verified** on Base mainnet (tx 0x38a0d9ec) |
| Production SMB onboarding | **Planned**  -  real businesses not yet enrolled |

> The MCP server is live and callable right now. Bookings hit demo data. 8 utility tools are free (no key, unmetered). Premium data tools (verify_company_record, screen_sanctions, map_trade_restriction) are free up to a daily limit; beyond that, $0.02/call via credits or x402. Write tools require a free email-verified key (50 ops/day)  -  get one at https://hatchloop.dev/agent-broker. Credit packages from $9/1,000 credits at https://hatchloop.dev/pricing.

---

## 19 MCP Tools

All tools are callable via MCP, REST, OpenAI function calling, Anthropic tool_use, or A2A protocol.

| # | Tool | What it does | Auth |
|---|---|---|---|
| 1 | `find_business` | Search SMBs by vertical, location, and capability | **free** |
| 2 | `verify_business` | Confirm an SMB is real, operating, and capable of the requested service | **free** |
| 3 | `get_status` | Poll the current state of an async operation | **free** |
| 4 | `get_outcome` | Retrieve the final `OutcomeReceipt` (with cost and reason codes) | **free** |
| 5 | `preview_cost` | Estimate cost, latency, and success probability before committing | **free** |
| 6 | `self_test` | Verify service health and all claimed capabilities are responding | **free** |
| 7 | `check_booking_link` | Classify a URL and confirm import_booking_url will accept it  -  sub-100ms pre-flight | **free** |
| 8 | `check_compliance` | Preview TCPA/GDPR/CASL/10DLC gate result before spending a paid send | **free** |
| 9 | `verify_company_record` | Live GLEIF LEI registry + SEC EDGAR lookup  -  official legal name, status, jurisdiction, address | **free up to daily limit** |
| 10 | `screen_sanctions` | Check a name or entity against OFAC SDN, EU Consolidated, UN, UK HM Treasury + 40+ lists via OpenSanctions | **free up to daily limit** |
| 11 | `map_trade_restriction` | OFAC country embargoes + export-control Entity List + sanctioned-party screening for a proposed shipment | **free up to daily limit** |
| 12 | `send_message` | Send SMS, email, or voice with compliance pre-check enforced | key |
| 13 | `capture_lead` | Structured intake of a prospect into an SMB pipeline with CRM integration | key |
| 14 | `schedule_appointment` | Book, reschedule, or cancel  -  tries direct booking API, falls back to voice AI | key |
| 15 | `send_transactional_confirmation` | TCPA-exempt OTPs, booking confirmations, receipts | key |
| 16 | `handle_inbound` | Classify inbound messages: booking / cancel / opt-out / question / complaint | key |
| 17 | `escalate_to_human` | Hand off a stuck or ambiguous task to a human operator with full context | key |
| 18 | `import_booking_url` | Turn any Cal.com, Calendly, Doctolib, Booksy, OpenTable, Square, Acuity, or Fresha URL into a bookable SMB record | key |
| 19 | `call_business` | Place a conversational voice-AI phone call to a business on behalf of a consumer | key |

Free key (50 write ops/day + 50 premium data calls/day): https://hatchloop.dev/agent-broker  -  Credits from $9/1,000 ops: https://hatchloop.dev/pricing  -  Premium data beyond quota: $0.02/call

---

## Quick start

### Connect via MCP (Claude Desktop, Cursor, Cline, Continue, etc.)

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker"
    }
  }
}
```

**11 tools require no key** (find_business, verify_business, verify_company_record, screen_sanctions, map_trade_restriction, check_booking_link, check_compliance, get_status, get_outcome, preview_cost, self_test).

**Write tools** require an `X-Agent-Identity` bearer token:
- Free email-verified key (50 ops/day): https://hatchloop.dev/agent-broker
- Credits from $9/1,000 ops: https://hatchloop.dev/pricing

Add your key to the config once you have one:

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": {
        "X-Agent-Identity": "Bearer YOUR_KEY_HERE"
      }
    }
  }
}
```

### Or via npx (stdio transport)

```bash
npx agentbroker-mcp
```

With a key:

```bash
AGENT_BROKER_KEY=your_key npx agentbroker-mcp
```

### Discover tools (JSON-RPC)

```bash
curl -X POST https://hatchloop.dev/mcp/agent-broker \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### Call a tool (JSON-RPC)

```bash
curl -X POST https://hatchloop.dev/mcp/agent-broker \
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
    "https://hatchloop.dev/.well-known/openai-tools.json"
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
    "https://hatchloop.dev/.well-known/anthropic-tools.json"
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
curl -X POST https://hatchloop.dev/ops/find_business \
  -H "Content-Type: application/json" \
  -d '{"vertical":"personal_services","location":{"zip_or_city":"30309"},"capability":"haircut"}'
```

---

## Discovery surfaces

| Surface | URL |
|---|---|
| **MCP (streamable-http)** | `https://hatchloop.dev/mcp/agent-broker` |
| MCP descriptor | `https://hatchloop.dev/.well-known/mcp.json` |
| OpenAI function tools | `https://hatchloop.dev/.well-known/openai-tools.json` |
| Anthropic tool_use | `https://hatchloop.dev/.well-known/anthropic-tools.json` |
| A2A (Agent-to-Agent) | `https://hatchloop.dev/.well-known/agents.json` |
| OpenAI ChatGPT plugin | `https://hatchloop.dev/.well-known/ai-plugin.json` |
| llms.txt | `https://hatchloop.dev/llms.txt` |
| OpenAPI 3.1 | `https://hatchloop.dev/openapi.yaml` |
| npm shim (stdio) | `npx agentbroker-mcp` |
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
                +-- 19 operation handlers  (core/)
                +-- Compliance gate        (compliance/pre_check)
                +-- Channel adapters       (channels/ -- Twilio, Cal.com, Vapi, SendGrid)
                +-- Billing + outcome store
                +-- All .well-known / MCP endpoints (also served from edge bundle)
```

The edge worker can outlive the origin: discovery still works even if the origin is down. Idempotency is keyed by `(agent_id, operation, idempotency_key)` with 24h TTL. Async operations return `pending_async`; poll with `get_status` / `get_outcome`.

---

## Compliance

Every outbound communication passes through `compliance/pre_check()`:

1. **Content classification**  -  blocks restricted categories (gambling, adult, cannabis, spam)
2. **Opt-out check**  -  TCPA STOP keyword, GDPR right-to-be-forgotten, CASL
3. **Consent check**  -  TCPA written consent, GDPR opt-in, CASL implied/express
4. **10DLC registry check**  -  US SMS campaign compliance
5. **Two-party recording consent**  -  CA, FL, IL, MD, MA, MT, NV, NH, PA, WA
6. **Audit log**  -  PII stored as SHA-256 hash, never plaintext

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

- [Architecture](./docs/architecture.md)  -  module map, data flow, fallback chains
- [Compliance](./docs/compliance.md)  -  full jurisdiction matrix, pre-check sequence
- [Agent integration guide](./docs/AGENT_INTEGRATION_GUIDE.md)  -  copy-paste examples for every protocol
- [API errors](./api/errors.md)  -  16 error codes with retry semantics
- [API identity](./api/identity.md)  -  Agent-Identity JWT spec
- [API async](./api/async.md)  -  execution profiles, polling rules, webhook contract
- [Benchmarks](./docs/BENCHMARKS.md)  -  measured WinRate, latency, cost vs alternatives
- [Mission](./docs/mission.md)  -  north-star metric and scope

---

## Contributing

Licensed under MIT. Issues and discussion are welcome  -  open a GitHub issue to report bugs or suggest features. For substantial changes, please open an issue first to discuss direction. Note: this repo is the open-source server; the hosted service at agent-broker-edge.basil-agent.workers.dev (supply index, billing rails) is operated by Hatchloop.

---

## License

MIT  -  see [LICENSE](LICENSE). The hosted service and its supply/billing data are operated separately by Hatchloop.

---

*Built by [Basil Al-Shukaili](https://github.com/basilalshukaili). Listed on the [MCP Registry](https://github.com/modelcontextprotocol/servers) and [Glama](https://glama.ai/mcp/servers).*
