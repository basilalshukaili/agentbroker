# Hacker News "Show HN" post

Submit at: <https://news.ycombinator.com/submit>
Type: Show HN
Best timing: Tuesday – Thursday, 9-11am Eastern (US peak), launch URL ready.

## Title (80 char max)
```
Show HN: SMB Broker – Horizontal MCP server, agents call businesses worldwide
```

## URL
```
https://agent-broker-edge.basil-agent.workers.dev
```

## Text (use sparingly — HN prefers links; only add if it helps reviewers)
```
We built this after watching agents fail to interact with the long tail
of small businesses — barbers, plumbers, accountants, dentists — who
have no API.

What's there:
- 13 MCP tools (find / verify / message / schedule / import_booking_url / ...)
- Compliance pre-check that fires per jurisdiction (TCPA, GDPR, CASL,
  10DLC, two-party recording consent for 10 US states)
- Channel fallback: Cal.com direct API → voice AI (Vapi) → SMS (Twilio) →
  email (Resend) → web form (Playwright)
- Idempotency keyed per (agent_id, operation, key) with 24h TTL
- 7 discovery surfaces: MCP, .well-known/{ai-plugin,openai-tools,
  anthropic-tools,agents,mcp}.json, llms.txt

Cost model:
- Free for any agent up to 100 ops/month
- Outcome-based: $0.15 attempt + $0.85 only on confirmed booking
- SMB-side: free listing, $29 verified, $99 featured, $499 exclusive
  per (vertical, zip)

What's measured (not assumed):
- 103 / 103 tests pass in 0.40s
- Self-test 6 / 6 in <300ms
- Simulated WinRate 0.818 across 504 trials with adversarial corpus
  (we deliberately included tasks where browser_automation should win)

What I'd love feedback on:
- Pricing — is outcome-based the right model for agent-tool services?
- Compliance — what jurisdictions did we miss? Which need stricter rules?
- Tools — what 13th operation would unlock the most agent use cases?

Repo, docs, and live MCP endpoint at the URL above. AMA.
```
