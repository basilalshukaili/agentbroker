# Release 2026-10-03: find_business that explains itself and answers in 5 s; honest tool labels; tombstoned retired doors

Branch `feat/find-business-speed-labels-20261003`, built from the live commit `1f85885`. Driven by the 2026-10-03
MCP demand review (`find_business` was the tool outsiders called most, and the least likely to work). Not deployed
by this change; the order of operations is below.

## What the demand review found

In the 47 hours to 2026-10-03, 26 external `find_business` calls: **5 worked, 21 died before a search ran**
(11 with no arguments, 10 that sent a `location` and a `vertical` the strict schema refused), each refusal naming a
field and nothing else, as a JSON-RPC error many clients never show the model. Successful live lookups took
16-25 s. Probing the live server the same day found something worse than a refusal: `{"city": "Muscat",
"vertical": "restaurant"}` - the shape the published schema itself advertises - was **answered for Atlanta**, as a
success, because the dispatcher defaulted a missing `location` to Atlanta and nothing implemented `city`.

## What changed

| # | Change | Where |
|---|---|---|
| 1 | **The request is read, not rebuilt field by field.** A place may be a string, a `location` object, or top-level `city` / `region` / `country`; a kind of business may be `capability`, a synonym key, or a real kind sent as `vertical` ("restaurants", "cafe", "clinic"). A word we do not map is searched by business name, and the result says so. `vertical` is optional. **The Atlanta default is gone**: no place is an error. What was interpreted is in `input_notes`, what was ignored in `ignored_arguments`, the assembled place in `location_normalized_from`. | `core/find_business_input.py`, `core/models.py`, `agent_interface/mcp_server.py` |
| 2 | **A refusal teaches.** An unsearchable request is a tool error (`isError`, which the model sees), names what was wrong in the caller's terms, and carries an example that works; a test runs every example it can emit through the real pipeline. | same |
| 3 | **5 s budget with partial results.** The whole call returns by `CALL_BUDGET_S` (5 s; `FIND_BUSINESS_BUDGET_S`, 1-25). A lookup that is not back says `status: partial`, `reason_code: search_in_progress` (not an error; `isError` stays false), shows the resolved place, and **keeps running in the background**, so the cache fills and an identical repeat is instant. It used to be cancelled at 25 s, so a query needing 7 s could never finish. Background lookups are capped (12); past the cap a call falls back to the old cancel-at-budget. | `core/find_business.py` |
| 4 | **Pre-warm.** Production only (`FIND_BUSINESS_PREWARM=0` turns it off): 24 place x kind lookups, one at a time, 20 s apart, starting 2 minutes after boot and refreshed every 5 h (cache life is 6 h). Made the way a real call is made, so a later real call is a cache hit (tested). Stops at the first 429 / 403 / open breaker / kill switch and after 4 failures in a row. The list is the places and kinds this product documents plus the cities we work in; **it is not measured popularity** (arguments are logged as names only) - replace it once they are logged. | `core/find_business_prewarm.py`, `main.py` |
| 5 | **`result_count`** in every result, so an empty answer is countable. | `core/find_business.py` |
| 6 | **Every tool that is not production-ready says so.** `[beta]` / `[limited]` in the description, the reason in `_meta["hatchloop/readiness"]` (costs the model nothing), the facts in `manifest.json`, one definition (`core/tool_readiness.py`). Delivery-channel tools get `unavailable` / `beta` from the environment, as before. A test fails if tools/list, the catalogue, the discovery documents and the README disagree, and each limit is demonstrated by calling the tool. | see below |
| 7 | **Keyless-count contradictions.** `mcp.json` listed `screen_sanctions`, `verify_company_record` and `map_trade_restriction` in BOTH `paid_tools` and `quota_free_tools` (a keyless call to them works); the four access lists now partition the 23 tools exactly. `docs/PRICING.md` said 12 free tools (it is 13: 11 keyless + 2 that need a key); five submission drafts said 15 keyless (it is 14). `/keys/mint` was documented as `503 not_configured`; the live server answers `401 invalid_request` (a secret is set, and is not published). | `agent_interface/well_known.py`, docs |
| 8 | **Retired doors are tombstoned in MCP.** The six dead servers answer `initialize` (named "(RETIRED)"), `tools/list` (one tool, `server_retired`) and any call with the live server's address; GET/HEAD stay `410 Gone` with `Link: rel="successor-version"`. They are not counted as agent requests. Needs a Caddy route (below). | `agent_interface/retired_doors.py`, `deploy/caddy/mcp_retired.py` |
| 9 | `self_test` stays green when the upstream is merely slow (`search_in_progress` is the contract working). | `agent_interface/self_test.py` |

### Tool labels

| State | Meaning | Tools |
|---|---|---|
| `beta` | Works, with a limit you must plan around | `find_business`, `capture_lead`, `handle_inbound`, `escalate_to_human` |
| `limited` | Works for a narrow subset of inputs; otherwise fails honestly, uncharged | `verify_business`, `schedule_appointment`, `import_booking_url`, `mint_key` |
| `unavailable` | Cannot run on this deployment (computed from the environment) | `call_business` today; `send_message` / `send_transactional_confirmation` when a channel is missing |

## Measurement

`scripts/measure_find_business.py` scores any checkout on a fixed sample: 26 calls shaped like the 26 external
calls (11 with no arguments, 5 working lookups, 10 reconstructed mistakes - the review recorded argument NAMES
only, so those 10 are a reconstruction, labelled as such) and 20 distinct valid lookups for latency. Classes:
`complete`, `partial`, `guided_error`, `bare_error`, `unavailable`, and `wrong_place` (a result for a different
town, never counted as usable). Both versions were scored by the same script.

