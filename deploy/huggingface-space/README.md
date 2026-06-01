---
title: Agent Broker
emoji: 🤖
colorFrom: green
colorTo: blue
sdk: docker
app_port: 8000
pinned: true
license: other
short_description: Horizontal MCP server — agent-to-business action layer.
---

# Agent Broker on Hugging Face Spaces

Live MCP endpoint: `https://basilalshukaili-agentbroker.hf.space/mcp`

12 tools for AI agents to find, verify, message, and schedule appointments
with small businesses worldwide. Free for any agent up to 100 ops/month.

- Manifest: `/manifest`
- llms.txt: `/llms.txt`
- OpenAPI: `/openapi.yaml`
- Health: `/health`

Source: <https://github.com/basilalshukaili/agentbroker>

## Setup notes (for the Space owner)

This Space is configured to use Docker (`sdk: docker` above). The `Dockerfile`
and source code at the root of this repository are used directly.

If you forked from GitHub, set the secrets in **Settings → Repository secrets**:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `CALCOM_API_KEY`
- `CALCOM_USERNAME`
- `VAPI_API_KEY`
- `RESEND_API_KEY`
- `PADDLE_API_KEY`
- `POLAR_API_KEY`
- `AGENT_IDENTITY_SIGNING_SECRET`
- `BILLING_RECEIPT_SIGNING_SECRET`

And the public env vars in **Settings → Repository variables**:

- `ENVIRONMENT=production`
- `PUBLIC_BASE_URL=https://basilalshukaili-agentbroker.hf.space`
- `BILLING_PROVIDER=paddle`
- `COMPLIANCE_DEFAULT_JURISDICTION=international`
- `SUPPLY_SEED_MODE=empty`
- `REQUIRE_AUTH=false`
