# PR text for awesome-mcp-servers

Repository: <https://github.com/punkpeye/awesome-mcp-servers>
Section to insert into: **🏢 Workplace & Productivity** (alphabetical by server
name). The category was named "Business & Productivity" when this draft was
first written; the live README now uses "🏢 Workplace & Productivity" — verify
the current heading before opening the PR in case it has moved again.

Per the repo's own `CONTRIBUTING.md`: link the server name to its repository,
one line per entry, alphabetical within the section, concise description.
Agent-submitted PRs can add `🤖🤖🤖` to the PR title for the fast-tracked
review lane.

## Entry

```markdown
- [Agent Broker](https://github.com/basilalshukaili/agentbroker) — MCP server
  whose `screen_sanctions` and `check_compliance` tools return an
  Ed25519-signed compliance receipt, verifiable offline (no callback) against
  the public key at hatchloop.dev/agents.md. Also does live lookups against
  official sources: OFAC/EU/UK sanctions lists, GLEIF, SEC EDGAR, and
  USASpending federal contract awards. 23 tools, 15 usable with no key at all.
  MIT licensed.
```

## PR title

```
Add Agent Broker — offline-verifiable compliance receipts + live business data (23 tools)
```

## PR body

```
Adding Agent Broker to Workplace & Productivity.

What makes it different from other business/scheduling MCP servers:

1. Verifiable compliance receipts. `screen_sanctions` and `check_compliance`
   attach an Ed25519-signed receipt: a hash-bound record of which list copies
   were screened, how fresh they were, what ruleset decided, and what was
   returned. It verifies OFFLINE, months later, against a public key we
   publish at https://hatchloop.dev/agents.md — no call back to us required.
   It asserts facts about our own system's actions, never "this party is
   clean."
2. Real data from official sources, no key required. `screen_sanctions`
   (OFAC SDN + EU Consolidated list + UK Sanctions List), `verify_company_record`
   (GLEIF LEI + SEC EDGAR), and `lookup_us_contracts` (USASpending.gov federal
   award data) are 3 of the 15 (out of 23) tools usable with no authentication at all (the
   other 12 are always-free and unmetered).

It also finds, verifies, messages, and schedules with small/mid-sized
businesses (Cal.com direct booking + 11 further platforms recognised via
`import_booking_url`), with every outbound message routed through the same
non-bypassable compliance gate (TCPA/GDPR/CASL/PDPL across 26 jurisdictions).

- 23 tools total. 15 need no key (12 always-free + 3 free within a daily
  quota of 100/day anonymous, 500/day with a free key). The 8 write tools
  (send_message, schedule_appointment, etc.) need a free email-verified key,
  100 write ops/day, or credits/x402 for volume.
- License: MIT.
- Repo: https://github.com/basilalshukaili/agentbroker

MCP endpoint: https://hatchloop.dev/mcp/agent-broker
Docs: https://hatchloop.dev/docs
```
