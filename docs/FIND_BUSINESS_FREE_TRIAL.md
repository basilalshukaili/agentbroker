# find_business free trial - what it does and how to roll it out

Founder scope (chat 4, row 3070, "Yes proceed" to the proposal in row 3068):
*make find_business a zero-friction free trial (no signup, 10 free calls) so any
Smithery visitor can test it immediately.* Nothing wider: no other tool's price
or access changes.

## Behaviour

| Caller | find_business |
|---|---|
| Presents a valid key (`X-Agent-Identity`, `Authorization: Bearer`, or `x-api-key`) | Free, unlimited, never counted. Unchanged. |
| No valid key | The first 10 SUCCESSFUL calls per caller are served. Call 11 returns a normal MCP tool result (`isError: true`, `error_code: auth_required`, `reason_code: free_trial_exhausted`) that says how to get a key. |
| No valid key, service-wide ceiling reached | `rate_limited`, `free_trial_daily_capacity`, `retry_after_ms` to 00:00 UTC, plus how to get a key. |
| No valid key, counter unreachable | Fails CLOSED: tool not run, `free_trial_unavailable`, how to get a key. |

Discovery (`initialize`, `tools/list`, `resources/*`, `prompts/*`, `ping`) is never
counted. Both doors are gated: MCP `tools/call` (on `/mcp` and every `/mcp/<door>`)
and `POST /ops/find_business`.

The number of keyless calls is `core/tool_auth.TRIAL_CALLS_PER_CALLER` and nowhere
else; every sentence that promises it (tool description, `/.well-known/mcp.json`,
initialize instructions, registry catalogues, README, llms-install.md, pages) is
derived from or tested against it. It is deliberately not an environment variable.

## Who is "a caller"

The client IP as Caddy saw it, hashed with an HMAC keyed from a secret the
container already holds. IPv6 is grouped by /64. Forwarding headers are believed
only when the TCP peer is our own proxy (main.py stamps the peer address; a client
cannot pre-fill it), and the rightmost non-proxy hop is used, never the leftmost.
The User-Agent is NOT consulted, so a crawler-looking User-Agent is not a way
around the limit.

## Rollout order (the order matters)

1. **Apply `sql/agentbroker/008_anon_trial_reserve_release_rpc.sql`** in the hatchloop
   workspace (`python scripts/apply_sql.py sql/agentbroker/008_...sql`, one
   transaction, idempotent). It adds two functions and touches no table grant.
   Until it is applied the trial fails closed: every keyless find_business call
   answers "get a key".
2. Run `tests/integration/test_anon_trial_rpc_live.py` with `SUPABASE_URL` and
   `SUPABASE_ANON_KEY` in the environment. It uses a private probe tool name and
   leaves its rows at zero.
3. Only then deploy with `ops/vps/deploy_agentbroker_vps.py <sha>` (rollback-first).
4. For per-visitor counting on the canonical URL `hatchloop.dev/mcp/agent-broker`,
   also make the box's own address a trusted proxy in Caddy and list it in
   `ANON_TRIAL_TRUSTED_PROXIES` (see below). Without this every visitor on that URL
   shares one allowance; `api.hatchloop.dev/mcp` is not affected.

## Environment (all optional, all have working defaults)

| Variable | Default | Meaning |
|---|---|---|
| `FIND_BUSINESS_TRIAL_GLOBAL_DAILY` | `1000` | Service-wide anonymous find_business calls per UTC day. `0` switches the keyless trial off. |
| `ANON_TRIAL_TRUSTED_PROXIES` | `127.0.0.1` | Extra proxy addresses/networks (comma separated) whose `X-Forwarded-For` entries are ours. |

## The second proxy hop

`hatchloop.dev/mcp/agent-broker` is a Next.js rewrite on the same box that calls
`https://api.hatchloop.dev/mcp` server-side. Caddy sees that call arrive from the
box's own public address, does not trust it, and overwrites `X-Forwarded-For`, so
the visitor's address is gone before the container sees it. Fixing it is two
operator steps outside this repository: tell Caddy to trust the box's own address
(global `servers { trusted_proxies static <box ip> }`; check the syntax for the
installed Caddy) and list that address in `ANON_TRIAL_TRUSTED_PROXIES`. The same
limitation already applies to the anonymous premium-data quota.

## Rollback

`python ops/vps/deploy_agentbroker_vps.py --rollback-only` restores the previous
image. The counter rows left in `anon_data_quota` (`trial:c:*`, `trial:g:*`) are
inert without the code and can stay.

## Known limits

* Two people behind one NAT share an allowance.
* Someone with many real IP addresses gets many allowances; the global daily
  ceiling is what bounds that.
* A registry or gateway that relays visitors' calls from its own addresses makes
  those visitors one caller.
* Lifetime counter rows accumulate (one per keyless caller); there is no age column
  to prune on.
