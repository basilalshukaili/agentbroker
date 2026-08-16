# AgentBroker listing/positioning refresh — drift audit + founder steps
**Date: 2026-08-16. Nothing in this doc has been submitted anywhere — research + in-repo docs only.**

---

## TL;DR for the founder

1. **Before touching any listing: the "16 tools" claim isn't live yet, anywhere.** The two newest tools
   (`check_booking_link`, `check_compliance`) are merged into `manifest/manifest.json` on `main`, but
   the Render origin *and both* Cloudflare edges are still serving the old 14-tool `tools/list`. This
   is very likely the known "Render `autoDeploy` didn't fire" gotcha repeating — needs one explicit
   redeploy trigger + a live re-check before any listing claims 16.
2. **Smithery was acquired by Arcade.dev on 2026-08-05.** The listing UI, docs, and probably the
   update/verification flow now live under the Arcade umbrella. Re-verify the login/dashboard path
   still works before assuming the old steps apply as-is.
3. Smithery's tool **count** on the listing (14) is actually *live-accurate* right now — it auto-scans
   your MCP endpoint. What's stale is the **hand-written overview text** ("13 MCP tools"), the
   **per-call cost strings baked into your own tool descriptions** (`$0.01/call`, `$0.02/call`, etc. —
   leftover x402 wording, contradicts the real $49/mo flat model), and the **connection URL** (still
   the old, credential-unknown `basil-agent` edge). Those need action beyond `git push`.
4. Cloudflare's **Monetization Gateway** is real, announced 2026-07-01, still **waitlist-only** (GA
   expected Q4 2026). The waitlist form is a plain Google Form — no Cloudflare login required to
   join it. But *using* it later will require the Cloudflare account that actually owns the
   zone/domain serving the MCP endpoint, which raises the same basil-agent-vs-techmate account
   question as the Smithery listing.
5. The official MCP registry entry (`io.github.basilalshukaili/agent-broker`) is on **v1.0.2**,
   published 2026-05-25, and **still contains the x402/USDC pricing language** the README explicitly
   disavowed in a later commit. `server.json` in the repo has the identical stale text — the README
   fix never propagated there. Re-publish procedure is documented below (GitHub Actions + OIDC, no
   manual dashboard).

---

## 1. SMITHERY DRIFT

### 1a. What's actually live right now (verified 2026-08-16, not from memory)

| Surface | Tool count via `tools/list` | Notes |
|---|---|---|
| Render origin `smb-broker.onrender.com/mcp` | **14** | `check_booking_link` / `check_compliance` return 404 on REST (`/ops/check_compliance`, `/ops/check_booking_link`) — not deployed |
| Edge `agent-broker-edge.techmate.workers.dev/mcp` | **14** | The new, founder-controlled edge (rate-limiter deployed here 2026-08-13) |
| Edge `agent-broker-edge.basil-agent.workers.dev/mcp` | **14** | The OLD edge — still live and healthy, still what Smithery's listing and the MCP registry both point at. Uses stale per-call cost strings in tool descriptions, e.g. `"Cost: $0.005/call (requires Developer token — get one at https://agent-broker-edge.basil-agent.workers.dev/checkout)"` — a checkout mechanism that doesn't match the current Polar flow at all |
| Smithery listing (`smithery.ai/servers/lordbasil147/agent-broker`) | **14** (auto-scanned, live) | Overview text still says "13 MCP tools" (stale, older than the scan) |
| `manifest/manifest.json` (repo, source of truth) | **16** | `check_booking_link`, `check_compliance` present, wired into `agent_interface/mcp_server.py` |
| `manifest/mcp_tools.json` (repo, static snapshot source) | **14** | **Never regenerated** after the two new-tool commits |
| `edge/src/snapshots/mcp-tools-list.json` (repo, edge-embedded snapshot) | **14** | Same gap — this is what the edge serves for `initialize`/`tools/list` per the architecture doc |

**Root cause, confirmed by git:**
- Commits `08fea62` (check_booking_link) and `4331159` (check_compliance) are on `origin/main` —
  they were pushed correctly.
- `agent_interface/mcp_server.py` builds `tools/list` dynamically from `manifest/manifest.json`
  (`get_full_manifest()`, cached in memory) — so the *code*, if actually running, would already
  serve 16.
