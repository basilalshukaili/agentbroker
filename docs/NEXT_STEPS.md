# Next Steps — Agent Broker

> **Last updated: 2026-05-05**

---

## Current state (facts, not assumptions)

| Item | Status |
|------|--------|
| Python service (Render) | ✅ Live — `hatchloop.dev` (origin, internal) |
| Cloudflare Worker edge | ✅ Live — `hatchloop.dev` |
| Tests | ✅ 103/103 passing in 0.40 s |
| MCP tools | ✅ 21 tools, all callable |
| Discovery endpoints | ✅ 40–70 ms globally (embedded snapshots) |
| Compliance gate | ✅ 22 jurisdictions, non-bypassable |
| Fortnightly metrics reminder | ✅ Telegram bot, every 14 days |
| Smithery listing | ✅ Updated — edge URL, 21 tools |
| mcp.so listing | ✅ Submitted |
| Official MCP Registry | ✅ Published — `io.github.basilalshukaili/agent-broker` v1.0.1 |
| PulseMCP | ⏳ Pulls from Official MCP Registry — live within ~1 week |
| mcphub.io | ⏳ GitHub issue #15 open — maintainer review pending |
| Composio | ⏳ Discord message sent — response pending |
| modelcontextprotocol/servers | ⏳ [PR #4108](https://github.com/modelcontextprotocol/servers/pull/4108) open |
| awesome-mcp-servers | ⏳ [PR #5890](https://github.com/punkpeye/awesome-mcp-servers/pull/5890) open |
| APIs-guru openapi-directory | ⏳ [PR #2465](https://github.com/APIs-guru/openapi-directory/pull/2465) open |
| GitHub topics | ✅ 11 topics set (`mcp`, `mcp-server`, `ai-agents`, `compliance`, …) |
| **Real agent traffic** | ❌ `total_agents_requested = 0` |

**Distribution is done. The bottleneck now is waiting for traffic.**

---

## The one optional setup worth doing now (5 min)

**UptimeRobot free monitor** — passive, runs itself:

1. Go to <https://uptimerobot.com> → create free account
2. Add HTTP monitor:
   - URL: `https://hatchloop.dev/edge/health`
   - Interval: 5 minutes
   - Alert email: your address

This gives you instant notification if the worker ever goes down, and provides a public
status page URL you can reference. That's the only remaining setup item.

---

## Wait for traffic — honestly

Everything that can be submitted has been submitted. The pending PRs and listings will
resolve themselves; none require follow-up action from you unless a maintainer asks a
question.

**What the waiting period looks like:**

- PulseMCP picks up the Official MCP Registry entry within a week — no action needed
- mcphub.io, modelcontextprotocol/servers, awesome-mcp-servers PRs merge on maintainer
  schedule (days to weeks) — no action needed
- Composio may reply on Discord or may not — check back in 2 weeks if no response

**Check `total_agents_requested` every 2 weeks** (Telegram reminder is already set):

```bash
curl -s https://hatchloop.dev/api/metrics | python -m json.tool
```

---

## What changes at `total_agents_requested > 0`

The first non-zero reading means a real agent found and called the server. At that point:

1. Look at which tool was called — that tells you what the agent needed
2. Check if it succeeded or failed — that tells you what to fix first
3. If it's a paid agent framework (e.g., Cursor, Windsurf), check if they have a "popular
   servers" list you can get on

Nothing else changes operationally. The system handles itself.

---

## What changes at `total_agents_requested > 100` (sustained)

This is the threshold for re-engaging on engineering:

| Item | Action |
|------|--------|
| Postgres / Redis persistence | Migrate in-memory stores (Neon free tier, ~30 min) |
| Real JWT (RS256) | Rotate to managed KMS key |
| Multi-region Render deploy | Only if p99 latency complaints from non-EU agents |
| Custom domain | Buy `agentbroker.io` (~$30/yr) at Cloudflare Registrar |
| Paid billing activation | Complete Paddle vendor verification |
| SOC 2 audit | Only when first enterprise customer requests it |

---

## What changes at `total_agents_requested > 5,000/month` (sustained)

Phase 2:

1. Port hot tool handlers from Python to TypeScript Workers + D1 (eliminate Render)
2. Activate Paddle billing
3. Apply for inclusion in Anthropic / Cursor / Continue default MCP server lists
4. Add more SMB verticals (healthcare, legal, automotive)

---

## Files to know

| File | Purpose |
|------|---------|
| `server.json` | Official MCP Registry manifest — edit version and re-run `mcp-publisher publish` for any updates |
| `edge/README.md` | Edge architecture, deploy instructions |
| `EXPIRY_CHECKLIST.md` | Free-tier service limits and what to do when each runs out |
| `docs/SECURITY.md` | Gate between v0.1 and v1.0 production |

---

## The single metric that decides everything

**`total_agents_requested` in `/api/metrics`**

Zero today. When it goes non-zero, the project has entered a different phase.
Until then, the system runs itself — no maintenance required.
