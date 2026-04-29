# Mission — SMB Transaction & Communication Broker

**Phase P0 Artifact | OrchestratorAgent | v3**

---

## Mission Statement

Build a production-grade, agent-callable transaction and communication broker for the long-tail SMB layer. A single service that autonomous AI agents discover in a tool registry, select over competing services when given a real business goal under cost/time/quality constraints, successfully execute against to reach a real-world small or mid-size business, and pay for through usage.

---

## North-Star Metric: WinRate

```
WinRate = P(selected | in candidate set) × P(task succeeds | selected)
          per dollar of agent budget spent
```

Every architectural decision, every feature addition, every manifest change, and every optimization must be justified against WinRate. If a change does not measurably move WinRate or its components, it is deferred.

**WinRate components tracked separately:**
- `selection_rate` — fraction of tasks where our service is chosen from the candidate set
- `success_rate_when_selected` — fraction of tasks that reach a successful terminal outcome after selection
- `cost_per_dollar_of_agent_budget` — efficiency of spend relative to outcome

---

## Service Selection Rationale

### What we are (one sentence)
A horizontal, agent-callable broker that lets an autonomous agent discover, verify, communicate with, schedule with, transact with, and follow up with the long tail of small/mid businesses through one clean tool surface — routing internally through whatever channel actually reaches the SMB (direct API, voice AI, SMS, email, web form, or browser automation as last resort).

### Why this slice wins
1. **Massive agent demand, no winner.** Consumer-side agents (ChatGPT Operator, Anthropic computer use, Perplexity Comet, Google agentic search) already attempt to transact with long-tail SMBs via brittle browser automation. Every one of those calls is a candidate for our service.
2. **Big players are structurally not pursuing this.** OpenAI does browser automation and single-vertical platform partnerships. Google does platform partnerships (Reserve with Google). Twilio/Vapi/Bland/Retell are voice infrastructure within one SMB at a time. Cal.com/Calendly/Booksy require the SMB to onboard their platform. Nobody is signing up the corner salon, local plumber, and independent dentist into one agent-callable network — the work is unglamorous, requires SMB sales, and is exactly what big-player culture avoids.
3. **The hard part is supply, not the API.** The agent-facing API is table stakes. The supply network is the moat. Every successful transaction adds a verified SMB. Agent selection eventually follows the network because reliable supply outranks clever description.
4. **Position: "agent-native abstraction over fragmented SMB reality."** Browser automation is slow, brittle, and token-expensive. We are the proper API that replaces it.

---

## Competitive Landscape Map

| Layer | Big-player territory | Our position |
|---|---|---|
| Consumer agent UIs | OpenAI Operator/Agent, Anthropic, Perplexity Comet, Google | We are a tool they call |
| Enterprise agent platforms | Salesforce Headless 360, OpenAI Workspace Agents, HubSpot Breeze | We are a tool their agents call |
| Voice channel infrastructure | Vapi, Bland, Retell, Synthflow, Goodcall | We use them as a delivery channel |
| Calendar/scheduling SaaS | Cal.com, Calendly, Booksy, Square Appointments | We integrate with them as a back-end |
| Payments / agentic commerce | Stripe ACP, x402, AP2, UCP | We use them as a payment rail |
| Business directories | Yelp, Google Business Profile | We use them as a discovery source |
| CRM | Salesforce, HubSpot, Attio | We hand off to them when needed |
| Vertical SaaS for SMBs | Booksy, ServiceTitan, Mindbody | We integrate as a back-end |

**Operating principle:** useful to all of them, competitive with none of them. Any feature that creates head-on competition with a big-player core product is rejected (§9.13).

---

## Compliance & Legal Reality Map

| Channel / region | Regulation | Build implication |
|---|---|---|
| US SMS (A2P) | 10DLC registration, carrier filtering | All SMS traffic uses registered campaigns; brand + use-case registration is a precondition for live SMS |
| US Voice | TCPA (consent for autodialed / prerecorded calls), DNC list | Outbound voice requires recorded prior express consent for the recipient + channel + use-case |
| US Voice recording | Federal one-party + state two-party (CA, FL, IL, MD, MA, MT, NV, NH, PA, WA) | Per-state consent prompt at call start; recording mode chosen per recipient location |
| US Email | CAN-SPAM | Functional unsubscribe on every commercial message; sender ID; physical address |
| EU/UK | GDPR, ePrivacy | Lawful basis per recipient, data subject rights, data residency, prior consent for marketing channels |
| Canada | CASL | Express consent + identification + unsubscribe |
| Restricted content | Carrier rules + state law | Gambling, lending, cannabis, adult — categorical filters at content layer |
| Healthcare-adjacent | HIPAA | **Out of scope for v0.1** |

