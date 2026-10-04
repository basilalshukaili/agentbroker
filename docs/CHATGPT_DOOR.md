# The ChatGPT-only door: `/mcp/chatgpt`

Added 2026-10-04 on branch `feat/chatgpt-free-door-20261004` (base `4e46f8e`). Not merged to `main`.

## Why a separate door

OpenAI's plugin guidelines (Commerce and monetization) do not allow a listed plugin to sell digital goods,
subscriptions, tokens or credits, directly or through a freemium upsell; to show plans or promote upgrades; or to link to
a page that starts a purchase. Everything the three Claude-facing doors do around their data tools is that:
a `[free in quota, then $0.02/call]` tag on every description, "call `preview_cost` first" in the handshake, and an
over-quota answer that links to a page with Buy buttons. Pricing text is fine in Claude's directory, so those doors are
unchanged and ChatGPT gets its own (`projects/hatchloop/docs/directory-kit-2026-10/ISSUES-BEFORE-SUBMITTING.md` section 13,
"What AgentBroker has to change").

## The URL, which cannot change after submission

OpenAI treats a changed path as a new version and a changed origin (scheme, host, port) as a support ticket.

* **Path: `/mcp/chatgpt`.** Do not rename it.
* **Host: submit `https://api.hatchloop.dev/mcp/chatgpt`, and only that.** It answers the moment the release is
  deployed, with no change to Caddy or the site. `https://hatchloop.dev/mcp/chatgpt` is not served: the Caddy `mcp_direct`
  block (marker `mcp_direct 2026-10-01`) is already applied on the box, `mcp_direct.apply` returns an applied file
  untouched, so running `install_mcp_direct.py` again adds nothing, and the site answers that path itself. (An earlier
  version of this page said a re-apply would include the door; that was wrong, found in review on 2026-10-04.)
  `install_mcp_direct.py` routes the listed doors only. Serving the door on hatchloop.dev would take a separate Caddy
  change with its own marker pair, as `deploy/caddy/mcp_retired.py` did for the retired doors; it is not built and
  nothing here needs it.
* **Domain challenge.** OpenAI's portal generates a plain-text token to serve at `/.well-known/openai-apps-challenge` on the
  MCP host or a parent domain. Nothing in the app serves it, and nothing needs to: on the API host it is one Caddy
  `respond` line added the day of submission (the app does not need a redeploy, and no new environment variable is
  introduced). `hatchloop.dev/.well-known/*` still goes to Next.js (open item from the last release). One challenge
  file serves one plugin per hostname.

## What is different on this door

Everything below is in `agent_interface/no_commerce.py`; `profiles.PROFILES["chatgpt"]` turns it on with four flags.
`mcp_server.py` reaches it from the handshake, `server/discover`, `tools/list`, `tools/call`, the four resources and prompts
handlers and the key-warning step, each branch commented. Nothing in that file changes what another door lists or
answers. Three changes DO reach every door, all made after review on 2026-10-04 and all deliberate: the full server
(`/mcp`, no door) ignores a `_profile` sent inside a request (it used to let a caller pick the ChatGPT door, which
skips the billing rails); the text inputs are bounded (below); and the sanctions scan normalises the caller's name once
and runs off the event loop.

