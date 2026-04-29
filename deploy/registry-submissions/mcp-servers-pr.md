# PR text for the official MCP Servers list

## Where to submit

Repository: <https://github.com/modelcontextprotocol/servers>
File to edit: `README.md` — section **"Community Servers"** (alphabetical).

## Insert this entry (alphabetical position: between "Slack" and "Stripe")

```markdown
- **[SMB Broker](https://github.com/YOUR_GITHUB_USERNAME/smb-broker)** —
  Horizontal agent-to-business action layer. 12 tools for finding, verifying,
  messaging, and scheduling with small/mid-sized businesses worldwide, with
  built-in TCPA / GDPR / CASL / 10DLC compliance and channel fallback
  (Cal.com → voice AI → SMS → email → web form). Free tier for agents:
  100 ops/month.
```

## PR title

```
Add SMB Broker — agent-to-business action layer with worldwide compliance
```

## PR body template

```
This PR adds **SMB Broker** to the Community Servers list.

### What it does
Horizontal MCP server that lets any agent discover, verify, communicate
with, and schedule appointments with small/mid-sized businesses worldwide.
12 tools; channel fallback handles SMBs whether they use Cal.com, Calendly,
Doctolib, Booksy, OpenTable, or no booking system at all.

### Why it's useful for the MCP ecosystem
Every existing booking-related MCP server is vertical (one tool — Cal.com,
Twilio, etc.). SMB Broker is the horizontal stitching layer with compliance
built in, so an agent doesn't need to wire 5 separate MCP servers and the
TCPA/GDPR/recording-consent rules themselves.

### Discovery surfaces
- MCP: <https://agentbroker.qzz.io/mcp>
- Manifest: <https://agentbroker.qzz.io/.well-known/mcp.json>
- Tools: 12 (find_business, verify_business, send_message, capture_lead,
  schedule_appointment, send_transactional_confirmation, handle_inbound,
  escalate_to_human, get_status, get_outcome, preview_cost, self_test)
- Free tier: 100 ops/month for any agent.

### Tested with
- Claude Desktop (MCP)
- OpenAI function calling (.well-known/openai-tools.json)
- Anthropic tool_use (.well-known/anthropic-tools.json)
- A2A protocol (.well-known/agents.json)
- llms.txt for LLM crawlers

Repo: <https://github.com/YOUR_GITHUB_USERNAME/smb-broker>
Docs: <https://agentbroker.qzz.io/docs>
License: proprietary (free tier for agent use)
```
