# Strategy Review — Honest Audit

> Date: 2026-04-28
> Triggered by founder feedback: "I'm in Oman. Stripe doesn't work. I want worldwide. I want facts not assumptions. You can throw away what's wrong."

This document is a brutally honest audit of what we built vs what will actually win. It is not a sales pitch. It identifies what stays, what changes, and what we shouldn't have built.

---

## What we built (recap)

A US-centric SMB Transaction & Communication Broker:
- 12 operations (find_business, schedule_appointment, send_message, ...)
- 7 discovery surfaces (MCP, OpenAI plugin, Anthropic tools, A2A, llms.txt, ...)
- US compliance (TCPA / 10DLC / two-party recording consent)
- 20 hardcoded seed SMBs in Atlanta and Boston
- Stripe-based billing
- SendGrid for email
- Simulated WinRate of 0.818

## What's wrong with that for our actual situation

| Assumption baked in | Reality |
|---|---|
| US-only compliance regime | Founder is in Oman; can't legally register US 10DLC (needs US LLC + EIN). |
| 20 hardcoded Atlanta/Boston SMBs | Founder cannot drive to Atlanta to onboard real SMBs. They're decorative. |
| Stripe billing | Stripe doesn't operate in Oman as a recipient. |
| SendGrid | User couldn't configure it; we should not depend on a specific vendor. |
| WinRate 0.818 | Measured against 3 strawman competitors. Means nothing for actual selection by Claude / GPT. |
| "60M US SMBs" market sizing | We can't reach them from Oman without partners. |

**Honest verdict:** what we built is technically correct but strategically aimed at a market the founder cannot serve.

---

## What's still right (DO NOT discard)

These pieces are jurisdiction-neutral and worth keeping:

1. **The 12-operation surface** — `find_business`, `verify_business`, `send_message`, etc. are universal.
2. **MCP + 6 well-known discovery surfaces** — every agent ecosystem can find us.
3. **OutcomeReceipt + 16 error codes** — universal response schema.
4. **Compliance pre-check architecture** — the *gate* is right; what changes is *what rules fire*.
5. **Channel fallback chain** — universal pattern.
6. **Idempotency model** — universal.
7. **Manifest / OpenAPI / MCP tool generation from one source** — universal.
8. **Test harness structure** — universal.
9. **Deploy scaffolding** — universal.

Roughly **80% of the code stays**.

## What changes

### 1. Compliance: from US-default to jurisdiction-detected

Old: country defaults to "US"; TCPA / 10DLC fire by default.
New: country is **required** on every outbound op. Compliance rules table extended with: AE, SA, OM, IN, PK, JP, SG, ID, KR, FR, DE, IT, ES, NL, BE, AU, NZ, BR, MX. Default behavior for unknown jurisdictions is: **opt-in required, no marketing, transactional-only, no recording**. Conservative-first.

### 2. Supply: from hardcoded to bring-your-own + scraped

Old: 20 fake Atlanta/Boston SMBs.
New: directory starts EMPTY. Two ingest paths:
- **Self-serve onboarding** (already built) — SMB enters their own info + Cal.com URL.
- **Public booking-page scraper** — given any Cal.com / Calendly / Square / OpenTable / Doctolib / Booksy URL, the system auto-extracts the SMB profile.

This makes the directory **worldwide from day one**, with zero manual data entry by a founder in Oman.

### 3. Email: SendGrid → Resend

SendGrid free tier is 100 emails/day, requires phone verification, and the user couldn't configure it from Oman. **Resend** has 3,000 emails/month free, works globally, ships in 30 seconds.

### 4. Voice: Vapi (already wired) — no change

Vapi works in Oman, you have credentials, voice is the universal API for businesses that don't have a website.

### 5. SMS: Twilio (already wired) — but defer 10DLC indefinitely

You have Twilio credentials but cannot register 10DLC without a US LLC. Strategy:
- **Use Twilio for transactional SMS only**, internationally where 10DLC doesn't apply.
- For US numbers, **fall back to email or voice** instead of SMS until US entity exists.
- Document this clearly: "We don't do US marketing SMS today. Use voice or email."

### 6. Billing: Stripe → Polar.sh + Lemon Squeezy + Crypto

Three options the founder can pick on day-one without leaving Oman:

| Provider | Why it works for Oman |
|---|---|
| **Polar.sh** | Merchant of Record. Pays out via international wire to local bank. Confirmed Oman support. |
| **Lemon Squeezy** | MoR. Pays out via Wise / direct deposit. Confirmed Oman support. |
| **Coinbase Commerce** | Crypto (USDC). Settles to any wallet, anywhere. Zero KYC blocker. |

