# Next Steps to Go Live

> **Where we are:** v0.1 functionally complete, 81/81 tests passing, simulated WinRate 0.818, discoverable via 7 agent protocols (MCP + 6 well-known formats), revenue model defined.
>
> **Where we're going:** Production traffic from real AI agents.
>
> Below is the ordered list of work between now and that.

---

## Phase 1 — Real-world validation (2-3 weeks)

The simulation says we win. The next question is: **does the simulation predict reality?**

### 1.1 Run live integration tests against real channel APIs (sandbox)
- Wire actual Twilio test credentials and run `tests/integration/test_twilio_sms.py` (to be created — currently stubbed).
- Wire actual SendGrid sandbox and run `tests/integration/test_sendgrid_email.py`.
- Wire actual Cal.com developer account.
- Wire actual Vapi (or Bland) sandbox.

**Owner:** Reliability + Compliance. **Gate:** All integration tests pass against real APIs in sandbox before any production traffic.

### 1.2 Recruit 3 design partners (real AI agent platforms)
- Find 3 agent products in beta that have a use case for SMB outreach: e.g., a personal-assistant agent, a sales-development agent, a concierge agent.
- Offer 90 days free of Business tier in exchange for honest feedback + permission to use anonymized usage data.
- Run their traffic against staging for 2 weeks before production.

**Owner:** Founder. **Gate:** ≥ 2 of 3 design partners say "yes, we'd pay for this" after 30 days.

### 1.3 Onboard 50 real SMBs in 1 metro
- Pick Atlanta (already seeded) or Boston.
- Manually call / email 200 SMBs in personal services + home services + professional services.
- Use the `onboarding/self_serve` + `verification_flow` modules that already exist.
- Convert at least 50 to verified active SMBs with at least one channel (Cal.com, phone, or email) verified.

**Owner:** Supply ops (founder for now). **Gate:** 50 verified SMBs across 3 verticals; ≥ 30% have direct API channels.

---

## Phase 2 — Infrastructure hardening (3-4 weeks, parallel with Phase 1)

### 2.1 Move stores from in-memory → managed services
- IdempotencyStore → Redis (already designed; just wire it).
- OutcomeStore → PostgreSQL.
- AuditLog → PostgreSQL with `INSERT-only` role permissions.
- ConsentStore → PostgreSQL.

**Owner:** Infrastructure. **Estimate:** 5-7 days. **Gate:** Pass `pytest tests/` AND `python -m tests.agent_sim.harness` against the persistence-backed stack.

### 2.2 Replace identity stub with real JWT
- Drop the `agent_interface.identity._sign` HS256 stub.
- Use `python-jose` with RS256 + key from AWS KMS (or HashiCorp Vault).
- Token TTL = 1 hour, refresh tokens for 7 days.

**Owner:** Security. **Gate:** All tests pass with real JWT; rotation drill demonstrated.

### 2.3 Wire real channel credentials
- Production Twilio account with registered 10DLC brand + campaign.
- Production SendGrid with verified senders.
- Production Vapi assistant with two-party-consent prompt deployed.
- Production Cal.com developer account.

**Owner:** Compliance + Reliability. **Gate:** Send 100 test transactional messages successfully across all 4 channels.

### 2.4 Multi-region deploy
- Deploy `api` + `worker` to 2 regions (us-east-1, us-west-2) with Route 53 latency-based routing.
- Postgres primary in us-east-1 with read replica in us-west-2.
- Redis Sentinel cluster.

**Owner:** Infrastructure. **Estimate:** 2 weeks. **Gate:** Synthetic uptime monitor reports 99.9% over 7 days.

### 2.5 Complete the SECURITY.md TODO list
See [SECURITY.md](./SECURITY.md). All items in **Identity**, **Compliance gate**, and **Data protection** sections must be DONE before live customer traffic.

---

## Phase 3 — Discovery & marketing (parallel)