| Surface | Other doors | `/mcp/chatgpt` |
|---|---|---|
| Tools | 8 (4 data/compliance + `get_status`, `get_outcome`, `preview_cost`, `self_test`) | exactly 3: `screen_sanctions`, `verify_company_record`, `map_trade_restriction` (`orientation: False`; `preview_cost` is a price list) |
| Descriptions | cost tag appended, "Free ..." opener | the manifest's own sentences with the opener removed; no tag |
| Input descriptions | cut at 80 characters mid-sentence | whole |
| Per-tool fields | `annotations.title` only | `title`, `outputSchema`, explicit `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`, `securitySchemes: [{"type":"noauth"}]` (never oauth2) |
| `initialize` / `server/discover` | "free within a daily quota ... call preview_cost first", write operations, a pointer to the full server | one description, "N tools, all read-only", the informational limits, the fencing rule; capabilities `tools` only |
| `resources/*`, `prompts/*` | five resources (the manifest with every `cost_model`) and four prompts | empty; `resources/read` and `prompts/get` refuse |
| Refusal of a tool not on the door | names the full server and its URL | names the three tools the door has; does not echo the requested name |
| x402 (`_meta["x402/payment"]`) | priced offer when enabled | refused before anything runs, no offer; nothing counted against the ceiling |
| Data quota, credits rail, free-key daily cap | consulted | never entered |
| Bad key | `hatchloop/auth_warning` with a link to key issuance | no warning |
| Over the daily ceiling | `free_quota_exceeded` with a top-up link | `rate_limited`: the limit, the reset time (UTC midnight), `retry_after_ms`, no link |
| Too fast | HTTP 429, 60-token bucket per address, shared with all other traffic from it | HTTP 429, its own bucket (150 burst, 2 a second per address), same neutral body, no link |
| A third-party text that could not be fenced | `untrusted_content.status: labelling_failed` | the same status, and a notice that says nothing in the result is fenced |
| Result | full receipt: `operation_id`, `trace_id`, `latency_ms`, `cost`, channel fields, `next_actions`, signed `compliance_receipt` (operation id, issue time, service version), `_matcher` | allow-list: `status`, `reason_code`, `human_message`, `result` (without the signed receipt and underscore fields), `retriable` when true, `untrusted_content` (the door's own notice, only the fields that were fenced). `structuredContent` on success equals the `content` text |
| Not advertised | listed in `llms.txt`, the MCP descriptor, the unknown-door 404, OAuth protected-resource metadata | none of those (`listed: False`, `oauth: False`) |

Kept on purpose: `screened_at`, `lists_screened` (with list dates), `sources_queried`, `disclaimer`,
`sources_unavailable`, `possible_matches_unverified`, `matching_method`. OpenAI's rule allows identifiers and timestamps that are
strictly required to answer; when a sanctions screen was run, and against which copy of which list, is the answer's
currency. The signed receipt is dropped because it cannot be trimmed without breaking its own signature and its
payload is exactly the session metadata OpenAI asks tools not to return.

Two of our own sentences read as pricing text to a scanner and are reworded on this door by exact anchored patterns
(`no_commerce._PHRASES`): "these free registries" becomes "these public registries" and ", and nothing was charged" is
dropped. Third-party text is never rewritten: a registry can hold "Credit Suisse AG".

### Inputs are bounded (every door)

`core/input_limits.py`: a name 300 characters, a product 300, a country 100, a Legal Entity Identifier 64, an HS code 32,
each party name in `map_trade_restriction` 300. A request over a limit is refused with `bad_input` before anything runs
(never truncated, never echoed) and nothing is screened for any party of that call. This door's tool list declares the
same numbers as `maxLength`; the Claude-facing doors enforce them without declaring them, because their manifest is
generated and was left alone. Why: one request with a name of 20,000 spaces held the single worker for 13.6 s (40,000
spaces: 37 s) on the sanctions door, because the matcher normalised the name again for each of ~79,000 list entries,
on the event loop. The matcher now does that once and runs on two worker threads of its own, and the cap bounds
everything else that is linear in the name.

### `openWorldHint: true`, and why (for the submission form)

OpenAI: true for public or open-ended entities; false for a bounded private account or catalogue. These tools read live
public registries and sanctions lists about arbitrary third parties, and the caller's text is sent on to them (GLEIF and
SEC EDGAR; our copies of the OFAC, EU and UK lists). They are not a bounded account. All three are `readOnlyHint: true`,
`destructiveHint: false`, `idempotentHint: true`: they change nothing anywhere. A test pins that each tool on this door is
read-only on the main tool list, so a write tool cannot be added here and silently labelled read-only.

### The allowance

The anonymous quota on the other doors is 100 calls a day per network address (when metering is on). OpenAI publishes
the ranges ChatGPT's requests come from (`openai.com/chatgpt-connectors.json`, fetched 2026-10-04: 278 IPv4 prefixes,
36,359 addresses, list dated 2026-09-22) but not how traffic is spread over them, so one address may stand for many
users: a count of 100 would shut ChatGPT out after the first hundred calls of the day, and show each refused user a
pricing link (section 13, "a second problem"). The decision:

