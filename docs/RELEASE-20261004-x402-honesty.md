# Release note: every payment claim follows the switch that decides it (2026-10-04)

Branch `feat/x402-honesty-20261004`, based on `4e46f8e` (the tooling commit above the live build `48e8b62`).
Nothing here changes what a call costs or who can make one. It changes what the service SAYS about how to
pay, so that no surface describes a rail whose switch is off.

## The defect, measured

In the running container (2026-10-03/04, read with `check_row358_money_path_apply_and_deploy.py` and a live
`preview_cost`): `CREDITS_ENABLED=off`, `DATA_METERING_ENABLED=off`, and `X402_ENABLED` not set at all.

| Surface | Said | Was true |
|---|---|---|
| `/.well-known/mcp.json` `payments` | `status: "active"`, `rails: ["credits"]` (literals) | no rail on; no call charged; a Polar purchase grants no credits |
| `auth_required` text | "Option 2 (credits) ... Option 3 (pay per call): attach an x402 payment" | both switches off; a payment attached to a call is ignored |
| `free_tier_daily_limit_exceeded` | "Buy credits at hatchloop.dev/pricing for no daily cap" | credits off |
| `GET /keys/request`, the 503 | "Pay per call with x402" | x402 off |
| `/checkout` page | "Two rails ... pay per call in USDC on Base via x402" | x402 off |
| premium-data note | "up to a daily quota, then cost credits" | metering off: those tools are free and unmetered |
| `/openapi.json` | listed `/.well-known/x402` | route is a 404 while the rail is off |
| README, `docs/PRICING.md`, `llms-install.md` | x402 "live since 2026-08-29"; premium data "then $0.02/call via credits" | switched off |

## The rule, and where it lives

`billing/switches.py` is the ONLY module that reads `CREDITS_ENABLED` and `DATA_METERING_ENABLED`
(`tests/unit/test_billing_switches_are_the_only_readers.py` fails if another one appears). The gates
(`mcp_server`, `polar_webhook`, `preview_cost`, the REST credits middleware) and everything that
describes them call the same functions, so "the gate runs" and "we say it runs" are one expression.
x402 keeps its own gate, `billing.x402_gate.enabled()` (flag + receiver + CDP credentials); `switches`
delegates to it.

What now follows the switches:

- `payments.status` (`active` / `not_enabled`), `payments.rails`, the new `payments.premium_data_quota_enforced`,
  and the closing sentences of `payments.note`. The access lists (`free_tools`, `quota_free_tools`, ...) are a
  classification and do not move.
- The `auth_required` options are built as a list and numbered as they come (Option 1 free key always;
  credits and x402 only while their gate runs), and `how_to_resolve` carries `credits` / `x402` only then.
- The daily-limit message carries its upgrade pointer only while credits are on.
- `GET /keys/request` and its 503, the `/checkout` page, tool descriptions (`[or pay per call: x402 ...]`),
  `/.well-known/x402` (404 when off) and the OpenAPI schema (the route is hidden from it).
- `scripts/check_pricing.py` now refuses a literal rails list in any spelling, a literal `"status": "active"`
  and hand-numbered payment options, and checks `agent_interface/key_requests.py` too.
- A bare price request (`_meta["x402/payment"]` = any string) no longer pages the founder as "a real buyer";
  only a structured payment payload does (`billing/x402_gate._is_signed_payment_attempt`).

## Second pass (review of the first pass; every item has a test that fails without it)

The review found the first pass left the same defect standing on surfaces it had not looked at. Fixed:

- **Cost and quota claims.** With metering off, the three premium data tools are free and unmetered (the
  descriptor says `premium_data_quota_enforced: false`), yet `tools/list` tagged them "[free in quota, then
  $0.02/call]" and `llms.txt` / `llms-full.txt` said "free within the daily quota, then $0.02 per call". They now say
  free, and the quota wording returns only while `DATA_METERING_ENABLED` is on. The manifest's static
  `free_quota_note` no longer states the quota flatly (it is conditional and points at the live descriptor).