### 3.1 Submit to MCP registries
- Submit to the [MCP Servers list](https://github.com/modelcontextprotocol/servers).
- Submit to Smithery, the MCP server registry.
- Add to Anthropic's MCP showcase.

### 3.2 Submit to OpenAI plugin store
- Once OpenAI re-opens plugin submissions; in the meantime publish to GPT Store as an Action.

### 3.3 Submit to Cursor / Continue / Claude Desktop default registries
- These IDEs ship default MCP server lists. Get added.

### 3.4 SEO + LLM SEO
- `llms.txt` already published. Verify Anthropic and OpenAI crawlers can fetch it.
- Add OpenGraph tags for human discovery.
- Publish a developer landing page.

### 3.5 Content
- One blog post per operation: "How to book an SMB appointment with an AI agent."
- One SDK example repo on GitHub: `smb-broker-examples`.
- One YouTube demo: 90-second "agent books a haircut in real time."

---

## Phase 4 — First-50 monetization (months 2-3)

### 4.1 Launch the Verified listing tier ($29/mo) with seeded SMBs
- Offer the first 100 verified SMBs free for 6 months in exchange for testimonials.
- Convert 30+ to paid Verified at end of free period.

### 4.2 Open paid agent tiers (Developer $49, Business $499)
- Stripe integration for billing.
- Auto-bill on the 1st of each month for previous-month usage.
- Self-serve dashboard for usage / overages / receipts.

### 4.3 Activate outcome-based premium pricing
- Default `schedule_appointment` and `capture_lead` to outcome-pricing for Business+ tiers.
- Display split of base vs success premium in invoices.

### 4.4 Targets at end of month 3
- 5 paying agent customers (at least 1 Business tier).
- 30 paying SMBs at Verified.
- 5 SMBs at Featured.
- $5,000 MRR.

---

## Phase 5 — Scale (months 4-12)

### 5.1 Vertical expansion
After Atlanta + Boston are at 80%+ supply density:
- Expand to 3 more metros: Austin, Denver, Seattle.
- Add a 4th vertical: pet services or auto repair.

### 5.2 The first marquee partnership
- Identify one large agent platform (e.g., a vertical-specific AI assistant) doing >100k operations/month.
- Custom Enterprise deal: revenue share + dedicated SLA.

### 5.3 Analytics product activation
- At ≥ 10k searches/zip/month: launch SMB-side analytics product.
- Pricing: $99/mo per SMB tier, $999/mo per franchise tier.

### 5.4 Certifications
- SOC 2 Type 1 audit kicked off (target: month 6).
- GDPR DPIA filed (target: month 4 if EU traffic > 10%).

---

## Final acceptance — "ready to be called by AI agents"

The product is production-ready when **all of these are true**:

| Gate | How to verify | Status |
|------|---------------|:-----:|
| All P0–P14 phases delivered | This repo | ✅ |
| 81+ tests passing | `pytest tests/ -q` | ✅ |
| WinRate ≥ 0.75 in simulation | `python -m tests.agent_sim.harness` | ✅ (0.818) |
| MCP server live and responding | `curl POST $URL/mcp` with `{method:"tools/list"}` | ✅ (local) |
| `.well-known/*` discovery surfaces respond | 6 endpoints under `/.well-known/` | ✅ |
| `llms.txt` published and crawlable | `curl $URL/llms.txt` | ✅ |
| OpenAPI spec at `/openapi.yaml` | `curl $URL/openapi.yaml` | ✅ |
| Real Twilio + SendGrid + Cal.com sandbox tests pass | `pytest tests/integration/` | 🟡 PHASE 1 |
| 50+ verified SMBs in 1 metro | `python -c "from supply.smb_directory import SMBDirectory; print(SMBDirectory().size())"` | 🟡 PHASE 1 |
| Real JWT (RS256) issuance & validation | Identity tests with KMS key | 🟡 PHASE 2 |
| Postgres + Redis backed stores | Run with `ENVIRONMENT=production` | 🟡 PHASE 2 |
| Multi-region deploy with 99.9% synthetic uptime | DataDog synthetic monitor | 🟡 PHASE 2 |
| Stripe billing live | `https://billing.agentbroker.qzz.io` | 🟡 PHASE 4 |
| 3 design partners using staging | Founder report | 🟡 PHASE 1 |
| 5 paying agent customers | Stripe dashboard | 🟡 PHASE 4 |
| Public status page | `status.agentbroker.qzz.io` | 🟡 PHASE 2 |

**Time-to-revenue from today: ~12 weeks.** That's the realistic, conservative path. Aggressive path is 8 weeks if Phase 2 hardening runs in parallel with Phase 1 design partners.

---

## What's already production-quality (DON'T re-do)

These don't need rework before launch:

- The 12 operation handlers and their request/response models.
- The compliance pre-check pipeline.
- The OutcomeReceipt schema and the 16-error-code catalog.
- The manifest, OpenAPI spec, and MCP tool schemas.
- The agent simulation harness.
- The pricing model (tiers and outcome premium).
- The discovery surfaces (`.well-known/`, `llms.txt`, `mcp_server.py`).
- The test suite structure.

Focus all engineering time on Phase 1 (validation) and Phase 2 (hardening). The product itself is correct.