* the door never touches the daily data quota or the credits rail, whatever `DATA_METERING_ENABLED` and `CREDITS_ENABLED` say;
* it has **two limits, both per network address, both abuse limits and neither a user allowance**:
  * a **daily ceiling of 20,000** (`CHATGPT_DOOR_DAILY_CEILING`, 0 turns it off), counted in memory (the service runs one
    worker; a restart resets it), not counted when the address cannot be determined. It answers with the limit and the
    reset time and no link;
  * a **rate bucket of its own: 150 burst, refilled 2 a second** (`CHATGPT_DOOR_RATE_BURST`, `CHATGPT_DOOR_RATE_PER_S`),
    kept apart from the 60-burst, 1-a-second bucket that every other `/mcp/*` request from that address shares, so a
    scanner cannot spend a ChatGPT user's tokens or the other way round. Over it, HTTP 429 with
    `{"detail":"rate_limited","retry_after_seconds":1}`, which is neutral: no link, no price. This is not an MCP error
    body; a client sees a transport-level 429.
* **Neither number is measured against ChatGPT's traffic**, which is not knowable before launch (how it is spread over the
  published ranges). The bucket is sized against the one thing that was measured: a sanctions scan costs about half a
  second of one core (0.5 to 0.6 s on a synthetic list of the live size, 2026-10-04) and the service is one worker, so 2 a
  second from a single address is already about a core; the 10 a second this first had would have let one address ask for
  five. After deploy, read the `rate_limited` outcomes the service already logs for this path and the per-address counts,
  and tune the two variables; no code change is needed.

The over-ceiling answer was proven with fakes; the live ceiling was not run out (20,000 calls, and the live verifier does
not try).

## Proof

* `tests/unit/test_chatgpt_free_door.py`: 43 tests, each guard paired with a control on an existing door showing the
  thing the guard removes is really there. Fixtures are real receipts captured from the handlers
  (`tests/fixtures/chatgpt_door/`).
* `tests/unit/test_chatgpt_door_review_fixes.py`: one test or more per finding of the review of 2026-10-04 (the `_profile`
  bypass, the denial of service, the hidden fencing failure, the Caddy claim, the rate bucket), each run against the
  unfixed tree first and seen to fail (30 failed on it); then 33 guards were removed one at a time and each made a
  named test fail, the tree being restored byte-for-byte after each. `tests/unit/test_chatgpt_door_review_p3.py` does the
  same for the smaller findings, including the post-deploy check itself, run offline against the in-process app.
* Mutation check: 20 guards removed one at a time (tool path, x402 refusal, ceiling, order of refusal and counting, auth-warning
  suppression, resources and prompts, handshake, discovery listing, OAuth metadata, receipt trim, underscore trim, phrases,
  notice, annotations, schema enums, orientation opt-out, the unknown-door list, the "Free" opener, the vocabulary scan); every one made a named test fail, and the tree was
  verified byte-identical to its snapshot after each.
* `scripts/check_no_commerce_door.py`, a CI step: scans every surface the door can say, plus every string literal in the three
  handlers, and refuses to pass unless the same scan finds pricing on the Claude-facing door.
* `python scripts/live_verify_release.py --expect-commit <sha> --only chatgpt_door --base <host>` after a deploy (not in the
  default list, so a run against an older build does not fail on a door that build never had).

## Before submitting (none of this is in this branch)

Recorded here so the other items do not rediscover it; the full list is section 13 of the kit's issues file.

1. **Support page.** `https://hatchloop.dev/support` returns 404 (checked 2026-10-03). OpenAI requires website, support,
   privacy and terms URLs, all HTTPS. This is a site change: **for the site item.** Not built here.
2. Demo video, release notes, the five positive and three negative test cases re-pointed at this door (the kit's
   `reviewer-test-cases.json`), a 30-character subtitle (for example "Sanctions list name checks"), and the commerce
   declaration (`review.commerce` false: nothing is sold or charged inside ChatGPT).
3. `LISTING.md` may say "the app never sells or links to credits" for this door only once this release is deployed and the
   live verifier's `chatgpt_door` check passes.
4. Re-read the privacy policy against what this door returns (it now returns less than the other doors).
5. Organisation verification and the domain challenge are founder steps in OpenAI's portal.
