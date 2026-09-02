# Registry / directory submission drafts

Nothing in this directory has been submitted anywhere. Every file here is a
draft, checked against current code truth (manifest/manifest.json,
billing/pricing.py, docs/PRICING.md, LICENSE, server.json, smithery.yaml,
glama.json) as of 2026-09-02, not against what these products claimed at the
pre-pivot MCP-booking-supply stage.

| File | Target site | Submission mechanism | Requirements | Status |
|---|---|---|---|---|
| `awesome-mcp-pr.md` | [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | PR against `README.md`, one line under the **🏢 Workplace & Productivity** section, alphabetical by name (per the repo's own `CONTRIBUTING.md`) | A GitHub account; no DNS/account verification. `🤖🤖🤖` in the PR title opts into the repo's agent-submission fast lane. | Draft ready, not submitted. |
| `hn-show.md` | [Hacker News](https://news.ycombinator.com/submit) | Direct form submission (Show HN), no PR | An HN account with enough karma/age to post (no other verification) | Draft ready, not submitted. |
| `mcp-servers-pr.md` | Officially, the [MCP Registry](https://registry.modelcontextprotocol.io/) (NOT a PR — see below) | **No manual submission needed.** `.github/workflows/publish-mcp.yml` auto-publishes `server.json` (and `registry/*/server.json`) to the registry via DNS auth whenever the `version` field changes on `main`. The file's original premise — a PR to `modelcontextprotocol/servers` README — is obsolete: that repo dropped its community-server list and now points at the registry API instead. | Nothing to request: `hatchloop.dev` is already DNS-verified and the signing key already lives in the `MCP_REGISTRY_DNS_KEY` repo secret. | **Already live**, not a pending draft — see the file for how to verify the current entry. Kept in this directory as reference copy in case a *different* directory ever asks for the same hand-written blurb. |
| `product-hunt-launch.md` | [Product Hunt](https://www.producthunt.com/posts/new) | Direct form submission, no PR | A Product Hunt maker account; gallery assets (hero image, screenshot, demo GIF, logo — listed as founder TODO in the file, none produced yet) | Draft ready, not submitted (and blocked on gallery assets). |
| `smithery.yaml` | [Smithery](https://smithery.ai/server/new) (registry acquired by Arcade.dev 2026-08-05 — re-verify the login/dashboard path still applies before using this) | PR to `smithery-ai/registry`, or paste directly into the Smithery web form | A GitHub account (for the PR route) or a Smithery/Arcade account (for the web form) | Already corrected to the canonical `hatchloop.dev` host in an earlier pass (see the file's own header note) and to MIT licensing; **not re-verified in this pass** since it was not one of the four files this task was scoped to. Re-check its tool list/quota language against `manifest/manifest.json` and `docs/PRICING.md` before submitting — it was not touched here. Not submitted. |
| `smithery-sync-2026-08-16.md` | N/A — this is a dated research/audit memo, not a submission draft | N/A | N/A | Historical record only. Explicitly states at its own top: "Nothing in this doc has been submitted anywhere." Do not treat as current without re-verifying its live-state claims (dated 2026-08-16). |

## What changed in this pass (2026-09-02)

The four files this task covered (`awesome-mcp-pr.md`, `hn-show.md`,
`mcp-servers-pr.md`, `product-hunt-launch.md`) all carried pre-pivot numbers:
a workers.dev endpoint no longer canonical, "23 tools" described purely as a
booking/messaging layer, a flat "100 ops/month" quota, `$1.00` per confirmed
booking, and (for the official-registry file) a submission mechanism that no
longer exists. All four are rewritten to lead with the two differentiators
that are actually true today — Ed25519-signed, offline-verifiable compliance
receipts, and 15 no-key tools backed by real official-source data (OFAC/EU/UK
sanctions lists, GLEIF, SEC EDGAR, USASpending) — and to use only numbers that
match the code: 23 tools total, 15 usable with no key (12 always-free + 3
free within a daily quota), 8 write tools gated behind a free key (100 write
ops/day), MIT license, canonical endpoint `https://hatchloop.dev/mcp/agent-broker`.
