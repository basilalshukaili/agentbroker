# Outreach Kit — Agent Broker

> **Goal:** be visible everywhere an AI agent or its developer might look
> for a tool that talks to small businesses. None of the channels below
> require social media, design partners, or a HN account.
>
> Send all six emails on the same day. Submit all four GitHub PRs in the
> same hour. Then leave it alone for 30 days.

---

## Channel matrix — what's automated, what costs one click

| Channel | Status | What's needed from you |
|---|---|---|
| **Smithery** registry | **LIVE** ✅ pointing at smb-broker.onrender.com | nothing — listing at https://smithery.ai/server/lordbasil147/agent-broker |
| **GitHub topics** (11 set) | **LIVE** ✅ | nothing — Glama crawls within ~3 days |
| `modelcontextprotocol/servers` PR | forks ✅, PAT scope ❌ | extend the PAT (see STEP 1 below) and re-run script |
| `punkpeye/awesome-mcp-servers` PR | forks ✅, PAT scope ❌ | same — same script handles both |
| **Glama** | crawls GitHub topic — auto-indexed in 1-3 days | nothing |
| **MCP Hub** (mcphub.io) | crawls GitHub topic | nothing |
| **apis.guru** OpenAPI catalog | manual PR — see below | one PR (5 min) |
| **Cursor MCP catalog** | email submission | send the email below |
| **Continue.dev** plugin marketplace | GitHub PR | one PR (3 min) |
| **Cline (Claude Coder)** | GitHub PR | one PR (3 min) |
| **OpenAI plugin store** | dormant — see note | skip for now |
| **Anthropic dev relations** | email — low ROI but free | send the email |
| **Perplexity API hub** | email | send the email |
| **You.com agent index** | email | send the email |
| **AI Agents Directory** (theresanaiforthat-style sites) | manual submit | 4 forms × 5 min |

---

## STEP 1 — finish the two GitHub PRs (5 minutes total)

> ✅ The forks already exist — confirmed `basilalshukaili/servers` and
> `basilalshukaili/awesome-mcp-servers` are reachable via the API.
> Only thing blocking the PRs is **PAT scope**: your fine-grained
> token is scoped to `agentbroker` only, so it can't write to the
> two new fork repos. One 30-second fix:

**Option A — extend the existing PAT (recommended)**

1. Go to <https://github.com/settings/personal-access-tokens>
2. Click the `agentbroker` token to edit it.
3. **Repository access → Only select repositories** → add the two new
   forks:
   - `basilalshukaili/servers`
   - `basilalshukaili/awesome-mcp-servers`
4. **Repository permissions** → ensure these are **Read and write**:
   - `Contents`
   - `Pull requests`
5. Click **Update**.

Then re-run `python scripts/submit_to_registries.py` — it will splice
our entry into both upstream READMEs, commit on the `add-agent-broker`
branch in each fork, and open both PRs upstream programmatically.

**Option B — issue a classic PAT** (if Option A is fiddly)

1. <https://github.com/settings/tokens/new> → `Tokens (classic)` → `Generate new token`.
2. Scope: **`public_repo`** (only this one).
3. Paste the new token into `.env` over the existing `GITHUB_PAT=…` line.
4. Re-run the script.

Either way, both PRs typically merge within 5-10 days.

---

## STEP 2 — apis.guru OpenAPI catalogue (5 minutes, one-time)

apis.guru is the canonical catalogue of public OpenAPI specs. Tools like
RapidAPI, AI plugins, and several MCP-discovery crawlers consume their
JSON index. Submission flow:

1. Open <https://github.com/APIs-guru/openapi-directory> in a browser.
   Click **"Fork"** (top-right).
2. In your fork, navigate to `APIs/` and create the path:
   `APIs/smb-broker.onrender.com/1.0.0/openapi.yaml`.
3. Paste the contents of our live OpenAPI:
   `https://smb-broker.onrender.com/openapi.yaml` (curl it, save as that file).
4. Open `APIs/smb-broker.onrender.com/1.0.0/openapi.yaml` in your fork's web
   editor and ensure the top-level `info` block has `x-providerName:
   smb-broker.onrender.com` and `x-origin` referencing the URL.
5. Commit on a branch named `add-agentbroker`, then **"Compare & pull
   request"** against `APIs-guru/openapi-directory` `main`.
6. Their bot validates within ~10 minutes; a maintainer merges within a week.