- **A price is a schedule while no rail can charge it.** With no rail on, a priced tool's tag, its `Cost:` sentence in
  `llms.txt` / the tool JSON, and `preview_cost`'s basis say "not charged while no payment rail is on". The dollar
  figures do not move (receipts and the x402 gate read the same schedule). One sentence, `billing.switches.NOT_CHARGED`.
  **This labelling is a product-wording call; it is reversible in one place** (`not_charged_note()` returns `""`).
- **x402 on, metering off.** The three data tools are answered free before the x402 branch, so they are no longer tagged
  "[or pay per call: x402 ...]" in that state (a payment attached to one was never read).
- **`/checkout` and `/billing/checkout`.** With credits off the page no longer sells credits (no package table, no
  pay button, no "bought by card") and `/billing/checkout` redirects to `/checkout` instead of minting a Polar session:
  a purchase made while credits are off mints a key but is never credited (the grant is skipped and is idempotent on the
  order id). "Two rails" needs both rails; x402 alone reads "One rail". The page, the link and the webhook's grant all
  call `billing.switches.credits_enabled()`.
- **Other surfaces that sold credits:** the free-key success page, the past-quota messages of the premium data tools
  (reachable in "metering on, credits off", the first-flip state) and the OAuth consent page are now gated on credits.
  `smithery.yaml` / `glama.json` (generated from `registry/servers.yaml`) are static, so they say nothing about credits or
  a quota and point at the live descriptor. `scripts/check_pricing.py` now polices these generators and refuses an offer
  of credits or a quota promise in static copy; its option and status rules no longer depend on how a literal is quoted.
- **The buyer-intent predicate is the SDK's own parse** (`x402.mcp.utils.extract_payment_from_meta`), so it cannot disagree
  with the SDK in either direction: a valid payment sent as a JSON string is counted; `{"payload": {"a": 1}}` is not.
- **`live_verify_release.py payments` requires positive evidence.** A 500 from `/keys/request`, a failed or empty
  `tools/list`, an unreadable `preview_cost`, an unreadable `send_message` refusal, or a `/.well-known/x402` that does not
  answer 404 while the rail is off each fail the check. Tool descriptions are judged per tool against the descriptor (the
  quota tag, the x402 mention on the data tools, "not charged" versus a live rail). The receipt also says whether the
  committed edge snapshot's payments block is in step (informational).
- **Gate side pinned.** Three mutations survived the first pass (the data-tool bypass, the REST credits middleware, the
  data block of `check_quota`); `tests/unit/test_switch_gates_are_pinned.py` kills all six directions.

## Third pass (the second review; every item has a test that fails without it)

One P1, five P2 and several P3 were verified against the second pass. Fixed:

- **P1: the money path.** `hatchloop.dev/pricing` (the separate Next.js site, not in this repo) links every credit package to
  `/portal?package=starter|growth|scale`, and said "Buy credits for write actions", while `CREDITS_ENABLED` is off. The
  portal's button POSTs `/portal-api/topup`, which the site proxies to `agent_interface/portal.py` in THIS repo, and that
  route minted a Polar checkout for a package that would never be credited (the webhook skips the grant while credits are
  off, idempotent per order id). It now refuses with `credits_not_enabled` while `billing.switches.credits_enabled()` is
  false (the portal page shows its existing "Top-up packages are not yet configured" note), the same expression the grant
  and `/billing/checkout` use. **The page copy itself is still a founder decision** (see below), and
  `live_verify_release.py payments` now reads `/pricing` and fails while it links a purchase with no credits rail.
- **P2: quota wording.** `free_tier_sentence()` / `auth_note()`, the `initialize` instructions, `GET /keys/request` and its 503,
  the agent-service text and the sanctions door's description all said "free within a daily quota" while the quota is not
  enforced. The words now come from `billing.switches.free_quota_clause()` ("free within a daily quota" only while metering is
  on, otherwise "free and unmetered at this time"); the door's description is static and says only "No key needed", so it is
  true in every state. `payments.note` no longer says "then cost credits" or "spend credits once past any quota": the clause
  after "callable with no key" is `switches.premium_data_terms()` and the second count is "carry a list price".