- But the Render origin's live `tools/list` still returns 14, and its own `/` health page footer
  literally says *"12 tools, 7 discovery protocols"* (even more stale than 14) — meaning the running
  process predates several recent merges. This matches the previously-documented Render gotcha:
  **`autoDeploy: yes` does not reliably fire on push; an explicit `POST /v1/services/<id>/deploys`
  is needed.**
- The Cloudflare edge problem is separate and additional: even *after* Render redeploys, the edge's
  own embedded snapshot files (`edge/src/snapshots/mcp-tools-list.json`,
  `manifest/mcp_tools.json`) are static, checked-in JSON that must be **manually regenerated and
  redeployed to the Worker** — they don't pull live from the origin per-request (that's the whole
  point of the edge: serve `initialize`/`tools/list` from a snapshot in ~40-70ms without hitting
  Render). Confirmed: neither of the two new tool names appears anywhere under `edge/src/`.

**Action needed before any "16 tools" claim goes out (Smithery, registry, marketing):**
1. Trigger an explicit Render redeploy (`POST /v1/services/<id>/deploys`, per the known gotcha) and
   re-check `curl -X POST https://smb-broker.onrender.com/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'` for 16 results.
2. Regenerate `manifest/mcp_tools.json` and `edge/src/snapshots/mcp-tools-list.json` (and the sibling
   `mcp-initialize.json` / `mcp.json` snapshots) from the current `manifest/manifest.json`, and
   redeploy the Worker(s) — both `techmate.workers.dev` and (if credentials are ever recovered)
   `basil-agent.workers.dev`.
3. Re-curl both edges to confirm 16 before touching Smithery.

This is infra/deploy work, not documentation — flagging it here rather than doing it, since it's
outside this task's "don't submit/change anything live" scope and touches paid infra (Render/CF).

### 1b. `smithery.yaml` (in-repo) — updated

`agentbroker/deploy/registry-submissions/smithery.yaml` has been updated in place to current truth:
- Tool count 14 → 16, both new tools added to the `tools:` list.
- Read-free/write-gated split corrected: it's **8 read tools free / 8 write tools gated**, not 6/8
  (`check_booking_link` and `check_compliance` both carry `readOnlyHint: true` /
  `cost_model.basis: "free"` in `agent_interface/mcp_server.py` — confirmed by reading the
  annotation-building code directly, not assumed).
- **Fixed a real bug**: the file's Polar checkout link was the *old, dead* one-time $9/90-day link
  (`polar_cl_xmYfvh3u747R3UuYV4H4lBUZsRxAk9uD02MxY1XXufL`). The link actually wired into the live
  rate-limiter (`edge/src/rate-limit.ts`) and the README is a **different** link
  (`polar_cl_zRn6I67zMjFuenkjDme5RCnDYmA3vefHqX1zG3A5Phh`, $49/mo). Anyone who read the old
  smithery.yaml and paid via its link would have bought the wrong (expired-framing) product. Now
  matches the live link.
- Added an inline comment flagging the deploy gap (§1a) so this doesn't get "fixed" a second time
  by copying stale numbers back in from a docs pass that doesn't re-verify live.
- Left the `connection.url` pointed at `techmate.workers.dev` (the founder-controlled account) since
  that's the intended target per commit `081f271`, with a comment explaining the live listing still
  points elsewhere — see founder step 2 below.

### 1c. Exactly what the founder must do to sync the LIVE Smithery listing

Smithery does **not** offer a bare metadata-edit web form the way the old registry PR flow did.
Per its own docs (`smithery.ai/docs/build/publish`, confirmed live 2026-08-16):

> "Smithery scans your server to extract metadata (tools, prompts, resources) for your server page.
> Public servers: Scan completes automatically."

So the tool list re-syncs itself once the live endpoint actually serves 16 (§1a) — no manual
tool-by-tool form-filling needed for that part. What genuinely needs the founder's hands:

1. **Log in** at `smithery.ai/login` (redirects through `/servers/lordbasil147/agent-broker` —
   this is a **GitHub OAuth login**, presumably `basilalshukaili`/`lordbasil147` GitHub identity,
   now under the Arcade.dev-owned Smithery). *I cannot do this step — it's an account login.*