After merge: anyone using <https://api.apis.guru> for tool discovery
sees Agent Broker. Several MCP clients use this index as a fallback.

---

## STEP 3 — emails to send (six of them, same day)

> Tone: terse, no marketing copy, lead with the URL. **Anyone who reads
> these has < 10 seconds.** First two lines must answer "what is it" and
> "how do I try it."

### 3.1 Anthropic (developer relations)

**To:** `developers@anthropic.com`, `mcp@anthropic.com`
**Subject:** `MCP server: 12-tool agent-to-business action layer (live, free tier)`

```
Hi team,

I built an MCP server that gives Claude 12 tools to find, verify,
message, and schedule appointments with small businesses worldwide.
Live, free for any agent up to 100 ops/month, full TCPA / GDPR / CASL
compliance gate built in.

Live MCP endpoint:    https://smb-broker.onrender.com/mcp
Repo (open code):     https://github.com/basilalshukaili/agentbroker
Smithery listing:     https://smithery.ai/server/lordbasil147/agent-broker
Anthropic-tools JSON: https://smb-broker.onrender.com/.well-known/anthropic-tools.json

Test it in 30 seconds — drop this into Claude Desktop's claude_desktop_config.json:

  {
    "mcpServers": {
      "agent-broker": {
        "url": "https://smb-broker.onrender.com/mcp"
      }
    }
  }

Happy to demo on a call or hand over a test agent identity.

— Basil Al Shukaili
   Sultanate of Oman
```

### 3.2 Cursor — MCP catalog

**To:** `hi@cursor.com`, `support@cursor.com`
**Subject:** `Submission: agent-broker MCP server for Cursor catalog`

```
Hi,

Submitting Agent Broker for the Cursor MCP catalog.

Endpoint:        https://smb-broker.onrender.com/mcp
Repo:            https://github.com/basilalshukaili/agentbroker
12 tools: find/verify/message/schedule across small businesses worldwide.
Free tier 100 ops/month. No auth required for read-only ops.

Connection JSON:
  {
    "mcpServers": {
      "agent-broker": {
        "url": "https://smb-broker.onrender.com/mcp"
      }
    }
  }

Smithery already lists us. Glama auto-indexes our `mcp-server` topic.
Adding to the Cursor catalog completes the major IDE clients.

— Basil
```

### 3.3 Continue.dev

**To:** `hello@continue.dev`
**Subject:** `MCP server submission: agent-broker (12 tools, free tier)`

Use the Cursor template above, change "Cursor" → "Continue".

### 3.4 Cline (Claude Coder)

**To:** open an issue at <https://github.com/cline/cline/issues>
**Title:** `Add Agent Broker to recommended MCP servers list`

```
Hi,

Submitting Agent Broker for the Cline recommended-MCP-servers list:

  Endpoint: https://smb-broker.onrender.com/mcp
  Repo:     https://github.com/basilalshukaili/agentbroker
  Tools:    12 (find_business, verify_business, send_message,
             capture_lead, schedule_appointment, send_transactional_confirmation,
             handle_inbound, escalate_to_human, get_status, get_outcome,
             preview_cost, self_test)
  Free tier: 100 ops/month, any agent, no card.

Gives Cline users the ability to actually book appointments and message
small businesses, not just simulate. Compliance gate is non-bypassable
(TCPA/GDPR/CASL across 22 jurisdictions + INTERNATIONAL fallback).

Happy to submit a PR if you'd prefer that over an issue.
```

### 3.5 Perplexity API hub

**To:** `api@perplexity.ai`
**Subject:** `Tool submission: agent-broker MCP for Perplexity Tools API`

```
Hi,

Submitting Agent Broker as a tool target for Perplexity's tool-use API.

Endpoint:               https://smb-broker.onrender.com/mcp
OpenAI-tools format:    https://smb-broker.onrender.com/.well-known/openai-tools.json
12 tools, free tier 100 ops/month, MoR billing via Paddle for paid traffic.

If Perplexity has a tools-marketplace, please point me to the submission
form. Otherwise treating this as a notice that we exist.

— Basil
```

### 3.6 You.com agent index

**To:** `partnerships@you.com`
**Subject:** `Agent Broker — MCP tool for you.com agents`

(Same template as Perplexity above with "You.com" substituted.)

---

## STEP 4 — passive discovery directories (each ~3 minutes)

