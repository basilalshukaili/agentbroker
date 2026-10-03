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

## Edge snapshot

`edge/src/snapshots/mcp.json` is regenerated (`refresh_edge_snapshots.py --local-routes mcp.json`): it is the
only snapshot that carries `payments`. Seven other snapshots were ALREADY stale against local code at `4e46f8e`
(agents, openai-tools, anthropic-tools, manifest, llms, llms-full, openapi); they are not touched here.
The snapshot is the all-switches-off document, which is what the origin serves today; after any switch is
flipped on, regenerate it (or `refresh_edge_snapshots.py --check` against the live origin will say it is stale).

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

`python scripts/live_verify_release.py --expect-commit <sha> --only payments --expect-rails ""` (empty string =
"no rail should be listed", the state while `CREDITS_ENABLED` and `X402_ENABLED` are off). The `payments` check
reads only: it fails when any surface offers a rail the descriptor does not list (or omits one it does), when
`/.well-known/x402` is served without the rail, when the premium-data quota flag disagrees with `preview_cost`
for `screen_sanctions`, and, with `--expect-rails`, when the descriptor names a different set than the one that
was staged. Run against `48e8b62` before this release it fails on the two x402 offers and the missing
`premium_data_quota_enforced` field, and on the credits rail once `--expect-rails ""` is given.

## Not changed on purpose

Legal text (`/terms`, `/refund`: "Payments settled on-chain via x402 are final"), the `/releases` changelog
(history), the human `/checkout` page's credit-package half, `hatchloop.dev/pricing`, per-tool cost tags
(`[free in quota, then $0.02/call]`), and the registry-submission scripts under `deploy/` and
`scripts/submit_to_registries.py`. Each still states the price schedule, not the switch state.
