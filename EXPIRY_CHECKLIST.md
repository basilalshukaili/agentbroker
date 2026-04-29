# Free-Tier Expiry Checklist

> **Bookmark this file.** Every free-tier service has a clock or limit.
> When one runs out, switch to the listed free alternative — never pay
> until traffic justifies it.

Last updated: 2026-04-29.

---

## What you're using right now (all free)

| Service | What it does | Free limit | Hard expiry | Watch this |
|---------|--------------|-----------|-------------|------------|
| **Fly.io** | Hosts the app | 3 shared VMs · 3GB volume | none (forever-free if usage stays under) | $5 credit/mo absorbs minor overages |
| **DigitalPlat (qzz.io)** | Domain `agentbroker.qzz.io` | unlimited | annual renewal (free) | Renew yearly via dashboard |
| **GitHub** | Repo + Actions | unlimited public repos · 2,000 Action min/mo | none | Action minutes only |
| **Twilio** | SMS + voice | $15.50 trial credit | depleted on first 200 SMS | When credit hits $0 |
| **Cal.com** | Booking direct API | unlimited on free plan | none | None |
| **Vapi** | Voice AI calls | $10 free credit | depleted on first ~50 calls | When credit hits $0 |
| **Resend** | Transactional email | 3,000 emails/month · 100/day | resets monthly | Daily quota |
| **Paddle** | Billing | free until you make a sale | none | Per-sale 5% + $0.50 |
| **Polar.sh** | Backup billing | free | none | Per-sale 4% + $0.40 |
| **Smithery** | MCP registry listing | unlimited | none | None |
| **Glama** | MCP registry listing | unlimited | none | None |

---

## What to do when each runs out

### Twilio trial credit depletes (~200 SMS or ~30 voice mins)

**Symptom:** SMS sends start failing with `auth_failed` or `insufficient_funds`.

**Free alternatives (no Twilio replacement needed first month):**
1. **Switch to voice-only** — Vapi handles voice; SMS is optional. Add a UI flag to disable SMS channel temporarily.
2. **Use Resend (email) as primary** — many transactional flows work via email. Update `_FALLBACK_CHAINS` in `reliability/channel_fallback.py`.
3. **Sign up for Plivo** ($25 trial credit, works in Oman) as a Twilio alternative. Wire as a second adapter.
4. **MessageBird** — €25 trial, also Oman-friendly.
5. **Vonage** — $2 free trial. Last resort.

**Monitoring:** add this to your weekly check (`scripts/check_balances.py` — see below).

### Vapi credit depletes (~50 calls)

**Symptom:** Voice calls fail with billing errors.

**Free alternatives:**
1. **Bland AI** — has a free tier (~$5 credit), drop-in replacement for Vapi (similar HTTP API).
2. **Retell AI** — sometimes runs free credit promotions.
3. **OpenAI Realtime API + Twilio** — DIY route, technical work but pure pay-as-you-go.
4. **Pin to email-only fallback** — voice channel temporarily disabled.

**To swap:** add a `BlandVoiceAdapter` in `channels/voice_ai/bland.py` (similar shape to `vapi.py`) and switch the channel name in `_FALLBACK_CHAINS`.

### Resend monthly quota (3,000 emails) — resets the 1st of each month

**Symptom:** Emails 3,001+ return HTTP 429 / quota exceeded.

**Free alternatives:**
1. **Brevo (formerly Sendinblue)** — 300 emails/day free (= 9,000/month). Wire as `channels/sms_email/brevo_email.py`.
2. **Mailgun** — 100 emails/day free for 30 days; then 1,000/month free if billing details added (no charge).
3. **SendPulse** — 12,000 emails/month free.
4. **Self-host Postal or Mautic** — full control, free; needs a VPS, more work.

**Switch by:** running a feature flag — alternate adapters per request based on which quota has remaining headroom.

### Fly.io free tier ($5 credit covers ~3GB egress + always-on machine)

**Symptom:** Bill shows up. Free credit was used up.

**Free alternatives:**
1. **Render free web service** — already configured in `deploy/render.yaml`. Switch in 10 min.
2. **Railway** — has a $5 trial credit. `deploy/railway.json` ready.
3. **Cloudflare Workers** — free tier includes 100k req/day, but requires a small refactor (different runtime).
4. **Koyeb** — generous free tier. Docker-compatible.
5. **GCP Cloud Run** — 2M req/month free.

