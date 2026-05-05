# Agent Broker — Edge Worker

Cloudflare Worker that fronts the Render-hosted Python service. All discovery
and MCP read methods are served from **embedded snapshots** baked into the worker
bundle — no origin contact, always sub-70 ms, globally. Tool execution is proxied
to origin with cold-start retry.

## Architecture

```
agent  →  agent-broker-edge.basil-agent.workers.dev  (Cloudflare Worker, 300+ PoPs)
            ├── Discovery endpoints          → embedded snapshots    40–70 ms
            │   /.well-known/*, /manifest*, /llms.txt, /openapi.yaml
            │   /supply/platforms, /compliance/jurisdictions
            ├── MCP read methods (POST /mcp) → embedded snapshots    40–65 ms
            │   initialize, tools/list, ping, prompts/list, resources/list
            ├── MCP tools/call (POST /mcp)   → proxy to origin      170–190 ms
            ├── /ops/*, /supply/import_*     → proxy to origin
            ├── /edge/*                      → handled in worker, never proxied
            └── /api/metrics                 → KV cache (60 s) then origin

                          ↓  (state-changing only)
                smb-broker.onrender.com  (Render, Python FastAPI)
                    cron */2  keeps Render warm → tools/call stays at ~185 ms
```

**Verified timings (2026-05-05):**

| Path | Latency | Source |
|------|---------|--------|
| `/edge/health` | 47 ms | worker-internal |
| All discovery endpoints | 40–70 ms | embedded snapshot |
| POST `/mcp` — `initialize`, `tools/list`, `ping` | 40–65 ms | embedded snapshot |
| POST `/mcp` — `tools/call` | 170–190 ms | proxied to origin |
| POST `/ops/preview_cost` | 175–200 ms | proxied to origin |
| First isolate hit after new deploy | 1–4 s | one-time URL-rewrite cost; subsequent requests warm |

## Why this exists

Render free dyno sleeps after 15 min idle (~30 s cold start). The edge eliminates
that problem without paying for an always-on dyno:

- All discovery payloads are baked into the worker bundle at deploy time and served
  directly from the edge, anywhere on Earth, regardless of Render state.
- MCP read methods (`initialize`, `tools/list`, `ping`) are also edge-served — an
  agent connecting for the first time gets a 50 ms tool surface before any booking.
- `tools/call` and all state-changing operations proxy to origin. A cron job pings
  origin every 2 minutes so the dyno never sleeps; proxied calls land at ~185 ms.
- If Render dies permanently, discovery and MCP negotiation still work indefinitely
  from the embedded bundle — the worker outlives the origin.

## Files

| File | Purpose |
|------|---------|
| `wrangler.toml` | Worker name, KV binding, env vars, cron `*/2 * * * *` |
| `src/index.ts` | Main router: `/edge/*` internal, `/mcp` → mcp-edge, `/health`, `/api/metrics`, discovery catch-all, cron handler |
| `src/mcp-edge.ts` | MCP JSON-RPC dispatcher: edge-serves read methods, proxies `tools/call` to origin |
| `src/discovery.ts` | 14 discovery handlers: KV-live overlay over embedded snapshots |
| `src/proxy.ts` | Smart proxy with cold-start retry (502/503/504 · 2 attempts · 1500 ms delay · 45 s timeout) |
| `src/snapshots/index.ts` | Imports all snapshot files, rewrites origin URLs to edge URL, memoizes per base URL |
| `src/snapshots/*.json` | Embedded JSON snapshots of every discovery payload |
| `src/snapshots/*.txt`, `*.yaml` | Embedded text/YAML snapshots (llms.txt, llms-full.txt, openapi.yaml) |
| `src/snapshots/modules.d.ts` | TypeScript declarations for Wrangler `[[rules]] type=Text` imports |

## Deploy

```sh
export CLOUDFLARE_API_TOKEN=<full-access-token>
export CLOUDFLARE_ACCOUNT_ID=4996b77206796d62ffa870cdd6e5c4f5

cd service-root/edge
npm install
npx wrangler deploy
```

Wrangler reads `wrangler.toml`, bundles the TypeScript + all snapshots (~313 KiB),
and uploads to Cloudflare. KV namespace and cron trigger are already declared in the
config — no extra steps needed.

## Verify

```sh
URL=https://agent-broker-edge.basil-agent.workers.dev

# Edge alive — never touches origin
curl -s "$URL/edge/health"

# Origin probe through edge (shows origin latency)
curl -s "$URL/edge/info" | jq .

# Discovery — x-edge-source: embedded (or kv-live after cron runs)
curl -s "$URL/manifest" -D - 2>&1 | grep -i x-edge

# MCP tools/list — 13 tools in ~50 ms
curl -s -X POST "$URL/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# → 13

# tools/call — proxied to origin, ~185 ms
curl -s -X POST "$URL/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"preview_cost","arguments":{"operation":"schedule_appointment"}}}' | jq .
```

## Cloudflare resources

| Resource | Value |
|----------|-------|
| Account ID | `4996b77206796d62ffa870cdd6e5c4f5` |
| Worker name | `agent-broker-edge` |
| Worker URL | `https://agent-broker-edge.basil-agent.workers.dev` |
| KV namespace | `agent-broker-cache` (id `f45691e20cdd4937ae88ccb64159d928`) |
| Cron | `*/2 * * * *` — pings origin `/health` + refreshes 10 KV discovery paths |
| Bundle size | ~313 KiB (750 LOC TypeScript + 240 KB embedded snapshots) |

## Free-tier capacity

| Resource | Free / day | Per agent call |
|----------|-----------|----------------|
| Worker requests | 100,000 | 1 |
| KV reads | 100,000 | ~2 (cache lookup) |
| KV writes | 1,000 | ~0.05 (cron refresh) |

At 100k agent calls/day the service stays within the Cloudflare free tier. Beyond
that, Workers Paid at $5/mo covers 10M req/mo.

## Adding new endpoints

- **New cacheable read endpoint** (stable, public output): add a handler in
  `src/discovery.ts` and list it in `DISCOVERY_HANDLERS`. Add the path to
  `REFRESH_TARGETS` in `src/index.ts` so cron keeps KV warm.
- **New state-changing endpoint** (POST, writes data): the wildcard proxy in
  `src/index.ts` already forwards it to origin. No edge config needed.
- **New env-var-dependent payload**: the worker just proxies — no snapshot needed.

## Phase 2 trigger conditions

- `total_agents_requested > 5,000/month` sustained → port hot tool handlers to
  Workers + D1; decommission Render.
- Render free tier policy change that breaks service → migrate everything to Workers.

## Security

- KV namespace contains only public discovery payloads — no secrets, no PII.
- `X-Agent-Identity` and all other auth headers are forwarded to origin unchanged;
  auth enforcement is the origin's responsibility.
- **Rotate the Cloudflare API token** after any session where it was used in
  plaintext: <https://dash.cloudflare.com/profile/api-tokens>
