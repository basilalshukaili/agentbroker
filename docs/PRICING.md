# Pricing

> **Single source of truth: [`billing/pricing.py`](../billing/pricing.py).**
> Every number below is derived from it. If this file and that table ever
> disagree, the table is right and this file is a bug — run
> `python scripts/sync_manifest_pricing.py --check`.

**1 credit = 1 US cent.** There is no subscription, no monthly fee, and no
minimum. Credits do not expire.

> **This file is the price schedule. Whether a rail is switched on is a separate
> fact, and it is not typed here.** Three switches decide it: `CREDITS_ENABLED`
> (calls are charged to a credit balance and purchases grant credits),
> `DATA_METERING_ENABLED` (the premium-data daily quota is enforced) and
> `X402_ENABLED` (pay-per-call USDC is accepted). The running service reports
> them in `/.well-known/mcp.json` (`payments.status`, `payments.rails`,
> `payments.premium_data_quota_enforced`), derived from the same function the
> gates evaluate (`billing/switches.py`).
> **As of 2026-10-04 all three are off on the running service**: no call is
> charged, the premium-data quota is not enforced, and a payment attached to a
> call is ignored.

---

## What is free

**13 tools are free and unmetered; 11 of them need no key.**

The 11 that need no key: `find_business`, `verify_business`,
`check_booking_link`, `check_compliance`, `preview_cost`, `get_status`,
`get_outcome`, `self_test`, `check_quota`, `mint_key`, `lookup_us_contracts`.
(`mint_key` takes no key but refuses anyone who cannot sign with the
machine-mint secret; the way to a free key without it is `POST /keys/request`.)

The other two, `get_conversation` and `import_booking_url`, cost nothing and
need a free key. With the three premium data tools below (free within a daily
quota, no key), that makes 14 tools usable without signing up.

An agent can discover businesses, pre-check a booking link, preview what an
action would cost, check its quota, and read the outcome of its own operations
without ever authenticating or spending anything.

**`get_conversation` is free but not keyless.** A
thread is readable only by the agent identity that opened it, so a call with no
key is refused rather than answered: a request reference is four digits and a
business number is public, which is not a secret worth treating as one. Send
the same `X-Agent-Identity` key you send with `send_message` and it costs
nothing. Two consequences worth stating plainly: a thread opened WITHOUT a key
cannot be read back by id afterwards (its replies still arrive through the
inbound webhook), and threads opened before this rule shipped have no recorded
owner, so they are released to nobody.

`get_status` and `get_outcome` stay keyless, and "its own operations" is now
enforced rather than described: an operation created with an identity is
readable only by that identity. An operation created with NO identity at all
is released to nobody, not even the caller who omitted one — an unrecorded
owner is indistinguishable from an owner an earlier bug silently dropped, and
guessing which one happened would be a takeover primitive, not a convenience.
Send `X-Agent-Identity` when you create the operation and you can poll it
back; a truly anonymous booking or call can no longer be polled by
`operation_id` alone.

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

While `DATA_METERING_ENABLED` is off (as of 2026-10-04) there is no quota: these
three tools run free and unmetered for everyone, and `preview_cost` reports
$0.00 for them.

## Write tools — free tier, then credits

The 8 write tools perform real outbound actions, so they require an
`X-Agent-Identity` key.

| Tier | Cost | Limit |
|---|---|---|
| Free email-verified key | $0 | 100 write ops/day |
| Credits | see packages | no daily cap |
| x402 (USDC on Base) | per call, same prices as credits | no signup, no daily cap |

The x402 rail was enabled on 2026-08-29 and is **switched off on the running
service as of 2026-10-04** (`X402_ENABLED` is not set there). While it is on:
attach a signed payment in `params._meta["x402/payment"]` on a `tools/call` and
the call is served with no key and no account — the server answers an unpaid
attempt with a priced offer first. It is the one payment path an autonomous
agent can complete without a human. Every surface that mentions it (tool
descriptions, `auth_required` text, key-request guidance, the discovery
documents, `/.well-known/x402`) is derived from the gate and appears only while
it is on.

A call that fails, and a call that succeeds without doing billable work (a
duplicate lead, a booking that was not made — any receipt whose `cost.amount`
is 0), is verified but **not settled**: no USDC moves and the receipt says so
in `x402.settled_usd`.

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
| `call_business` | 20 | $0.20 — deliberately below our ~$0.30 vendor cost while we build trust |

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
