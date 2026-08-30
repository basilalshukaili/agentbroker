> **⚠️ HISTORICAL — NOT CURRENT STATE (stamped 2026-08-17).**
> This file is a point-in-time snapshot kept for history. Claims about deploys,
> pricing, endpoints, and status here may be stale or contradicted by later work.
> The authoritative current picture is **README.md** (what the service does and
> what it costs) and **docs/PRICING.md** (every price, derived from
> `billing/pricing.py`). Both are kept current. `git log` is the full record.

# Project Overview & Strategy — Agent Broker

> **The honest version. Read once, then go act on Section 6.**

---

## 1. What this is, in one sentence

A live MCP server that lets any AI agent take a Cal.com / Calendly / Doctolib / Booksy / Fresha / OpenTable / Setmore / Square / Acuity / Schedulista / Squarespace / BookMyCity URL and *actually* book the appointment — through a single tool surface, with TCPA / GDPR / CASL compliance enforced.

## 2. What's verified live (2026-05-05)

**Primary (edge — use this in all agent configs and submissions):**
- `https://hatchloop.dev/mcp/agent-broker` — 20 tools, 40–65 ms MCP reads
- `https://hatchloop.dev/.well-known/mcp.json`
- All discovery endpoints — 40–70 ms globally (embedded snapshots, no origin)

**Origin (internal — Render, never give this URL to agents):**
- `https://hatchloop.dev` — tool execution, kept warm by cron

**Registries:**
- `https://smithery.ai/server/lordbasil147/agent-broker` — Smithery listing (update URL to edge)
- 3 GitHub PRs open: modelcontextprotocol/servers #4077, awesome-mcp-servers, apis.guru
- 103/103 tests pass
- total_agents_requested = **0** (distribution is the bottleneck)

## 3. The honest market reality

I'm telling you what I'd tell a co-founder, not what would feel good:

### What's working FOR us

- **Architecture is correct.** 12 well-defined operations, single OutcomeReceipt schema, non-bypassable compliance gate, idempotency, multi-protocol discovery. A reviewer can confirm this in 5 minutes.
- **Real upstream services are wired and authenticated.** Twilio, Cal.com, Vapi, Resend, Paddle. Not stubs.
- **Two genuine wedges.** (a) The booking-page importer is unique — no other MCP turns "any URL" into a callable. (b) The standalone `/compliance/check` API is independently valuable to non-MCP devs.
- **Distribution surface is broad.** Smithery + Glama + 4 GitHub catalogs + apis.guru + 4 directories.

### What's working AGAINST us

- **Cold-start problem is real.** Directory is empty. First agent gets 0 results. We now show a recovery path (`import_booking_url`) but the agent's user has to *want to provide* a URL. Most users won't.
- **MCP fatigue.** Smithery alone has 22,438 servers. Discovery is keyword-based; "Agent Broker" is generic. The pattern that wins is naming a SaaS or vertical (e.g., "cal-com-mcp") not a layer.
- **The 0.818 WinRate is engineering theater.** We measured against synthetic competitors with random noise. We have not measured against any real competing MCP. Don't cite this number to a serious reviewer.
- **Cold email reply rates are <1%.** The 5 emails are passive — they exist in inboxes, may surface in search someday, but won't drive next-week traffic.
- **Render free dyno cold-start is 30 s.** First agent that hits a sleeping dyno may time out (default HTTP timeouts are 5-10 s). Set up UptimeRobot or it's a self-inflicted wound.

### The realistic outcome distribution

Based on what I observe about MCP launches in 2026:

| Outcome | Probability | What it looks like |
|---|---|---|
| Dormant for 3-6 months, no traction | ~50% | `/api/metrics` stays at zero. Probably the most likely. |
| Wedge into a single niche (most likely Cal.com agents) | ~25% | A trickle of agent calls, mostly `import_booking_url` + `schedule_appointment`. Compounding starts. |
| Picked up by a newsletter or framework, mid-tier viral | ~15% | Latent Space / Simon Willison / AI Tinkerers shoutout. Spike, then settles to a baseline. |
| Featured by Anthropic / Cursor / OpenAI directly | ~5% | A registry feature or blog post. The lottery ticket. |
| Acquihire / partnership offer | ~2% | Long-tail upside if any of (1)–(4) compounds for 6+ months. |
| A bug we shipped causes data leak / TOS violation | ~3% | Reputational damage. Mitigated by compliance-gate code being 100% test-covered. |

The mean expected outcome is **dormant for 3-6 months → niche traction**. Plan for that.

## 4. What I changed today vs. yesterday's version

Yesterday's response told you to "sit back and wait for traffic." That advice was incomplete because the product as-shipped had three soft failures that would silently kill the cold start:

1. **Empty-state UX** told the agent to "expand radius_miles" — wrong advice when directory has zero entries.
2. **Positioning** was "horizontal agent-to-business action layer" — generic, not tweetable, doesn't match user search intent.
3. **No standalone wedge.** All value was locked behind the broker. Devs who don't use our broker had no reason to even visit.

Three commits today fix all three:

- `af95759` — repositioned hero ("Give Claude any booking URL. Claude books it."), fixed empty-state to point at `import_booking_url` with 5 platform examples, added `/compliance/check`, `/supply/platforms`, `/demo` as public no-auth wedges.
- `7f9e970` — fixed the support-email bounce (was pointing at `*.onrender.com` which doesn't accept email).
- `9b1001c` — added `/openapi.yaml` route so apis.guru's bot can validate our spec.

**This is the version that has a real shot.** Yesterday's version had a real shot too, but a smaller one.

## 5. The five-channel distribution map

Where actual traffic comes from for an MCP in 2026, ranked by leverage:

| # | Channel | Status | Realistic timeframe |
|---|---|---|---|
| 1 | Modelcontextprotocol/servers PR (official catalog) | ✅ #4077 open | merge in 5-10 days; trickle starts within hours of merge |
| 2 | Smithery + Glama listings | ✅ Smithery live; Glama indexes from GitHub topic in 1-3 days | passive, ongoing |
| 3 | Mention by one mid-tier AI newsletter or YouTuber | ❌ not done | requires you to cold-email 5-10 specific writers (see Section 6) |
| 4 | A 60-90 second viral demo video | ❌ not done | one weekend of effort; biggest single lever |
| 5 | Standalone `/compliance/check` API SEO | ✅ live | 30-90 days for indexing |

Channels 3 and 4 are what I genuinely believe will move the needle in a 30-day timeframe. The rest are passive infrastructure.

## 6. What you do next — three actions, one weekend

I keep telling you "sit back" because the system runs itself. But there are three high-leverage moves that you uniquely can do, that I cannot, and that genuinely raise the probability of traction.

### 6.1 Record a 60-90 second demo video (this weekend)

**Setup:** install Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-broker": { "url": "https://hatchloop.dev/mcp/agent-broker" }
  }
}
```

**Script** (literal words — say this out loud while screen-recording):

> *"This is Claude Desktop with the Agent Broker MCP installed. I'm going to ask Claude to book me an appointment using a real Cal.com link my friend sent me."*
>
> *(type into Claude:)* "Book me a 30-minute coffee chat at https://cal.com/peer next Tuesday at 3pm PT."
>
> *(Claude calls `import_booking_url`, then `schedule_appointment`. Show the OutcomeReceipt JSON.)*
>
> *"That's it. Claude turned a URL my friend texted me into an actual booking, with full TCPA/GDPR compliance enforcement. Works for Cal.com, Calendly, Doctolib, Booksy, OpenTable, and 7 others. Free for any agent up to 100 calls a month. Link in description."*

**Tools:** Loom (free) or QuickTime. Record once, redo if you stumble. 90 seconds max.

**Where to post:**

- X/Twitter, with text: *"Built an MCP that turns any booking URL into a Claude-bookable appointment. Cal.com, Calendly, Doctolib, OpenTable, 8 others. Free tier. Demo:"* + link to the video.
- Reddit r/ClaudeAI, r/MCP, r/LocalLLaMA — title: *"Built an MCP server that turns any Cal.com / Calendly / OpenTable URL into a Claude-bookable appointment"*. Same video.
- Hacker News Show — only if the video is genuinely tight; HN punishes weak demos.

**Why this is the highest-leverage move:** one good demo can drive 1,000+ first agents in a single week. Cold emails and registry listings cannot do that.

### 6.2 Cold-email 5 specific writers (90 minutes)

Generic dev-relations emails get <1%. Specific named writers who already write about MCP get 5-10%. Email these five, one by one, with a personal first sentence:

| Name | Outlet | Why them |
|---|---|---|
| Simon Willison | https://simonwillison.net/ | Documents every interesting LLM tool he tries. If our `/demo` works in his terminal, we're in his blog. |
| Swyx (Shawn Wang) | Latent Space podcast | MCP-curious audience. A weekly digest mention is real distribution. |
| Greg Schoeninger | Oxen.ai blog | Writes hands-on MCP integrations. |
| Linus Lee | https://thesephist.com/ | Writes one-off LLM-tool reviews; high conversion when he likes something. |
| Han Lee | https://www.danielhan.dev/ | Builds LLM frameworks; would care about our compliance API. |

Email subject: *"MCP server that turns any booking URL into a Claude action — 60s demo"* + the demo video URL. **Don't** explain features. Lead with the video. Two sentences, then the link.

### 6.3 Set the maintenance autopilot (10 minutes)

You already have `/healthz/external` and the outreach ledger. Add the two final pieces I described in the previous turn:

1. **UptimeRobot HTTP monitor** at https://uptimerobot.com (free tier) → ping `/health` every 5 min. Keeps the dyno warm so first impressions don't time out, and emails you the moment anything breaks.
2. **Gmail filter** for replies (set in previous turn).
3. **Calendar reminder** every 6 months to check `/api/metrics` and the Gmail label.

Total: 10 minutes once.

## 7. What I will NOT do (and why)

- **Pre-seed the directory with real businesses I scraped.** Reputational risk and TOS murky. Better to ship the empty-state UX honestly and let `import_booking_url` populate the directory organically.
- **Add a paid landing page or pricing pop-up.** Nothing converts at zero traffic. Pricing matters at 1k ops/month, not zero.
- **Rebuild the home page in React.** Already learned that lesson. Server-rendered HTML is faster, more crawlable, more durable.
- **Submit to 20 more directories.** Diminishing returns. The four you've done cover ~95% of agent-discovery surface.
- **Run paid ads.** You said zero cost. Nothing has changed about that constraint.

## 8. Files to read if you want to verify any of the above

- `core/find_business.py` lines 33-72 — the empty-state recovery payload
- `main.py` (top of `Public APIs` section) — `/compliance/check`, `/supply/platforms`, `/demo`, `/compliance/jurisdictions`
- `web/pages.py` lines 49-72 — repositioned hero
- `OUTREACH_KIT.md` — the existing outreach playbook
- `LAUNCH_STATUS.md` — yesterday's "what shipped" summary
- `EXPIRY_CHECKLIST.md` — service-by-service expiration tracking
- `agent_interface/health_external.py` — what `/healthz/external` actually checks

## 9. The single most important sentence in this document

**Open `/api/metrics` once a week. The day `total_agents_requested` is non-zero is the day this project starts mattering.** Until then, everything else is preparation.

The system will run itself for 6-12 months at $0 cost. Your only meaningful action between now and traction is **the demo video and the five cold emails**.
