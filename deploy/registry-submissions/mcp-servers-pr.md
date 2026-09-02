# Official MCP Registry — NOT a PR to modelcontextprotocol/servers

## The mechanism this file originally described no longer exists

This draft used to target a PR against
`https://github.com/modelcontextprotocol/servers` README.md's "Community
Servers" section. Checked against the live repo (2026-09-02): that repository
no longer lists community servers at all — its README now reads "If you are
looking for a list of MCP servers, you can browse published servers on the
[MCP Registry](https://registry.modelcontextprotocol.io/)." It ships only the
reference implementations the steering group maintains. There is no README
section to send a PR against any more.

## What actually needs to happen instead — and it already has

Agent Broker is **already published** in the official MCP Registry, and it
gets there automatically, not through a manual submission:

- `.github/workflows/publish-mcp.yml` runs on every push to `main` that
  touches `server.json` or `registry/*/server.json`, installs
  `mcp-publisher`, validates every manifest, authenticates via DNS
  (`hatchloop.dev` is DNS-verified; the Ed25519 signing key lives in the
  `MCP_REGISTRY_DNS_KEY` repo secret), and publishes.
- The published name is `dev.hatchloop/agent-broker` (DNS auth, not the
  `io.github.<owner>/*` namespace GitHub OIDC would give — that's why DNS
  auth was chosen). `server.json` (repo root) carries `version: "0.2.12"` and
  the canonical remote `https://hatchloop.dev/mcp/agent-broker`.
- The five narrow capability doors (`compliance-check`, `company-verification`,
  `sanctions-screening`, `appointment-booking`, `sms-whatsapp-messaging`) are
  published the same way from `registry/<name>/server.json`, each pointing at
  its own endpoint under `https://hatchloop.dev/mcp/<name>`.
- The workflow is idempotent: an already-published version is logged and
  skipped, not treated as a failure, so re-running it after an unrelated
  change is safe.

**So there is nothing to draft or submit here.** The registry entry updates
itself the next time `server.json`'s `version` field is bumped and pushed to
`main`. What *would* need a human:

- Confirming the last publish actually succeeded: check the
  "Publish to MCP Registry" workflow runs at
  `https://github.com/basilalshukaili/agentbroker/actions/workflows/publish-mcp.yml`.
- Re-verifying the entry is live and current by querying the registry API
  directly, e.g.
  `curl https://registry.modelcontextprotocol.io/v0/servers?search=agent-broker`
  (exact query path may drift — check `registry.modelcontextprotocol.io`'s own
  API docs first) and confirming the returned `version` matches the repo's
  `server.json`.
- If a future registry redesign reintroduces a directory listing or
  "featured" tier with its own submission form, redo this file against that
  mechanism specifically — don't assume today's automation still covers it.

## Content to reuse if a manual listing description is ever needed again

If some other MCP directory (not this one) asks for a hand-written blurb in
this style, here is current, code-verified copy:

```
Agent Broker — horizontal agent-to-business layer with verifiable compliance.
screen_sanctions and check_compliance return an Ed25519-signed compliance
receipt, verifiable offline against the public key at hatchloop.dev/agents.md.
Live lookups against official sources: OFAC SDN, the EU Consolidated list,
the UK Sanctions List, GLEIF LEI, SEC EDGAR, USASpending.gov federal contract
awards. Also finds, verifies, messages, and schedules with small/mid-sized
businesses, gated by a non-bypassable TCPA/GDPR/CASL/PDPL compliance check
across 26 jurisdictions.

23 tools. 15 need no key (12 always-free + 3 free within a daily quota). The
8 write tools need a free email-verified key (100 write ops/day) or credits/x402.
License: MIT.

MCP endpoint: https://hatchloop.dev/mcp/agent-broker
Repo: https://github.com/basilalshukaili/agentbroker
```
