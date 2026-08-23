# Installing AgentBroker in Cline

AgentBroker is a **remote streamable-HTTP MCP server** — there is nothing to build,
clone, or run locally. Setup is a single URL entry.

## Add the server

Add this to your Cline MCP settings (`cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker"
    }
  }
}
```

That is the whole install. On connect, the server exposes **19 tools**. **11 read tools
are usable immediately with no key** (e.g. `find_business`, `verify_business`,
`verify_company_record` — live GLEIF/SEC company data, `screen_sanctions` — live
OFAC/EU/UN sanctions screening, `map_trade_restriction` — cross-border embargo/export-control
mapping, `check_compliance`, `self_test`).

## Optional: unlock the write tools (free)

The 8 write tools (send a message, book an appointment, etc.) need a free,
email-verified key. Get one:

```bash
curl -X POST https://hatchloop.dev/mcp/agent-broker/keys/request \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Click the verification link in the email, then add the key as a header:

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": { "X-Agent-Identity": "YOUR_KEY_HERE" }
    }
  }
}
```

Free tier: 50 write operations/day. Flat $9 / 90 days for unlimited.

## Verify it is working

Call `self_test` (free) — it returns `all_passed: true` when the server is healthy.
The endpoint is always-on (Cloudflare edge in front of the origin).