2. Open the `agent-broker` listing → **Settings**. Per the docs, `Settings → Verification` is where
   official-vendor verification lives; the connection URL / static overview text editing is very
   likely under the same Settings area (I could not reach it without logging in — this needs
   founder eyes once logged in).
3. **Update the connection URL** from `agent-broker-edge.basil-agent.workers.dev/mcp` to
   `agent-broker-edge.techmate.workers.dev/mcp` — this is the standing open gate
   `agentbroker-smithery-url` in `state/founder_gates.json` (status: `NEEDS-DECISION`, ~2 min,
   still open as of this audit). Recommended over the alternative (finding the basil-agent
   Cloudflare credentials) because techmate is the account you actually control and the rate
   limiter is only deployed there.
4. **Re-trigger a scan** (likely a "Redeploy"/"Rescan" action in the same Settings page, or it may
   auto-rescan once the URL changes — verify once logged in) so the tool count, cost strings, and
   overview text refresh from the corrected live endpoint.
5. **Manually edit the Overview description** if it isn't auto-replaced by the scan — the "13 MCP
   tools..." sentence is hand-entered copy, not scan output, per the docs' "Static Server Card
   (manual metadata)" option existing as a separate mechanism from the automatic scan.
6. Optional but recommended: fix the **per-tool cost strings baked into `tools/list` descriptions**
   themselves (e.g. `find_business`'s description literally says "Cost: $0.005/call (requires
   Developer token — get one at .../checkout)") — this text comes from your own server, not
   Smithery, so Smithery will keep showing it verbatim until the code that formats tool descriptions
   (`agent_interface/mcp_server.py::_format_description_for_llm`) is updated to describe the $49/mo
   flat model instead of per-call x402 pricing. This is a real, user-facing inconsistency: a buyer
   reading the Smithery card sees "$0.01/call" pricing and a $49/mo Polar link in the same breath.

**Do not** file a PR to `github.com/smithery-ai/registry` — that repo predates the Arcade
acquisition and the listing flow now runs through `smithery.ai/new` + live scanning, not a static
registry file, per current docs.

---

## 2. CLOUDFLARE MONETIZATION GATEWAY

### 2a. What it is (verified against Cloudflare's own blog, 2026-08-16)

