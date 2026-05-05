# PR text for awesome-mcp-servers

Repository: <https://github.com/punkpeye/awesome-mcp-servers>
Section to insert into: `### 💼 Business & Productivity` (alphabetical).

## Entry

```markdown
- [agent-broker](https://github.com/basilalshukaili/agentbroker) —
  Horizontal agent-to-business action layer. 13 tools for finding,
  scheduling, messaging SMBs worldwide; `import_booking_url` turns any
  Cal.com / Calendly / Doctolib / Booksy / OpenTable URL into a bookable SMB.
  Built-in TCPA/GDPR/CASL compliance.
```

## PR title

```
Add agent-broker (agent-to-business horizontal layer, 13 tools)
```

## PR body

```
Adding agent-broker — horizontal MCP server for agent-to-business actions.
Most existing servers are single-vertical (Cal.com, Twilio, etc.); this one
stitches them together with compliance + channel fallback.

- 13 tools including import_booking_url (the differentiator)
- Worldwide coverage (22 jurisdictions with native compliance rules)
- Free tier: 100 ops/month
- Edge: Cloudflare Worker (40–70 ms globally)
- Discovery: MCP + OpenAI tools + Anthropic tools + A2A + llms.txt

MCP endpoint: <https://agent-broker-edge.basil-agent.workers.dev/mcp>
Repo: <https://github.com/basilalshukaili/agentbroker>
```