- **P2: past the quota with no rail on.** With metering on and credits and x402 off, tags, `Cost:` sentences and
  `preview_cost` said "then $0.02/call, not charged while no payment rail is on", which reads as "the call carries on, free".
  `consume_data_quota` refuses it (`free_quota_exceeded`, nothing dispatched). They now say "free in quota, then refused
  until the quota resets" (`switches.QUOTA_REFUSED`) and quote the price only while a rail can charge it. A test drives the
  real gate so the wording is tied to what the gate does.
- **P2: x402 discovery with metering off.** `/.well-known/x402` listed the three premium data tools at $0.02 although a
  payment attached to one is never read (the data-metering bypass answers first). `discovery_document()` omits them while
  metering is off; a test asserts the document and `tools/list` name the same set of tools in both states.
- **P2: README.** The "Free-key email delivery" row and the blockquote said no email provider is configured and that
  `GET /healthz/external` reports `resend: not_configured`. Measured 2026-10-04: `resend: ok` (full-access key),
  `twilio: not_configured` (the reason that endpoint's overall status is `fail`). Corrected, pointing at the live endpoint
  instead of freezing a state; a test keeps the false sentences out.
- **P3, same files:** the live check also scans `/keys/request`, `initialize` and every tool description for quota and
  credits wording, parses the `/.well-known/x402` body, and no longer fails an honest server that numbers no options (it
  judges the refusal by `error_code` and `how_to_resolve.free_key`). The pricing checker now reads Python generators as a
  syntax tree (every respelling the reviewers showed passes, plus `switches.live_rails() + ["x402"]`, which an early version
  of the rule would have exempted). The buyer-intent alert says "payment attempt (unverified)": the predicate checks a
  payload's shape, not its signature. Two surviving mutants are pinned (`preview_cost` for the data tools; the `/checkout`
  write-tool note in every rail combination).

Not changed here: the Cloudflare edge worker is still not in the live path, and five origin-derived snapshots are still stale
against the new wording (see the next section).

## Edge snapshot

`edge/src/snapshots/mcp.json` is regenerated (`refresh_edge_snapshots.py --local-routes mcp.json`): it is the
only snapshot that carries `payments`. `mcp-tools-list.json` is regenerated too (`--local-tools`): its cost tags changed with
the second and third passes, and it also picks up the Arabic-script description and schema of `screen_sanctions` that the live
origin already serves. `tests/unit/test_edge_snapshots_follow_the_payments_block.py` is a ratchet: a snapshot not on its
`KNOWN_STALE` list may not contradict the payments block, and a listed one that gets refreshed must be removed from the list. Seven other snapshots were ALREADY stale against local code at `4e46f8e`
(agents, openai-tools, anthropic-tools, manifest, llms, llms-full, openapi); they are not touched here.
The snapshot is the all-switches-off document, which is what the origin serves today; after any switch is
flipped on, regenerate it (or `refresh_edge_snapshots.py --check` against the live origin will say it is stale).

The six snapshots on that list (`anthropic-tools`, `openai-tools`, `llms`, `llms-full`, `manifest`, `mcp-initialize`) are
refreshed from the live origin (`python scripts/refresh_edge_snapshots.py`), so that has to wait for this release to be
deployed. The edge worker is not in the live path today (neither `api.hatchloop.dev` nor `hatchloop.dev` answers with
`x-edge-source`), so the snapshot is latent, not live. **Procedure for flipping any money switch:** flip it, redeploy,
then `python scripts/refresh_edge_snapshots.py --local-routes mcp.json` (without `--check`, rewrites it) and commit; the
`payments` receipt field `edge_snapshot_payments_in_step` says whether they agree. Tool-definition and discovery snapshots
(`mcp-tools-list.json`, `llms*.txt`, `openai-tools.json`, `anthropic-tools.json`, `manifest.json`, ...) still carry the old
cost wording; refresh them before the edge worker is next deployed.

## Deploy preconditions (read before shipping)