- Announced 2026-07-01: **Monetization Gateway** — lets a Cloudflare customer charge for any
  resource behind Cloudflare (web pages, datasets, **APIs, and MCP tools**) via the **x402** open
  protocol, settled in stablecoins (USDC / Open USD), enforced at the edge (no origin payments
  stack needed). Pricing is written as edge rules/expressions (similar to existing CF rule syntax).
  Source: [blog.cloudflare.com/monetization-gateway](https://blog.cloudflare.com/monetization-gateway/)
- Announced 2026-08-04 (same week): **Cloudflare Wallets** + **cloudflare.pay** — the buyer-side
  complement. Gives AI agents a programmable wallet + human-readable handle
  (`handle.cloudflare.pay`) so they can actually pay for gated resources. Two-phase rollout: handle
  reservation is live now; full pay-in/pay-out functionality ("coming months," no firm date).
  Source: [blog.cloudflare.com/wallets](https://blog.cloudflare.com/wallets/)
- **Status as of 2026-08-16: both still pre-GA.** Monetization Gateway is waitlist-only; GA is
  expected **Q4 2026**. This is squarely a "stay warm, don't build on it yet" item, consistent with
  the existing founder framing that AgentBroker/agent-economy bets are a *future* line, not where
  near-term investment should go.

### 2b. Exact waitlist signup (verified live, 2026-08-16 — nothing submitted)

The waitlist is a **plain Google Form**, not a Cloudflare-account-gated signup:
`docs.google.com/forms/d/e/1FAIpQLSfq6yaIgp57FCGFg7riXlSWTeD8d8Adur2c8tWaKY4SuzweiQ/viewform`

Fields, in order:
1. **Email address** — required
2. **Name** — required
3. **Title** — optional
4. **Website** — required
5. **Service Description** ("Briefly describe the service you want to monetize") — optional
6. **Beta Program Interest** — 5-point scale, "Just exploring" → "Ready to implement"
7. **Feature Requests** (e.g. variable pricing, subscriptions) — optional
Plus a reCAPTCHA; confirmation is emailed to the address provided.

**So joining the waitlist itself doesn't require deciding the Cloudflare-account question** — it's
just contact info. That question only becomes real once (if) access is granted and actual
configuration starts.

### 2c. The account question (why it matters, not fully resolvable from outside)

Cloudflare's own copy states the waitlist is "open now for Cloudflare customers" and the gateway is
configured per protected zone/domain at the edge. AgentBroker currently has the **same
two-Cloudflare-account split** that's already an open problem for the Smithery listing:
- `basil-agent.workers.dev` — an older account, **credentials unknown**, currently what's live on
  Smithery + the MCP registry.
- `techmate.workers.dev` (account `8f0ebd0046e3f1c8a506f8f0f6d9476f`, `lordbasil147@gmail.com`) —
  the account the founder actually controls; rate-limiter already deployed here 2026-08-13.

Whichever domain/zone the founder eventually points buyers at is the account the Monetization
Gateway would need to be enabled on. Given the Smithery URL gate (§1c step 3) is already steering
everything toward `techmate.workers.dev`, that's the natural account to list on the waitlist form
too — but note one open technical unknown I could not verify from outside: **whether Monetization
Gateway requires a Cloudflare-proxied *custom domain/zone* (orange-cloud DNS) rather than a bare
`*.workers.dev` subdomain**, since `workers.dev` is a Cloudflare-owned shared domain, not a
customer zone in the traditional sense. If a bare `workers.dev` subdomain isn't eligible, using the
gateway later would require first putting a real custom domain (e.g. an `agentbroker.*` domain) on
Cloudflare DNS for the `techmate` account — worth a direct question to Cloudflare support or a
closer look at the docs once past the waitlist, not something to assume either way.

### 2d. Recommended signup steps (not yet done — awaiting founder go-ahead)

1. Decide: list `agentbroker@[founder email]`, Website = `https://agent-broker-edge.techmate.workers.dev`
   (or the eventual custom domain, if the zone question in §2c resolves that direction first).
2. Service Description (suggested draft): *"Agent Broker — an MCP server letting AI agents find,
   verify, message, and book with small/mid-size businesses. Want to monetize individual MCP tool
   calls (find_business, schedule_appointment, etc.) directly at the edge instead of running our own
   Polar subscription gate."*
3. Beta interest: mid-scale ("interested, evaluating") is honest — GA is Q4 2026 and the founder's
   own framing has this as a future-not-now bet.
4. Feature request (optional, worth asking since it's free): support for a **flat
   subscription/allowance model** (not just per-call stablecoin micropayments) — since AgentBroker's
   proven, actually-working rail is the $49/mo Polar flat fee, not per-call x402 (x402-as-primary-rail
   was already tried and explicitly rejected — see `agent-broker-state` memory's "Don'ts").

---

## 3. OFFICIAL MCP REGISTRY (`registry.modelcontextprotocol.io`)

### 3a. Current live entry (queried directly, 2026-08-16)

`io.github.basilalshukaili/agent-broker` has **3 published versions**; the latest (`isLatest: true`)
is:

```json
{
  "name": "io.github.basilalshukaili/agent-broker",
  "title": "Agent Broker",
  "description": "AI agents find, message & book SMBs; pay per call in USDC on Base via x402. 14 tools, compliant.",
  "websiteUrl": "https://agent-broker-edge.basil-agent.workers.dev",
  "repository": { "url": "https://github.com/basilalshukaili/agentbroker", "source": "github" },
  "version": "1.0.2",
  "remotes": [{ "type": "streamable-http", "url": "https://agent-broker-edge.basil-agent.workers.dev/mcp" }]
}
```
Published 2026-05-25 (`publishedAt`/`updatedAt` both that date — never touched since).

**What's stale, specifically:**
- **"pay per call in USDC on Base via x402"** — the README's own honesty-fix commit
  (`ab3f7a9`, "no false x402/supply claims") explicitly disavows this exact framing, but
  `server.json` in the repo (checked 2026-08-16) has **byte-identical stale text** — the fix never
  reached this file. This is the most actionable single fix here.
- **"14 tools"** — needs to become 16, but only *after* the deploy gap in §1a is actually closed
  (no point re-publishing a number that isn't live yet).
- **`websiteUrl` / `remotes[0].url`** — still the old `basil-agent` edge, same question as §1c step 3.

### 3b. Exact re-publish procedure (already built, no manual dashboard involved)

Publishing runs through `.github/workflows/publish-mcp.yml`, already in the repo:
- Trigger: **push a git tag matching `v*`** (e.g. `v1.0.3`), or run the workflow manually
  (`workflow_dispatch` is enabled).
- Auth: **GitHub OIDC** — no stored secret, authenticates as the repo owner to claim the
  `io.github.basilalshukaili/*` namespace automatically.
- Steps the workflow runs: install `mcp-publisher` → validate `server.json` → `mcp-publisher login
  github-oidc` → `mcp-publisher publish`.

**To actually fix the registry entry:**
1. Edit `server.json` (repo root) — remove the x402/USDC line, update `description` and tool count
   to match whatever is verified live at the time (don't pre-write "16" until §1a is closed), correct
   `websiteUrl`/`remotes[0].url` if the edge decision (§1c/§2c) has landed by then.
2. Bump `"version"` — must be a new, unique semver; **`v1.0.2` is already claimed** (confirmed: tag
   exists on the remote at a commit whose own message is literally *"feat(registry): refresh MCP
   Registry listing (x402 + 14 tools) via OIDC workflow"* — i.e. the x402 language was pushed
   *deliberately* at some point, not by accident; worth knowing before assuming this is pure drift).
   Next version should be `1.0.3`.
3. Commit the `server.json` change to `main`.
4. `git tag v1.0.3 && git push origin v1.0.3` — this alone fires the publish workflow. No web
   dashboard, no manual registry PR.
5. Re-query `https://registry.modelcontextprotocol.io/v0/servers?search=agent-broker` afterward to
   confirm `isLatest: true` moved to `1.0.3` with the corrected fields.

This is a code change + tag push, which I have not done (task scope was research + the one in-repo
`smithery.yaml` doc update explicitly requested) — flagging the exact diff needed rather than
pushing it.

---

## Consolidated founder action list (ordered)

1. **Redeploy the Render origin explicitly** (`POST /v1/services/<id>/deploys`, per the known
   auto-deploy gotcha) and confirm `tools/list` returns 16 — this unblocks everything else honestly
   claiming "16 tools."
2. **Regenerate + redeploy the Cloudflare edge snapshots** (`manifest/mcp_tools.json`,
   `edge/src/snapshots/*`) so the edge's own `tools/list` matches the 16-tool origin.
3. **Decide the basil-agent vs. techmate edge question once, for all three surfaces at once**
   (Smithery connection URL, MCP registry `remotes[0].url`, and any future Cloudflare
   Monetization Gateway zone) — recommend techmate since it's the account with known credentials
   and the rate-limiter already lives there. This closes the standing `agentbroker-smithery-url`
   gate in `state/founder_gates.json`.
4. **Log into Smithery** (`smithery.ai/login`, now under Arcade.dev) → `agent-broker` listing →
   Settings → update connection URL, re-scan, fix the manual overview text.
5. **Fix `server.json`** (remove stale x402/USDC line, correct tool count once §1 is closed,
   correct URL) → bump to `v1.0.3` → tag + push to re-publish the MCP registry entry via the
   existing OIDC GitHub Action.
6. **Optional, no rush**: join the Cloudflare Monetization Gateway waitlist (Google Form, no
   Cloudflare login needed) — low-cost, future-optionality move consistent with "keep the
   agent-economy arsenal warm without investing now."
7. **Optional cleanup**: update `agent_interface/mcp_server.py`'s description formatter to stop
   baking per-call x402 cost strings (`"$0.01/call"`, `.../checkout`) into live tool descriptions —
   these actively contradict the $49/mo flat-fee model buyers are actually offered.

## Files touched this session
- `C:\ai company\agentbroker\deploy\registry-submissions\smithery.yaml` — updated to 16 tools,
  correct 8/8 free/paid split, fixed Polar checkout link, deploy-gap warning comment.
- `C:\ai company\agentbroker\deploy\registry-submissions\smithery-sync-2026-08-16.md` — this file.

Nothing else in the repo was modified. Nothing was submitted to Smithery, Cloudflare, or the MCP
registry.