**The like-for-like run (`--simulate`: one deterministic stand-in upstream, Overpass latencies and 25% failure
rate taken from the live sample, the same stand-in for both versions):**

| | before (`1f85885`) | after |
|---|---:|---:|
| usable answer, all 26 calls | 15.4% (4) | **57.7%** (15) |
| usable answer, the 15 calls that carried a place and a kind | 26.7% (4) | **100%** (15) |
| refusals that carry an example (of the 11 no-argument calls) | 0 of 11 | **11 of 11** |
| `wrong_place` answers (success for the wrong town) | 2 | **0** |
| cold lookup, 20 distinct: p50 / p95 / max | 17.9 s / 25.0 s / 25.0 s | **5.0 s / 5.0 s / 5.0 s** |
| cold lookup returning `complete` inside the call | 75% (15 of 20) | **35%** (7 of 20) |
| cold lookup returning `complete` or `partial` | 75% | **100%** |
| the same 20 repeated once | (no background work: same as cold) | **100% complete**, 18 from cache |

Read the two `complete` rows together. **Fewer cold lookups finish inside the call (35% against 75%) because the
call now stops at 5 s and a lookup that needs 12 s returns `partial` with the place and no businesses.** What it buys
is that no caller waits 25 s, and that the repeat is a cache read. The 11 no-argument probes can never be "usable"
(there is no place to search); they now get a refusal that works as a lesson, so 57.7% is the ceiling for this sample
and the 100% row is the fair one for calls that said what they wanted. The pre-warm is not in these numbers.

**Live runs** (the public servers, from the development laptop, 2026-10-03; reports in
`docs/measurements/find-business-20261003/`). The live "before" run is the same 26-call outcome sample as above:
usable 15.4% (the same four), 2 `wrong_place`; on the 20-lookup sample 8 completed and 12 were refused after the public
Overpass server answered 5xx / timed out and the circuit breaker that follows four failures took over; lookups that
reached the upstream took p50 10.8 s / p95 20.1 s (21 attempts). The live "after" runs happened while
`overpass-api.de` was not answering from that host at all (a plain request to its status page timed out after 21 s and
had still not answered 45 minutes later; the cause is unknown, and the host had sent about 35 Overpass requests in the
previous quarter hour, so a per-IP limit is possible and unconfirmed). They therefore prove the budget - the first four
lookups returned `partial` at 5.0-5.8 s with the place resolved while the upstream hung, and every later call returned
at once - and **cannot measure success. No live "after" success rate exists yet**; take it from `usage_events` after
deploy (below). Do not read the unavailable rows in that report as a regression: the same host could not reach the
upstream at all.

## Order of operations

1. **Push the branch** (never `main`) and **deploy** `ops/vps/deploy_agentbroker_gated.py <sha>`. No new environment
   variable is required: the 5 s budget and the pre-warm default correctly (pre-warm on only when
   `ENVIRONMENT=production`).
2. **Verify the origin**: `tools/list` on `api.hatchloop.dev/mcp` shows `[beta]` on `find_business`;
   `find_business` with `{"city": "Muscat", "capability": "dentist"}` searches Muscat; with `{}` it returns a tool
   error with `how_to_resolve.example_arguments`; `POST api.hatchloop.dev/mcp/data-enrichment` answers 200 with
   `serverInfo.name` ending "(RETIRED)". `docker logs` should show a `find_business_prewarm pass` line at WARNING
   about two minutes after boot, and then every five hours.
3. **Caddy, only after step 1** (until the origin knows these doors it answers `404`, worse than today's 410):
   `python deploy/caddy/install_mcp_direct.py plan --change retired ...`, then `install --change retired --yes`
   (backup, `caddy validate`, reload, probes, automatic restore on any failure). Afterwards use
   `verify --change retired`; `verify` without `--change` still expects the old 410 for the retired POSTs and will
   flag them.
4. **Edge snapshots**: `mcp-tools-list.json` and `mcp.json` are regenerated in this branch from the code
   (`refresh_edge_snapshots.py --local-tools` / `--local-routes mcp.json`). The others come from the live origin and
   should be refreshed after deploy with `refresh_edge_snapshots.py`.
5. **Measure**: external `find_business` success from `usage_events`, and `result_count` (in the result, not yet a
   column) once the instrumentation item lands.

## Rollback

Code: `ops/vps/deploy_agentbroker_vps.py --rollback-only`. Caddy: `install_mcp_direct.py rollback --yes`. Nothing in
this change writes to the database or needs a migration.

## Model-context cost

`scripts/check_context_budget.py` (in the HatchLoop tree) was already red at the live commit (agent-broker 5056 against
a recorded 5045; two doors above their baselines). This change moves every number **down**: agent-broker 5056 -> 5029,
sms-whatsapp-messaging 1942 -> 1931, appointment-booking 1677 -> 1672. `find_business` fell from 486 to 469 tokens
while gaining a label; the description sits at 449 of the 450-character cap (a test fails if the label or a clause
would truncate it). It does not clear the existing breaches (`find_business` is still over the 350-token per-tool
rule; the two door baselines).

## Things this change deliberately does not do

* It does not make `find_business` faster for a place nobody has asked about: the public Overpass server sets that.
  It makes the slow case honest, bounded and repeatable, and warms the likely cases. A smaller first query, or a
  second Overpass operator, are the next levers and are deployment decisions.
* It does not delist the dead doors from the five directories that list them (that is a message to each).
* It does not touch the A2A agent card's `streaming` / `pushNotifications` claims, the OAuth discovery 404s, the
  `server/discover` handshake or the `POST /keys/request` 503 - separate items in the same review.
* It does not add a `result_count` database column (a migration); the number is in the result.