Every outbound channel call passes a `ComplianceAgent` pre-check before reaching a channel adapter. Failed checks return a structured `compliance_violation` error — never silently dropped. Compliance gates (P3, P4, P14) cannot be bypassed by orchestrator override.

---

## Wedge Verticals (GTM Focus)

The service stays **horizontal and channel-agnostic**. Early GTM sales focus:

1. **Personal services** — hair, nails, massage, beauty, spa, fitness/PT
2. **Home services** — cleaning, lawn, repair, pest, plumbing, electrical
3. **Local professional services** — short consultation bookings (lawyer, financial advisor, accountant, tutor)

**Deferred (do not build for these in v0.1):** healthcare (HIPAA), restaurants (OpenTable + OpenAI owns the lane), travel (Booking/Expedia/Priceline), retail/grocery (ACP, UCP, Instacart, DoorDash).

---

## Who We Are NOT (Hard Scope Guardrails)

- **Not a CRM** (HubSpot, Salesforce, Attio) — we hand off to them
- **Not a calendar SaaS** (Calendly, Cal.com) — we integrate with them as a back-end
- **Not voice infrastructure** (Vapi, Bland, Retell) — we use them as a delivery channel
- **Not payments infrastructure** (Stripe ACP, x402, AP2) — we use them as a payment rail
- **Not a directory** (Yelp, Google Business Profile) — we use them as a discovery source
- **Not a consumer agent** (Operator, Comet) — we serve them
- **Not a vertical SaaS** for any one industry — we are horizontal across SMB types

---

## Scope: v0.1

### In scope
- 12 agent-callable operations (find_business, verify_business, send_message, capture_lead, schedule_appointment, send_transactional_confirmation, handle_inbound, escalate_to_human, get_status, get_outcome, preview_cost, self_test)
- Channel adapters: ≥1 direct-API (Cal.com or Square Appointments), ≥1 voice-AI (Vapi or Bland), SMS (Twilio), email (SendGrid), web-form submission, browser-automation last-resort
- Agent-Identity JWT authentication with scope enforcement
- Compliance: consent store, opt-out lists, jurisdiction rules, 10DLC campaign scaffold, recording-consent prompts, audit log, content classifier
- Reliability: idempotency, retries, circuit breakers, channel fallback, async job runner, signed webhook delivery
- Billing: usage metering, preview_cost (±5% accuracy SLO), per-success pricing
- Telemetry: structured logging + distributed tracing with all required spans
- Agent-simulation harness: ≥50 tasks, ≥3 personas, WinRate computation
- Optimization loops: manifest A/B router, failure classifier, weekly reports
- ≥20 seed SMBs across 3 wedge verticals for v0.1 dogfood
- Docker + CI pipeline
- Discovery endpoints: `/.well-known/agent-manifest.json`, `/.well-known/openapi.yaml`, `/mcp/tools`, `/self-test`, `/preview-cost`, `/webhooks/register`

### Out of scope for v0.1
- Healthcare (HIPAA) vertical
- Restaurant, travel, retail verticals
- CRM capabilities
- Multi-tenant SaaS UI / admin dashboard
- Mobile SDKs
- Real-time voice streaming (full call handling, not just scheduling)
- Payment processing (we route to payment rails, we do not hold funds)

---

## Agent Roster

| Agent | Module ownership |
|---|---|
| OrchestratorAgent | Workflow, gate enforcement, conflict resolution |
| ArchitectAgent | /docs/architecture.md, /docs/adr/ |
| ManifestAgent | /manifest/ |
| APIDesignAgent | /api/ |
| ImplementationAgent | /core/, /channels/, /storage/, /supply/ |
| ReliabilityAgent | /reliability/ |
| BillingAgent | /billing/ |
| TelemetryAgent | /telemetry/ |
| TestingAgent | /tests/ |
| SelectionOptimizationAgent | /optimizer/ |
| OutcomeOptimizationAgent | /feedback/, /optimizer/reports/ |
| IntegrationAgent | /deploy/, /.ci/, /onboarding/ |
| ComplianceAgent | /compliance/ |

---

*P0 gate passed. Advancing to P1: Architecture.*
*Handoff to: ArchitectAgent*
