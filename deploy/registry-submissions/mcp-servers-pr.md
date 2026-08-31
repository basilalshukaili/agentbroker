# PR text for the official MCP Servers list

## Where to submit

Repository: <https://github.com/modelcontextprotocol/servers>
File to edit: `README.md` — section **"Community Servers"** (alphabetical).

## Insert this entry (alphabetical position: between "Slack" and "Stripe")

```markdown
- **[Agent Broker](https://github.com/basilalshukaili/agentbroker)** —
  Horizontal agent-to-business action layer. 21 tools for finding, verifying,
  messaging, and scheduling with small/mid-sized businesses worldwide, with
  built-in TCPA / GDPR / CASL / 10DLC compliance and channel fallback
  (Cal.com → voice AI → SMS → email → web form). Free tier for agents:
  100 ops/month. Turns any Cal.com / Calendly / Doctolib / Booksy /
  OpenTable booking URL into a callable SMB via `import_booking_url`.
```

## PR title

```
Add Agent Broker — agent-to-business action layer with worldwide compliance
```

## PR body template

```
This PR adds **Agent Broker** to the Community Servers list.

### What it does
Horizontal MCP server that lets any agent discover, verify, communicate
with, and schedule appointments with small/mid-sized businesses worldwide.
21 tools; channel fallback handles SMBs whether they use Cal.com, Calendly,
Doctolib, Booksy, OpenTable, or no booking system at all. The key
differentiator: `import_booking_url` turns any public booking page URL into
a bookable SMB in one call.

### Why it's useful for the MCP ecosystem
Every existing booking-related MCP server is vertical (one tool — Cal.com,
Twilio, etc.). Agent Broker is the horizontal stitching layer with compliance
built in, so an agent doesn't need to wire 5 separate MCP servers and the
TCPA/GDPR/recording-consent rules themselves.

### Discovery surfaces
- MCP: <https://agent-broker-edge.basil-agent.workers.dev/mcp>
- Manifest: <https://agent-broker-edge.basil-agent.workers.dev/.well-known/mcp.json>
- llms.txt: <https://agent-broker-edge.basil-agent.workers.dev/llms.txt>
- Tools: 13 (find_business, verify_business, send_message, capture_lead,
  schedule_appointment, send_transactional_confirmation, handle_inbound,
  escalate_to_human, get_status, get_outcome, preview_cost, self_test,
  import_booking_url)
- Free tier: 100 ops/month for any agent.

### Infrastructure
- Edge: Cloudflare Worker (300+ PoPs, 40–70 ms discovery globally)
- Origin: Python FastAPI on Render

### Tested with
- Claude Desktop (MCP)
- OpenAI function calling (.well-known/openai-tools.json)
- Anthropic tool_use (.well-known/anthropic-tools.json)
- A2A protocol (.well-known/agents.json)
- llms.txt for LLM crawlers

Repo: <https://github.com/basilalshukaili/agentbroker>
License: proprietary (free tier for agent use)
```
