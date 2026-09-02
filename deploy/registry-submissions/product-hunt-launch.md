# Product Hunt launch copy

Submit at: <https://www.producthunt.com/posts/new>

## Tagline (60 chars max — this is 57)
```
Compliance receipts and real business data, for AI agents
```

## Description (260 chars max — this is 238)
```
Agent Broker gives AI agents Ed25519-signed compliance receipts (verify offline, no callback) plus live OFAC/EU/UK sanctions, GLEIF, SEC EDGAR and USASpending lookups. 23 MCP tools, 15 usable with no key. MIT licensed, free tier included.
```

## Topics
- Artificial Intelligence
- Developer Tools
- API
- Compliance
- Open Source

## First comment (announce in the comments after launch)
```
Hi PH! We kept running into the same two problems building agent tools for
businesses: "compliance checked" is just our word for it at query time, and
most "verified business data" tools online are scrapes with no citable
source.

So Agent Broker does two things properly:

• Verifiable compliance receipts — screen_sanctions and check_compliance
  return an Ed25519-signed receipt: what was screened, against which list
  snapshot, under which rule, when. Verify it offline months later against
  the public key we publish at hatchloop.dev/agents.md. No callback to us,
  ever.
• Real data, official sources only — OFAC SDN, the EU Consolidated list, the
  UK Sanctions List, GLEIF LEI, SEC EDGAR, USASpending.gov federal contract
  awards. We deliberately skip the UN consolidated list — it isn't licensed
  for commercial redistribution, and the tool says so in its own output
  rather than silently including it.

Plus the original angle: finding, verifying, messaging, and scheduling with
small/mid-sized businesses, gated by the same non-bypassable TCPA/GDPR/CASL/PDPL
compliance check across 26 jurisdictions, with channel fallback across
WhatsApp/SMS/email/voice.

• 23 tools total. 15 work with no key — 12 always-free, 3 free within a
  daily quota (100/day anonymous, 500/day with a free key)
• 8 write tools need a free email-verified key: 100 write ops/day, no card
• Beyond the free tier: credits by card from $9/1,000, or pay-per-call in
  USDC on Base via x402 with no signup at all
• MIT licensed

Discoverable day one through MCP (Claude Desktop / Cursor / Continue / Cline),
OpenAI function calling, Anthropic tool_use, A2A, and llms.txt.

Honest caveat: the small-business directory itself is a small seed set today
(demo data) — we're not claiming a live worldwide SMB network yet. The
compliance-receipt and official-data-source tools are real and are what
we'd stake this launch on.

We'd love feedback from agent builders and anyone who has had to explain a
compliance decision to an auditor after the fact. What would make a receipt
like this actually usable as evidence for you?
```

## Gallery assets needed (founder TODO)
- Hero image (1270×760) showing a `screen_sanctions` call and its signed
  receipt (not a booking screenshot — the receipt is the differentiator)
- Screenshot of `verify_compliance_receipt` succeeding against the public key
  from hatchloop.dev/agents.md
- 30-second demo GIF: call `screen_sanctions` or `check_compliance` from
  Claude Desktop and show the receipt
- Logo (240×240)
