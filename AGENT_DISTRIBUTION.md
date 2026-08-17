> **⚠️ HISTORICAL — NOT CURRENT STATE (stamped 2026-08-17).**
> This file is a point-in-time snapshot kept for history. Claims about deploys,
> pricing, endpoints, and status here may be stale or contradicted by later work.
> The authoritative current picture is **AUDIT-2026-08-16.md** plus `git log`.

# Agent-First Distribution

> Customer = AI agent. Not developers, not humans, not Twitter.
> Delete every channel that targets a human; keep only what agents read.

---

## The two decision points where agents pick a tool

1. **Aggregator search.** A meta-agent calls Smithery / Glama / Composio /
   mcp.so / Pulse MCP / Anthropic-internal-search and asks "give me an MCP
   that does X". The aggregator returns ranked candidates from a metadata
   index. We need to rank well there.
2. **`tools/list` shortlist.** Once installed, the agent sees ~20-100 tools
   from all servers it has connected. When a user query arrives, the LLM
   picks ONE tool by reading descriptions. We need to be the obvious pick
   for our user queries.

Every distribution dollar is spent on one of those two points. **Anything
else (demos, newsletters, Reddit) is wasted unless it converts to an
aggregator submission or improves a description.**

---

## What we just shipped (commit `e8208e2`)

### Critical fix #1 — exposed `import_booking_url` as the 13th MCP tool

It was a REST endpoint only. Agents calling `tools/list` literally couldn't
see our differentiating feature. Fixing this alone is probably worth more
than every email and registry submission combined, because the LLM-pickable
description for the URL→booking flow is now in front of every agent that
connects.

### Critical fix #2 — wired 5 missing dispatcher cases

`send_message`, `send_transactional_confirmation`, `handle_inbound`,
`escalate_to_human`, and `import_booking_url` were advertised in `tools/list`
but the MCP dispatcher had no case for them. Agents trying to call them got
"registered but not yet routed" errors. **Now all 13 tools are callable.**

### Critical fix #3 — added user-query examples to every description

LLMs are massively few-shot driven. When an LLM gets a user query like
"book me a haircut at https://cal.com/jane-salon" it scans the available
tools' descriptions for similar-shaped phrases. Adding 2-4 example user
queries per tool dramatically raises the LLM's hit rate. We now have:

```
EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Book me a haircut at https://cal.com/jane-salon"
  -> call import_booking_url({"booking_url": "https://cal.com/jane-salon", ...})
  -> then schedule_appointment({"smb_id": "<from_above>", "action": "book"})
```

…on every tool. No more guessing what we're for.

### Critical fix #4 — `prompts/list` rewritten to be agent-pickable

The old prompts/list had three abstract entries ("book_appointment_workflow"
etc) that didn't match user intent. Replaced with four agent-pickable ones:

- `book_from_any_url` (PRIMARY — the killer flow)
- `find_then_book` (no-URL path)
- `compliant_outbound_message` (gate-aware send)
- `cost_estimation` (free preview)

`prompts/get` now returns a literal step-by-step plan agents can execute
verbatim. e.g. for `book_from_any_url`:

> Step 1: call import_booking_url with booking_url=<url>.
> Step 2: take the returned smb_id and call schedule_appointment...
> Step 3: optionally call send_transactional_confirmation...

### Critical fix #5 — `resources/list` includes a cookbook + platform catalogue

Two new resources:
- `agent-broker://booking_platforms` — the 12 supported platforms with regex
  + example URLs, so an agent knows exactly which URL formats are acceptable.
- `agent-broker://cookbook` — markdown with multi-tool flows so an agent
  reading our resources can plan multi-step workflows correctly.

---

## Where agents actually find MCP servers — the agent-aggregator map

Submitted = ✅. Pending = ⏳. Cannot-automate = 👆 (you click once).