**Monitoring:** Fly emails when you cross 80% of free allowance. Don't ignore that email.

### DigitalPlat domain — annual renewal

**Symptom:** Domain stops resolving on the anniversary.

**Action:**
- DigitalPlat free domains renew yearly via the dashboard. Add a calendar reminder for 11 months from launch.
- **Backup options:** if DigitalPlat ever goes away — Freenom (`.tk`, `.ml`), or a $1.50/year `.xyz` domain at Porkbun.
- **When you have revenue:** buy a real domain at Cloudflare Registrar (`agentbroker.io`, ~$30-50/year).

---

## Auto-monitoring: a 1-minute weekly script

Save the following as `scripts/check_balances.py` and run it weekly. It pings each
service's billing endpoint and reports remaining credit.

```python
# scripts/check_balances.py
"""Weekly check — does any service have less than 7 days of credit remaining?"""
import asyncio, os, httpx
from scripts.validate_credentials import _load_dotenv
_load_dotenv()

async def twilio_balance():
    sid, tok = os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", "")
    if not sid: return None
    async with httpx.AsyncClient(auth=(sid, tok), timeout=10.0) as c:
        r = await c.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json")
    return float(r.json().get("balance", 0)) if r.status_code == 200 else None

async def main():
    bal = await twilio_balance()
    if bal is not None:
        marker = "OK" if bal > 2 else "LOW"
        print(f"[{marker}] Twilio balance: ${bal:.2f}")
    # Vapi and Resend quota checks: their dashboards expose them; add when their APIs do too.

asyncio.run(main())
```

Add this to your fortnightly calendar: **"Check service balances — `python scripts/check_balances.py`"**.

---

## How to defer paying for as long as possible

The order in which costs would arrive — and how to push each one back:

1. **Twilio depletion** (most likely first) — at ~$0.0075 per SMS, $15.50 lasts ~2,000 SMS. **Push back by:**
   - Defaulting to email (Resend) for all transactional confirmations.
   - Only using SMS when an SMB explicitly opted into SMS.
   - Adding a per-day SMS budget cap in `reliability/channel_fallback.py`.

2. **Vapi depletion** (~$10 = 50 calls) — **push back by:**
   - Routing booking attempts to direct-API channels first (Cal.com, Calendly).
   - Voice only as last fallback before web-form scrape.
   - Limiting voice calls to ≤30 seconds via Vapi assistant config.

3. **Resend monthly cap** — only matters above 3,000 emails/month. **Push back by:**
   - Sending one combined daily summary instead of N transactional emails per day.
   - Setting `RESEND_DAILY_LIMIT=80` (10% safety margin) in env.

4. **Fly.io overage** — only matters above ~3GB egress/month. **Push back by:**
   - Setting `min_machines_running = 0` in `fly.toml` (DONE).
   - Caching the manifest endpoint at the CDN edge (Cloudflare in front of Fly).
   - Compressing responses (FastAPI `gzip` middleware).

If you do all four, **first $0 in cost** likely lasts **3-6 months** at 100 ops/day pace.

---

## Rules of thumb

- **Don't pay anything until total monthly traffic exceeds 1,000 ops.**
- **Don't pay more than 5% of revenue on infrastructure** (so don't pay for a $50/mo Postgres until you have $1,000+ in monthly revenue).
- **The first paid upgrade is usually a real domain** (~$30/year) — that's the only one I'd pay before having traffic.

---

## When you do start paying — ranked priority

| Priority | Service | When | Cost |
|----------|---------|------|------|
| 1 | Real domain (`.io` / `.dev`) | Once you have ≥3 paying customers | $30-50/year |
| 2 | Twilio paid ($20 starter) | Once SMS exceeds 2,000/month | ~$15/month base |
| 3 | Postgres (Neon free → paid) | Once in-memory data loss bites | $0 → $19/mo |
| 4 | Sentry / observability | Once first paying customer files a real bug | $0 (free tier) → $26/mo |
| 5 | Fly.io paid plan | Once free credits exhausted | $5-15/month |
| 6 | Resend paid | Once you exceed 3,000 emails/month | $20/mo for 50k |

**Keep this list. Tick items as they trigger.** Don't pre-pay any of them.