1. The parent repo commit `9703a51` (branch `feat/x402-honesty-deploy-env-20261004`, `scripts/check_deploy_env.py`)
   makes the deploy stage `X402_ENABLED`, `CREDITS_ENABLED` and `DATA_METERING_ENABLED` BY NAME. Without it,
   centralising the reads in `billing/switches.py` would silently drop `CREDITS_ENABLED` and `DATA_METERING_ENABLED`
   from the staged env file (the deriver cannot see a variable argument). Merge it first. The secret-drain route list
   needs the same name (`ops` repo, branch `feat/x402-switch-route-20261004`, commit `a66fc1b`): its drift guard
   (`tests/test_secret_drain_multi_target.py`) fails for any staged name with no route to `agentbroker.env`.
2. `projects/hatchloop/.env` must then DEFINE `X402_ENABLED` (`false` is the correct, explicit value today); the
   deploy script stops with "no value for: X402_ENABLED" otherwise. Both other switches already have `false`.
3. No SQL migration. No new required variable beyond that one.

## Verifying after the deploy

**Expect the `payments` check to FAIL on the pricing page until the founder decision below is made.** It now reads
`hatchloop.dev/pricing`; while that page links `/portal?package=...` or says "Buy credits" and `CREDITS_ENABLED` is off, the
check reports exactly that. It is the alarm for the one claim this repo cannot fix, not a defect in the deploy. The portal
route behind those links no longer takes a payment (above).

`python scripts/live_verify_release.py --expect-commit <sha> --only payments --expect-rails ""` (empty string =
"no rail should be listed", the state while `CREDITS_ENABLED` and `X402_ENABLED` are off). The `payments` check
reads only. It fails when:

- any surface offers a rail the descriptor does not list (or omits one it does), or `/.well-known/x402` is served without
  the rail, or is listed but its body is not a usable document or prices a tool that is answered free;
- the premium-data quota flag disagrees with `preview_cost` for `screen_sanctions`, or a quota promise appears on any
  surface (`/keys/request`, `initialize`, a tool description) while the descriptor says it is not enforced;
- a credits offer appears on any of those surfaces while credits is not a rail, or the site's `/pricing` page links a
  credit-package purchase (`/portal?package=...`) or says "Buy credits" while credits is not a rail;
- a tool description quotes a price as a charge while no rail is on, offers x402 on a premium data tool while metering is
  off, says "not charged" next to a quota, or (rail on) says a call past the quota is refused;
- with `--expect-rails`, the descriptor names a different set than the one that was staged;
- any surface it needs cannot be read (including the pricing page and the `initialize` answer).

Run against `48e8b62` before this release it fails on the two x402 offers and the missing `premium_data_quota_enforced`
field, on the quota tags, and on the credits rail once `--expect-rails ""` is given.

## Not changed on purpose

Legal text (`/terms`, `/refund`: "Payments settled on-chain via x402 are final"; the refund policy's credit-package
window), the `/releases` changelog (history), `hatchloop.dev/pricing` and the rest of that Next.js site (not in this
repo; a Polar purchase link there is the same trap as `/billing/checkout` was - see the founder decision below),
`render_home` / `render_pricing` in `web/pages.py` (dead code behind redirects), and the registry-submission scripts under
`deploy/` and `scripts/submit_to_registries.py` (static marketing copy; regenerate from the live descriptor before any
use). The six origin-derived edge snapshots on the ratchet list stay stale until they can be refreshed from the deployed
origin (see above).

## Founder decisions this release surfaces (not made here)

1. **Credits, and the pricing page.** While `CREDITS_ENABLED` is off, nothing can be bought on this service: `/billing/checkout`
   and the portal's `/portal-api/topup` both refuse. What is left is copy on `hatchloop.dev/pricing` (the Next.js site in
   `projects/hatchloop/web_hatchloop_v2`, which ships from the working tree and is not tracked in this repo): its meta
   description and 12 package links still sell credits, and a visitor who follows one signs in and then reads "Top-up packages
   are not yet configured. Available at launch." Either switch `CREDITS_ENABLED` on first, or hide the package offers and links
   there until it is. That edit is deliberately not made from this branch: it changes the public site, and the site deploys itself.
2. **"Not charged while no payment rail is on"** now appears next to every list price while no rail is on. It is true; if
   you would rather the schedule not say so, `billing.switches.not_charged_note()` is the one place to change.