| Aggregator | Status | How agents query it |
|---|---|---|
| **Smithery** | ✅ live, listing republished with 13 tools | REST API: `GET https://smithery.ai/api/v1/servers?q=<query>` returns ranked MCPs |
| **Glama** | ⏳ indexes from GitHub topic in 1-3 days | Their search UI + API consume the GitHub `mcp-server` topic crawl |
| **MCP servers official catalog** (`modelcontextprotocol/servers`) | ⏳ PR #4077 open | Built into Claude Desktop's "Add MCP" picker |
| **awesome-mcp-servers** (curated list) | ⏳ PR #5626 open | Read by every list-aggregator |
| **mcp.so** | 👆 needs human submit | https://mcp.so/submit — agents query their JSON index |
| **Pulse MCP** | 👆 needs human submit | https://www.pulsemcp.com/submit — listed in their crawl |
| **mcphub.io** | 👆 needs human submit | already in OUTREACH_KIT.md |
| **Composio** | 👆 needs human submit (developer-tier) | Major LangChain / CrewAI tool aggregator |
| **apis.guru** | ⏳ PR #2465 open | OpenAPI catalog read by RapidAPI + several agent frameworks |
| **Anthropic's internal MCP discovery** (when it launches) | indirect | Will crawl `/.well-known/mcp.json` automatically |

For the four `👆` items, see **STEP 1** below — exact form fields are
copy-paste ready.

---

## STEP 1 — submit to the four remaining aggregators (≈ 12 minutes)

Same descriptions, four different forms.

### 1.1 mcp.so

URL: <https://mcp.so/submit>

| Field | Value |
|---|---|
| MCP server name | `Agent Broker` |
| GitHub URL | `https://github.com/basilalshukaili/agentbroker` |
| Endpoint URL | `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| Description (short) | `13 MCP tools to find, message, and book appointments at small businesses worldwide. Turns any Cal.com / Calendly / Doctolib / Booksy / OpenTable / Setmore / Square / Acuity / Schedulista / Squarespace / BookMyCity URL into a Claude-bookable smb_id.` |
| Tags | `mcp, mcp-server, scheduling, business, compliance, cal-com, calendly, doctolib` |
| Author email | `basilalshukaili@gmail.com` |

### 1.2 Pulse MCP

URL: <https://www.pulsemcp.com/submit>

Same fields. They additionally ask for:

| Extra field | Value |
|---|---|
| Tool count | `13` |
| License | `Proprietary` |
| Self-hosted? | `No (hosted on Render)` |
| Auth model | `Optional X-Agent-Identity bearer token; read-only ops require no auth` |

### 1.3 mcphub.io

URL: <https://mcphub.io/submit>

| Field | Value |
|---|---|
| Server name | `Agent Broker` |
| MCP endpoint URL | `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| Repository | `https://github.com/basilalshukaili/agentbroker` |
| Smithery URL | `https://smithery.ai/server/lordbasil147/agent-broker` |
| Discovery URL (`mcp.json`) | `https://agent-broker-edge.basil-agent.workers.dev/.well-known/mcp.json` |
| Transport | `Streamable HTTP` |
| Tool count | `13` |
| Description | (same as mcp.so) |

### 1.4 Composio

URL: <https://composio.dev/> → "Submit a tool" link in the footer (or
ping their Discord — they prefer Discord for new MCP submissions).

| Field | Value |
|---|---|
| Tool name | `agent-broker` |
| Display name | `Agent Broker` |
| MCP endpoint | `https://agent-broker-edge.basil-agent.workers.dev/mcp` |
| Categories | `Scheduling, Communication, Business Operations` |
| Description | (same) |
| Auth required | `No (free tier 100 ops/month)` |

These four submissions take **3 minutes each**. They're pure aggregator
plumbing — no demo video, no email, no social, just structured metadata
that machines consume.

---

## STEP 2 — what an agent actually sees when it picks us (proof)

Run this from any terminal to confirm the agent-facing surface is doing
its job:

```bash
# 1. List all 13 tools (agents call this on connect)
curl -s -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m json.tool | head -40

# 2. Read the prompts library (high-value workflow templates)
curl -s -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"prompts/list","params":{}}' | python -m json.tool

# 3. Get a literal step-by-step plan for the killer flow
curl -s -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"prompts/get","params":{"name":"book_from_any_url","arguments":{"booking_url":"https://cal.com/jane"}}}' | python -m json.tool

# 4. Read the tool-chain cookbook resource
curl -s -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"resources/read","params":{"uri":"agent-broker://cookbook"}}' | python -m json.tool

# 5. Try the killer call live (no auth)
curl -s -X POST https://agent-broker-edge.basil-agent.workers.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"import_booking_url","arguments":{"booking_url":"https://cal.com/peer","vertical":"professional_services"}}}' | python -m json.tool
```

The agent's experience now: **`tools/list` → matches user query against
example queries → picks `import_booking_url` → calls it → gets smb_id back
in ≤ 1 s → calls `schedule_appointment` → done.** Three round-trips.

---

## STEP 3 — what we are NOT doing anymore

Following your reframe ("audience is the AI agent, not humans"):

| Channel | Verdict |
|---|---|
| Demo video on YouTube | ❌ skip — humans watch videos, agents don't |
| Reddit / HN posts | ❌ skip — same |
| Cold emails to writers (Simon Willison etc) | ❌ skip — they reach humans, who reach agents indirectly. Too lossy. |
| Twitter / X account | ❌ skip |
| Product Hunt launch | ❌ skip |
| Blog posts on dev.to | ❌ skip |
| Newsletter sponsorships | ❌ skip |
| AI directories (theresanaiforthat, futuretools) | ❌ skip — those are human-curated; agents don't query them |

We **keep**:

| Channel | Why kept |
|---|---|
| Smithery / Glama / mcp.so / Pulse MCP / mcphub.io / Composio | agents query these directly |
| modelcontextprotocol/servers + awesome-mcp-servers PRs | agent-discovery feeds |
| GitHub topic `mcp-server` | crawled by agent aggregators |
| `/.well-known/*` + `/llms.txt` | crawled by Anthropic / OpenAI agent crawlers |
| `/openapi.yaml` + apis.guru PR | RapidAPI + agent framework crawlers |
| `/compliance/check` standalone API | indexed by SEO bots that other agents read |

Already-sent emails (Anthropic, Cursor, Continue, Perplexity, You.com)
stay sent — they cost nothing now and may surface in their internal
agent-discovery indexes. **No new emails to humans.**

---

## STEP 4 — measure success the right way

The single signal that matters is `total_agents_requested` in
`/api/metrics`. But because agents identify themselves with
`X-Agent-Identity` (or anonymously), we can also break it down by:

```bash
# Count distinct agents calling us (each Identity is one)
curl -s https://agent-broker-edge.basil-agent.workers.dev/api/metrics
```

We don't yet expose distinct-agent count or per-tool counts. **If you
ever want sharper telemetry, that's the only feature worth adding next.**
Until then, "agents requested per week" is the proxy.

---

## STEP 5 — the agent-first leverage check (do once a month)

Open `tools/list` from the terminal above. **Read every description as
if you were an LLM. Ask:**

> "If a user said 'Book me a salon appointment in Tokyo', would I pick
> THIS tool, or another one in my library?"

If the answer isn't immediate "yes — find_business or import_booking_url
matches this user query exactly", the description is wrong. Edit
`manifest/manifest.json`'s `user_query_examples`, push, redeploy.

**This is the only ongoing maintenance task that materially raises
selection rate.** Everything else is plumbing.

---

## TL;DR

The work we did today materially raised the probability of agent
selection by exposing `import_booking_url` (the differentiator), wiring 5
missing dispatcher cases (so advertised tools actually work), and
rewriting every tool description with user-query examples that an LLM
matches against directly.

**You do four 3-minute aggregator submissions. Then walk away.** Every
agent that connects from now on sees a tool surface designed to be
LLM-picked.
