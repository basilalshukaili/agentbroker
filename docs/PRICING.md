# Pricing

> **Single source of truth: [`billing/pricing.py`](../billing/pricing.py).**
> Every number below is derived from it. If this file and that table ever
> disagree, the table is right and this file is a bug — run
> `python scripts/sync_manifest_pricing.py --check`.

**1 credit = 1 US cent.** There is no subscription, no monthly fee, and no
minimum. Credits do not expire.

---

## What is free

**8 utility tools — free, no key, unmetered.**

`find_business`, `verify_business`, `check_booking_link`, `check_compliance`,
`preview_cost`, `get_status`, `get_outcome`, `self_test`

An agent can discover businesses, pre-check a booking link, preview what an
action would cost, and read the outcome of its own operations without ever
authenticating or spending anything.

`get_conversation` is also free — it just needs your key, because it returns
only the threads you started.

## Premium data tools — free up to a daily quota

`verify_company_record` (GLEIF LEI + SEC EDGAR) · `screen_sanctions`
(OFAC SDN + EU Consolidated + UK Sanctions List) ·
`map_trade_restriction` (OFAC embargoes + export-control Entity List)

| Caller | Free per day | Beyond the quota |
|---|---:|---|
| Free email-verified key | 500 | $0.02/call |
| Anonymous (no key) | 100 | $0.02/call |

Past the quota the tool returns an honest failure
(`reason_code: free_quota_exceeded`, `cost: $0`) — never a silent charge.

## Write tools — free tier, then credits

The 8 write tools perform real outbound actions, so they require an
`X-Agent-Identity` key.

| Tier | Cost | Limit |
|---|---|---|
| Free email-verified key | $0 | 100 write ops/day |
| Credits | see packages | no daily cap |
| Crypto | not offered - built and proven in testing, currently disabled |  |

Get a free key at <https://hatchloop.dev/agent-broker>.

### Credit packages

| Package | Price | Credits | Effective |
|---|---:|---:|---|
| Starter | $9 | 1,000 | connect your first agent |
| Growth | $29 | 3,500 (~20% bonus) | regular agent workflows |
| Scale | $99 | 13,000 (~30% bonus) | production volume |

Buy at <https://hatchloop.dev/pricing>.

### Per-operation cost

Derived from `billing/pricing.py`. Variable operations reserve the maximum and
settle the actual cost from the receipt — call `preview_cost` for the exact
figure before committing.

| Operation | Credits | USD |
|---|---:|---:|
| `send_message` | 2 (up to 22) | $0.02 – $0.22 |
| `send_transactional_confirmation` | 2 | $0.02 |
| `handle_inbound` | 3 | $0.03 |
| `capture_lead` | 5 | $0.05 |
| `schedule_appointment` | 15 (up to 50) | $0.15 – $0.50 |
| `escalate_to_human` | 20 | $0.20 |
| `import_booking_url` | 0 | free — adoption wedge |
| `call_business` | 0 | free until voice billing is enabled |

WhatsApp sends currently cost us nothing, so they cost you nothing.

---

## What we do **not** promise

This section exists because an earlier version of this file advertised a
$49/mo "Developer" tier and a $499/mo "Business" tier with uptime SLAs,
latency targets and refund-on-miss clauses. **None of that was ever in
effect.** A public document promising an SLA we do not honour is worse than no
document, so it is recorded plainly here:

- **No SLA.** No uptime guarantee, no latency guarantee, no refund clause.
- **No subscription tiers.** No monthly fee at any level.
- **No priority queue** and no paid support tier. Email
  <hello@hatchloop.dev> — we read every message, with no response-time promise.

## Explored, not in effect

Kept so the product thinking is not lost. `billing/pricing_tiers.py` models
these; it is **not wired into any live billing path** (tests import it, nothing
in production does):

- outcome-based premiums for confirmed bookings
- supply-side listing revenue (businesses paying for placement)
- anonymised demand analytics sold back to businesses

Reviving any of these means wiring it to `billing/pricing.py` first, so there
is still exactly one price table.

---

## Direction

Our standing strategy is to be **generous first**: an irresistible price that
builds trust and reliance, raised later only with an explicit thank-you notice
and a hardship escape hatch for anyone the change hurts. Money is not the goal
in this phase. That is why the free quotas are large and why every read tool is
unmetered.