These are forms, not emails. Submit once, leave for years.

| Directory | URL | Why |
|---|---|---|
| AIagents.directory | https://aiagents.directory/submit | indexed by Google for "AI agent for X" queries |
| theresanaiforthat.com | https://theresanaiforthat.com/submit | high-traffic AI catalogue |
| futuretools.io | https://futuretools.io/submit-a-tool | curated list, decent SEO |
| AI Tool Mall | https://aitoolmall.com/submit | quick win, low competition |
| MCP Hub | https://mcphub.io/submit | MCP-specific catalogue |
| Awesome AI Tools | <https://github.com/mahseema/awesome-ai-tools> README PR | community-maintained list |

For each: submit `https://smb-broker.onrender.com`, category "Developer
Tools" or "Productivity / Scheduling", description "12-tool MCP server
for AI agents to interact with small businesses worldwide. Free tier."

---

## STEP 5 — long-tail SEO content (do this once you've sent the emails)

Write **one** short post on each, with the same content; it auto-fans
out across crawlers AI agents read:

1. **dev.to** — title "Agent Broker: an MCP server that lets Claude
   actually book appointments". 600 words. Links to repo + endpoint.
   `dev.to` is in OpenAI/Anthropic crawler diet.
2. **Hashnode** — same post, mirror.
3. **GitHub Discussions** in your own repo — pin a thread "How agents
   discover Agent Broker". Hosts a copy-paste config snippet.

Total writing time: 90 minutes once. Crawlers fetch within ~7 days.

---

## STEP 6 — the live `/healthz/external` endpoint

Now exposed at:

> **<https://smb-broker.onrender.com/healthz/external>**

It pings every upstream (Twilio, Cal.com, Vapi, Resend, Paddle) plus
internal discovery surfaces, in parallel. Returns:

- Twilio balance (with low-balance warning if < $2)
- Cal.com auth status
- Vapi auth status
- Resend key scope (full vs send-only)
- Paddle auth status
- Internal manifest / MCP tools-list / llms.txt sanity

Bookmark this. If it ever turns red, you'll know exactly which upstream
broke without logging into five dashboards.

You can also have Render's free **uptime alerts** ping it once a minute
(in your Render dashboard → service → Settings → Health Checks).

---

## What's still pending after you do all six steps

| Pending | Time | Critical? |
|---|---|---|
| Map `smb-broker.onrender.com` CNAME → `smb-broker.onrender.com` | 5 min in DigitalPlat web UI | **Yes — many of the URLs above don't resolve until this is done** |
| Open the two pre-fork URLs above and re-run the registry script | 3 min | High — these are the most valuable PRs |
| Paddle business verification | 2-5 days, hands-off | Only matters when you make a sale |
| Send the six emails | 15 min, all at once | Low (most won't reply) but free leverage |
| Submit the 4 directory forms | 12 min | Medium — they bring in human-driven traffic |

**Total active time from you: ~45 minutes.**

After that, the system runs itself for 6-12 months as documented in
`LAUNCH_STATUS.md` and `EXPIRY_CHECKLIST.md`.

---

## Why these channels (not Twitter/HN/Discord)

Each channel above is one of three types:

1. **Crawled by the LLM agents themselves** (registries, llms.txt,
   apis.guru) — the agent finds you in its own data without a human.
2. **Crawled by Google for "AI tool that does X" queries** (directories,
   dev.to posts) — the human Googling Claude usage finds you.
3. **Read by tool-marketplace curators** (Cursor / Continue / Cline
   maintainers) — the developer building their own tool catalog finds you.

None of these require a follower count. None require you to "build in
public". This is the channel mix designed for an MCP server in 2026.

---

## Win-rate facts (so when someone asks "why this one")

- 0.818 aggregate WinRate measured across 504 simulated trials
  (3 personas × 168 tasks × 3 trials). See `optimizer/simulate.py`
  and `docs/BENCHMARKS.md`.
- 22 jurisdictions with native compliance rules. INTERNATIONAL
  conservative default for the rest. Code: `compliance/jurisdiction_rules.py`.
- 7 agent-discovery protocols (MCP, ai-plugin.json, openai-tools.json,
  anthropic-tools.json, agents.json, mcp.json, llms.txt).
- 12 operations with one OutcomeReceipt schema, one compliance gate,
  one idempotency contract.

These four numbers are your headlines. Cite them, don't re-derive them.
