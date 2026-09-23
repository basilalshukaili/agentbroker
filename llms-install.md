# Connect an MCP client to AgentBroker

AgentBroker is a **remote streamable-HTTP MCP server**  -  there is nothing to build,
clone, or run locally. Setup is one URL:

```
https://hatchloop.dev/mcp/agent-broker
```

Add that URL to any client below. On connect, the server exposes **23 tools** - see
"What you get once connected" further down for the breakdown, and "Optional: unlock
the write tools" for the key some of them need.

## Pick your client

- Claude Code (CLI)
- Claude Desktop
- Cursor
- VS Code
- Cline

### Claude Code (CLI)

```bash
claude mcp add --transport http agent-broker https://hatchloop.dev/mcp/agent-broker
```

To also send your AgentBroker key (see "Optional: unlock the write tools" below) so
the key-gated tools work:

```bash
claude mcp add --transport http agent-broker https://hatchloop.dev/mcp/agent-broker \
  --header "X-Agent-Identity: YOUR_KEY_HERE"
```

Run `claude mcp list` to confirm - `agent-broker` should show as "Connected".

### Claude Desktop

1. Open **Settings -> Customize -> Connectors**.
2. Click **"+"**, then **"Add custom connector."**
3. Paste the server URL: `https://hatchloop.dev/mcp/agent-broker`.
4. Click **"Add."**

(Team and Enterprise plans: an Owner adds it once, under **Organization settings ->
Connectors -> Add -> Custom -> Web**; members then connect it individually from
**Customize -> Connectors**.)

This gets you the 11 free tools immediately, no key needed. Claude Desktop's custom
connector dialog only offers an OAuth Client ID/Secret under "Advanced settings," not
a plain header field, so there is currently no way to attach an AgentBroker key to a
Claude Desktop connector. Use Claude Code, Cursor, VS Code, or Cline below if you need
the key-gated tools.

### Cursor

Add to `.cursor/mcp.json` (project-scoped) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker"
    }
  }
}
```

With your AgentBroker key:

```json
{
  "mcpServers": {
    "agent-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": {
        "X-Agent-Identity": "YOUR_KEY_HERE"
      }
    }
  }
}
```

### VS Code

Add to `.vscode/mcp.json` (workspace) or your user-profile `mcp.json` (open it with
**MCP: Open User Configuration** from the Command Palette):

```json
{
  "servers": {
    "agent-broker": {
      "type": "http",
      "url": "https://hatchloop.dev/mcp/agent-broker"
    }
  }
}
```

With your AgentBroker key, kept out of the file itself via an input variable:

```json
{
  "servers": {
    "agent-broker": {
      "type": "http",
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": { "X-Agent-Identity": "${input:agent-broker-key}" }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "agent-broker-key",
      "description": "AgentBroker key (from /keys/request)",
      "password": true
    }
  ]
}
```

VS Code prompts for the value the first time the server starts and stores it securely.

### Cline

Edit `~/.cline/mcp.json` (CLI), or in the Cline panel open the MCP Servers icon ->
**Configure** tab -> **Configure MCP Servers** to edit the same JSON (or use the
**Remote Servers** tab to add it without hand-editing JSON). Add under `mcpServers`:

```json
{
  "mcpServers": {
    "agent-broker": {
      "type": "streamableHttp",
      "url": "https://hatchloop.dev/mcp/agent-broker"
    }
  }
}
```

The `type` field matters: Cline treats a remote server with no `type` as the legacy
SSE transport, not streamable HTTP. With your AgentBroker key:

```json
{
  "mcpServers": {
    "agent-broker": {
      "type": "streamableHttp",
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": {
        "X-Agent-Identity": "YOUR_KEY_HERE"
      }
    }
  }
}
```

## What you get once connected

**11 utility tools are unconditionally free (no key, no limit):** `find_business`, `verify_business`,
`check_compliance`, `check_booking_link`, `preview_cost`, `get_status`, `get_outcome`, `self_test`,
`check_quota`, `mint_key`, and
`lookup_us_contracts` (US federal contract awards via USASpending.gov).

**`get_conversation` is free and unmetered but needs a key.** A message thread is readable only by
the agent identity that opened it, so send the same `X-Agent-Identity` key you send with
`send_message`; a call with no key is refused rather than answered.

**3 premium data tools are free up to a daily quota, then $0.02/call:** `verify_company_record`
(live GLEIF/SEC company data), `screen_sanctions` (live OFAC/EU/UK sanctions screening),
`map_trade_restriction` (cross-border embargo/export-control mapping). Anonymous callers get
100 calls/day; email-verified free keys get 500/day. Beyond the quota: top up credits by card
at https://hatchloop.dev/pricing, or pay per call in USDC on Base by attaching an x402 payment
in `params._meta["x402/payment"]` (no account needed).

## Optional: unlock the write tools (free)

The 8 write tools (send a message, book an appointment, etc.) need a free,
email-verified key. Get one:

```bash
curl -X POST https://hatchloop.dev/keys/request \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Click the verification link in the email, then add the key as a header using the
`headers` (or `--header`) example under your client above:
`X-Agent-Identity: YOUR_KEY_HERE`.

Free tier (write tools): 100 operations/day. Free tier (premium data tools): 500 calls/day.
Credit packages from $9/1,000 credits at https://hatchloop.dev/pricing.

## Verify it is working

Ask your client to call `self_test` (free)  -  it returns `all_passed: true` when the server is
healthy. The endpoint is always-on (Cloudflare edge in front of the origin).
