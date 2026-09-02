# Hacker News "Show HN" post

Submit at: <https://news.ycombinator.com/submit>
Type: Show HN
Best timing: Tuesday – Thursday, 9-11am Eastern (US peak), launch URL ready.

## Title (80 char max — this is 72)
```
Show HN: MCP tools with offline-verifiable compliance receipts (Ed25519)
```

## URL
```
https://hatchloop.dev/agent-broker/
```

## Text (use sparingly — HN prefers links; only add if it helps reviewers)
```
We built this because two things kept bugging us about "compliance" and
"verification" tools for AI agents: you have to trust the vendor's word at
query time, and most business-data tools are actually screen-scrapes with no
citable source.

What's there:
- 23 MCP tools. 15 work with no key at all (12 always-free + 3 free within a
  daily quota — 100/day anonymous, 500/day with a free key). The 8 write
  tools (send a message, book an appointment, etc.) need a free
  email-verified key, 100 write ops/day.
- Ed25519-signed compliance receipts: screen_sanctions and check_compliance
  return a hash-bound receipt of exactly what was screened, against which
  list snapshot, under which rule. Verify it offline, months later, against
  the public key at hatchloop.dev/agents.md — no callback to us.
- Real data from official sources: OFAC SDN, the EU Consolidated list, the UK
  Sanctions List (screen_sanctions), GLEIF LEI + SEC EDGAR
  (verify_company_record), USASpending.gov federal contract awards
  (lookup_us_contracts).
- Business messaging + scheduling with a non-bypassable TCPA/GDPR/CASL/PDPL
  compliance gate across 26 jurisdictions, and channel fallback across
  WhatsApp/SMS/email/voice.
- Discovery: MCP, OpenAI function calling, Anthropic tool_use, A2A, llms.txt,
  OpenAPI.
- Free tier by design: reads are free forever; write ops get 100/day free
  with an email-verified key; credits by card ($9/1,000 and up) or pay-per-call
  in USDC on Base via x402 (no signup) beyond that.

Honest status: the SMB directory used by find_business/verify_business is a
small seed set (demo data) today — real businesses are not yet onboarded at
volume, and demo bookings say so explicitly rather than pretending to be
live. The compliance-receipt and official-data-source tools are the part
we'd stake the launch on; everything is MIT licensed.

What I'd love feedback on:
- Compliance — what jurisdictions or receipt fields would make this usable
  as evidence in your own audit trail?
- Data — which other official, redistribution-licensed sources should we add
  next (we specifically skipped the UN consolidated list; it isn't licensed
  for commercial redistribution)?
- Tools — what's missing for you to actually wire this into an agent today?

Repo, docs, and live MCP endpoint at the URL above. AMA.
```
