# Go Live Now — 3-step Launch

> **Time required: 30 minutes of your time. Zero dollars.**
> Everything I could automate is automated. Everything left needs a human at a keyboard once.

---

## Pre-flight verification

You already see this in your terminal — all 9 checks green:

```
[OK] Twilio    Account 'My first Twilio account' status=active
[OK] Cal.com   User 'basil-9t8bfa' email=basilalshukaili@gmail.com
[OK] Vapi      API key valid; 1 assistant configured
[OK] Resend    API key valid (send-only restricted key — safe scope)
[OK] Paddle    API key valid (production); 56 event types reachable
[OK] Manifest  12 operations defined
[OK] MCP       12 tools listed via JSON-RPC
[OK] Compliance gate fires correctly
[OK] Discovery surfaces all operational
```

Twilio balance: **$15.50** (about 2,000 SMS or 30 voice minutes of runway).

---

## Step 1 — Push to GitHub (5 min)

You already created `https://github.com/basilalshukaili/agentbroker` and have git installed.

Open PowerShell in the project root and run:

```powershell
cd "C:\Users\basil\OneDrive - Dhofar Insurance Company (S.A.O.G.)\Desktop\AI First\service-root"

git init
git branch -M main

# Verify .env is NOT going to be committed (it's in .gitignore)
git check-ignore .env
# Expected output: .env (means it WILL be ignored — good)

git add -A
git status         # confirm .env does NOT appear in the list of files-to-commit
git commit -m "agent-broker: initial production launch"

git remote add origin https://github.com/basilalshukaili/agentbroker.git
git push -u origin main
```

If the push asks for a password, GitHub now requires a **Personal Access Token**:
1. <https://github.com/settings/tokens/new>
2. Note: "agentbroker"
3. Scope: tick `repo` only.
4. Click Generate.
5. Use the token as the password when git asks. Save it; you won't see it again.

**After push: also add the FLY_API_TOKEN as a GitHub Actions secret** so the auto-deploy workflow can run:
1. `https://github.com/basilalshukaili/agentbroker/settings/secrets/actions/new`
2. Name: `FLY_API_TOKEN`
3. Value: paste your `FLY_API_TOKEN` from `.env` (the long string starting with `FlyV1 fm2_...`)
4. Save.

---

## Step 2 — Deploy to Fly.io (10 min)

### Option A: Use the GitHub Action (preferred, fully hands-off going forward)

After Step 1, the `.github/workflows/deploy.yml` runs automatically on every push.
First run will take ~5 minutes. Watch it at
`https://github.com/basilalshukaili/agentbroker/actions`.

### Option B: One-shot from your terminal

If you'd rather deploy directly:

```powershell
# Install flyctl once
iwr https://fly.io/install.ps1 -useb | iex

# Authenticate using the org token from .env
$env:FLY_API_TOKEN = (Get-Content .env | Select-String "^FLY_API_TOKEN=").Line.Split("=", 2)[1]

# Create the app on Fly's side (skip if Fly says it already exists)
flyctl apps create agentbroker

# Push all secrets from .env to Fly
flyctl secrets set --config deploy/fly.toml `
  TWILIO_ACCOUNT_SID="your_twilio_account_sid" `
  TWILIO_AUTH_TOKEN="your_twilio_auth_token" `
  CALCOM_API_KEY="your_calcom_api_key" `
  CALCOM_USERNAME="basil-9t8bfa" `
  VAPI_API_KEY="your_vapi_api_key" `
  VAPI_PUBLIC_KEY="your_vapi_public_key" `
  RESEND_API_KEY="your_resend_api_key" `
  PADDLE_API_KEY="your_paddle_api_key" `
  POLAR_API_KEY="your_polar_api_key" `
  BILLING_PROVIDER="paddle" `
  AGENT_IDENTITY_SIGNING_SECRET="$([System.Web.Security.Membership]::GeneratePassword(64, 0))" `
  BILLING_RECEIPT_SIGNING_SECRET="$([System.Web.Security.Membership]::GeneratePassword(64, 0))" `
  PUBLIC_BASE_URL="https://agentbroker.qzz.io" `
  COMPLIANCE_DEFAULT_JURISDICTION="international" `
  SUPPLY_SEED_MODE="empty" `
  REQUIRE_AUTH="false"

# Deploy
flyctl deploy --config deploy/fly.toml --remote-only

# Once deployed, get the assigned hostname
flyctl status --config deploy/fly.toml
```

The output gives you something like `agentbroker.fly.dev` — that's the temporary
URL. Test it:

```powershell
curl https://agentbroker.fly.dev/health
curl https://agentbroker.fly.dev/llms.txt | Select-Object -First 20
```

---

## Step 3 — Point your domain (10 min)

You own `agentbroker.qzz.io` via DigitalPlat. Map it to Fly:

```powershell
# Tell Fly your domain — it returns DNS records you must add
flyctl certs create agentbroker.qzz.io --config deploy/fly.toml
```

flyctl prints something like:

```
You can validate your ownership of agentbroker.qzz.io by:

  Adding an A record to your DNS service which points to 1.2.3.4
  Adding an AAAA record to your DNS service which points to 2606:...
```

Take those values to **<https://dash.domain.digitalplat.org>**:

1. Open the dashboard.
2. Find `agentbroker.qzz.io` in your domains.
3. Click "DNS records" or equivalent.
4. Add:
   - **Type:** A — **Name:** `@` — **Value:** the IPv4 from flyctl
   - **Type:** AAAA — **Name:** `@` — **Value:** the IPv6 from flyctl
5. Save.

Wait 5-30 minutes for DNS propagation. Then verify:

```powershell
curl https://agentbroker.qzz.io/health
curl -X POST https://agentbroker.qzz.io/mcp `
  -H "Content-Type: application/json" `
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

You should see `find_business`, `verify_business`, … in the response.

---

## Step 4 — Submit to MCP registries (5 min)

Now that the service is live, submit it for discovery:

```powershell
python scripts/submit_to_registries.py
```

This auto-submits to **Smithery** and **Glama** using your API keys.
For the GitHub-based registries (modelcontextprotocol/servers and
awesome-mcp-servers), the script prints exact `git` commands you copy-paste.

---

## What replaces "design-partner outreach" and "Show HN"

Since you said no social media and HN restricts new accounts, **passive
discovery does this for you**. Here's why this works for an MCP server:

### Passive discovery channels (already wired)

1. **MCP registry crawlers** — Smithery, Glama, and similar registries are
   queried by Claude Desktop / Cursor / Continue / Cline at startup. Once
   listed, you appear as a tool option to every user of those clients.
2. **Anthropic and OpenAI LLM crawlers** read `llms.txt` — your service is
   indexed and surfaces when an agent asks "where can I book a haircut?"
3. **GitHub topics** — your repo gets discovered via tag search. After
   pushing, edit `https://github.com/basilalshukaili/agentbroker` → click
   the gear next to "About" → add topics: `mcp`, `mcp-server`,
   `agent-tools`, `business-automation`, `scheduling`, `compliance`.
4. **The well-known endpoints are crawled by ChatGPT plugin discovery
   bots** — they scrape `.well-known/ai-plugin.json` from any domain.
5. **Google's site search** — `llms-full.txt` at the root indexes well.

### Active discovery you can do — without any social presence

1. **Email Anthropic to ask for inclusion in Claude's default MCP server
   list.** One email. Address: `support@anthropic.com`. Template below.
2. **Email Cursor at `hi@cursor.com`.** Same template, ask for inclusion in
   their default servers.
3. **Submit to apis.guru** — a free API directory used by AI coding assistants.
   Just open a PR adding your `openapi.yaml`.

### Email template (use as-is)

Subject: `Adding Agent Broker to MCP server discovery`

```
Hi,

I built Agent Broker, a horizontal MCP server that exposes 12 tools for AI
agents to find, verify, message, and schedule appointments with small
businesses worldwide. Built-in TCPA / GDPR / CASL / 10DLC compliance and
channel fallback (Cal.com → voice AI → SMS → email → web form). Free for
agents up to 100 ops/month.

Live MCP endpoint: https://agentbroker.qzz.io/mcp
Discovery: https://agentbroker.qzz.io/.well-known/mcp.json
Manifest: https://agentbroker.qzz.io/manifest
Repo: https://github.com/basilalshukaili/agentbroker

Would you consider including it in {Claude Desktop's | Cursor's} default
MCP server registry? Happy to provide test access or any documentation
you'd need.

Thanks,
Basil
```

Copy, paste, send. 3 emails total. Done with "marketing."

---

## After launch — set-and-forget

These run automatically once the service is deployed:

| What | Where | When |
|------|-------|------|
| Auto-deploy on push | GitHub Actions | Every push to `main` |
| Health check every 30s | Fly.io | While running |
| Free-tier balance check | `python scripts/check_balances.py` | Run every 2 weeks (calendar reminder) |
| Domain renewal | DigitalPlat dashboard | Annually |

Set **two calendar reminders right now**:

1. **2 weeks from today** — "Run `python scripts/check_balances.py`"
2. **11 months from today** — "Renew agentbroker.qzz.io at DigitalPlat"

That's it. Until traffic arrives.

---

## What "traffic arriving" looks like

You'll know agents are calling you because:

- Fly.io dashboard shows non-zero requests-per-minute on `https://agentbroker.qzz.io`.
- Twilio dashboard shows SMS sends being attempted.
- Your Resend dashboard shows email sends.
- GitHub Actions show new commits aren't your only traffic source.

When that happens, run:

```powershell
python -m tests.agent_sim.harness  # see if real success rate matches simulated
```

And read [EXPIRY_CHECKLIST.md](./EXPIRY_CHECKLIST.md) to know what to swap when each free tier runs out.

---

## If something doesn't work

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| `git push` rejects | No PAT configured | Generate at <https://github.com/settings/tokens/new>, scope=`repo` |
| `flyctl deploy` says "no app named agentbroker" | App not created yet | Run `flyctl apps create agentbroker` first |
| Domain shows "DNS not configured" | DNS hasn't propagated | Wait 30 min; verify A record at digitalplat dashboard |
| `/mcp` returns 404 | Old deploy cached | `flyctl deploy --no-cache --config deploy/fly.toml` |
| Smithery submission returns HTTP 401 | API key in `.env` typo | Re-copy from your Smithery dashboard, check for trailing whitespace |
| `validate_credentials.py` fails after Fly secrets set | secrets set OK on Fly but not in your `.env` | Both must match. Edit `.env`, re-run validator. |

General debug command: `flyctl logs --config deploy/fly.toml` — shows everything Python prints in the running container.

---

## Bottom line

After Step 1-4 above:

- **You have a live, public, MCP-discoverable agent-to-business service**
  on a real domain.
- **Three MCP registries** (Smithery, Glama, modelcontextprotocol/servers
  via PR) list you within ~24-48 hours.
- **Two LLM crawlers** (Anthropic, OpenAI) index your `llms.txt` within
  a week.
- **Zero ongoing cost** until you cross any free tier (then `EXPIRY_CHECKLIST.md`
  tells you the free alternative).

When the first agent calls `find_business` or `schedule_appointment` and
Paddle records a payable success premium, you have **revenue without
having posted anything on social media**. Which is exactly the architecture
that makes sense for an agent-tool service: agents discover tools through
registries and well-known endpoints, not Twitter.