We build a `BillingProvider` interface. The founder picks one and provisions it in 30 minutes.

### 7. WinRate measurement: simulation → real-world A/B

The 0.818 number is a model output, not evidence. We mark it as such. Replace with a **public dashboard** that updates as real agent traffic comes in. Until we have 100 real agent calls, the WinRate field reads `"insufficient_data"`.

### 8. Pricing: keep the model, soften the language

Pricing tiers stay. We add a **"Worldwide free tier"** as the headline because that's what acquires agents. Premium pricing kicks in only when measurable value is delivered.

---

## What we should NOT have built (and what to do about it)

| Item | Why it's wrong | What to do |
|---|---|---|
| 20 fake SMBs | Implies fake supply, hurts credibility | DELETE; add scraping ingest |
| US-state-specific recording-consent table | Useful but wrongly implies we're US-first | KEEP as US adjunct, DEFAULT to international jurisdictions |
| `core/preview_cost` hardcoded US prices | Wrong currency assumption | KEEP USD pricing (it's the agent-tool standard) but allow currency override |
| Atlanta/Boston-only test corpus in agent_sim | Misleading WinRate signal | EXTEND with international tasks |

---

## What's the ACTUAL competitive moat?

Honest list, ranked by realistic defensibility for a solo founder:

1. **Speed to MCP registries** — being among the first 50 production-ready MCP servers in the discovery registries is a real advantage. The window closes fast (months, not years).

2. **Booking-system metadata graph** — the data of "this SMB uses Cal.com, that one uses Doctolib, that one uses Booksy" is genuinely missing from the world. Build it once, others have to rebuild. **This is the realistic moat.**

3. **Compliance-aware messaging proxy** — every agent platform NEEDS a "send-with-compliance" service. Building it once and licensing it is a real business.

4. **NOT a moat: any individual operation.** Cal.com booking is a commodity. SMS is a commodity. Voice AI is a commodity. The moat is **stitching** them together with compliance and metadata.

5. **NOT a moat: "compliance handling."** Anyone with two engineers can copy the rules. The moat is the *audit trail* + the data.

---

## What "early advantage" actually means here

The user said "we are early in the market." Specifically:

- **MCP launched Nov 2024** — the protocol is ~1.5 years old now.
- **Smithery / MCP Servers list** — registries with ~thousands of servers, growing fast. Not yet saturated.
- **The vast majority of MCP servers today are dev-tool integrations** (GitHub, databases, file systems). **Almost nothing connects agents to physical-world businesses.** This is a real gap.
- **Vapi, Cal.com, Twilio all have MCP servers themselves** — but they're vertical (one tool each). The horizontal "agent-to-any-business" layer doesn't exist yet.

So the early advantage is real, but it's narrower than "the SMB market." It's specifically: **be the first horizontal agent-to-business action layer in the MCP ecosystem.**

If we don't ship in the next 30 days, someone else fills this slot.

---

## Decision matrix: what gets done in this pivot

| Action | Owner | Status |
|---|---|---|
| Wire Twilio with real creds | Code | NOW |
| Wire Cal.com with real creds | Code | NOW |
| Wire Vapi with real creds | Code | NOW |
| Replace SendGrid → Resend | Code | NOW |
| Replace Stripe → Polar+LemonSqueezy+Crypto interfaces | Code | NOW |
| Strip US-only compliance defaults | Code | NOW |
| Strip 20 hardcoded SMBs → empty directory | Code | NOW |
| Add public booking-page scraper | Code | NOW |
| Mark WinRate 0.818 as "simulated" | Docs | NOW |
| Generate registry submissions (PR templates) | Code | NOW |
| Generate one-click deploy configs (Fly.io free tier) | Code | NOW |
| Build credential validation script | Code | NOW |
| Public landing page (HTML) | Code | NOW |
| ONE master HANDOFF doc | Docs | NOW |
| Sign up for Resend / Polar / Lemon Squeezy / Coinbase | Founder | LATER |
| Submit to MCP registries (founder's GitHub) | Founder | LATER |
| Buy domain | Founder | LATER |
| Deploy to Fly.io with founder's account | Founder | LATER |
| Run real agent traffic + measure | Both | AFTER LAUNCH |

---

## Bottom line

**What we had:** an over-engineered US local-services broker the founder couldn't operate.
**What we're shipping:** a worldwide horizontal agent-to-business action layer with real credentials wired, jurisdiction-detected compliance, founder-runnable billing, and 7 agent-discovery protocols.

The pivot is not a rewrite. It's a **scope correction + 4 vendor swaps + dehardcoding the assumption that the founder lives in Atlanta**.

We keep the architecture. We change the marketing surface and the defaults. The founder ships in days, not months.
