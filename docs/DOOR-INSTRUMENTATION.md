# Door instrumentation: which door, which protocol version, how many results

Verdict item A7 (`docs/reviews/2026-10-03-mcp-focus-verdict.md` in the operations record): *"Instrument every door.
Without it we cannot see the first buyer."* Before this change the door lived in free text (`detail = 'door=<name>'`)
for the five capability doors and nowhere for the full server; the protocol version lived in `detail` for 2026-07-28
requests only; the number of results was nowhere; and the six retired doors answered every POST from a function that
wrote **no row** (only a GET's `410` reached the HTTP layer's failure log), so the part of the traffic that is mostly
directory scorers (about 440 requests a day) was the one part nobody could see.

Branch `feat/door-instrumentation-20261004`, built from `4e46f8e`. Deployment status of the migration and the code is
recorded in the private operations record, not in this document. Nothing here is switched on by a setting: the code
writes the new fields, and the database function that accepts them is created by migration 013.

## What every usage row now says

Three new nullable columns on `usage_events` (migration `013_usage_events_door_columns.sql`):

| Column | Values | NULL means |
|---|---|---|
| `door` | `agent-broker` (the full server: `/mcp` and `/mcp/agent-broker`, one door under two spellings), a capability door by its own name (`sanctions-screening`, `sms-whatsapp-messaging`, `appointment-booking`, `compliance-check`, `company-verification`), `retired:<slug>` (one of the six retired servers, answered with a tombstone), `unknown` (a path under `/mcp` that is not a door: a probe, a typo, a key pasted into the URL; the caller's text is not kept) | The request never reached an MCP door (a page, a REST route), or the row was written before the migration and its `detail` names no door |
| `protocol_version` | Only ever one of the versions we speak: the `_meta` declaration, else the `MCP-Protocol-Version` header, else (on `initialize`) the version the server negotiated | The request did not state one (a stateless server cannot know what an earlier handshake agreed), or it stated one we do not speak (the request is then labelled by its `error_code`, `unsupported_protocol_version`) |
| `result_count` | How many items the call's principal list held: `businesses` (find_business, which states its own `result_count`), `matches` (screen_sanctions), `restrictions` (map_trade_restriction), `awards` (lookup_us_contracts); the number of tools / resources / templates / prompts a list method returned | The call failed, or the method or tool has no list worth counting. Never a value from the result, only how many |

The door comes from the **route**, never from the payload, for the same reason the profile does: a caller that could
name its own door would be writing its own analytics. Every value is one of our own labels or a plain number, checked
in Python before it is sent and again by the database function (`22023` on anything else), and a value the container
cannot vouch for is sent as NULL, because a refused call loses the whole row.

The old free text is unchanged (`door=<name>`, `pv=<version>` for 2026-07-28), so anything that reads it still works.
`client_name` / `client_version` (from `initialize` or the 2026-07-28 `_meta`) and the full `user_agent` were already
columns, so no separate `client_hint` column was added: a family label derived from the user agent belongs in a query,
not frozen into rows.

## Which requests write a row

| Request | Row | Door |
|---|---|---|
| `POST /mcp`, `POST /mcp/agent-broker`, `POST /mcp/<capability door>` | one per JSON-RPC message (a batch writes one per member; a batch refused whole writes one) | `agent-broker` or the door's name |
| `POST /mcp/<retired>` and `/mcp/<retired>/mcp` (the tombstone) | one per message answered, built by the same code as a live door's row (client name, key state, address, version) | `retired:<slug>` |
| `GET` / `HEAD` on a retired door (410), a `429`, a `404` for an unknown door, a bad body, a crash | one `http_error` row from the HTTP layer, when the route did not already write one | from the URL path: the door, `retired:<slug>`, or `unknown` |
| Anything else (`/ops/*` REST calls that succeed, pages, webhooks) | not instrumented here (see "Not covered") | NULL |

A request is written once, whichever layer sees it. A keyless caller at a retired door is filed as `session_kind =
'crawler'` whatever its method: a retired door runs nothing, so nobody is doing work there, and a scorer's `tools/call`
would otherwise land in `anon_agent`, the bucket every "does anybody use us" figure reads. A caller that **holds a key**
keeps its key classification: somebody who holds a key and knocks on a dead door is the one signal such a door can give.
Calling a tool by name at a retired door is recorded as a request (`requested_name`), never as a run of that tool, so a
tombstone's failures never appear in `find_business`'s own numbers.

## Order of operations

1. **Apply migration 013 first**, as `spine_owner`, with the operator's apply tool (the operations repo's
   `scripts/apply_sql.py migrations/spine/013_usage_events_door_columns.sql`, `SUPABASE_DB_URL` through the spine
   tunnel; one transaction). The file refuses to run as any other role, because
   a function created by a superuser would be a superuser-owned `SECURITY DEFINER` callable by `anon`. It is additive and
   idempotent, and the image that is live today keeps writing exactly what it writes now: v1 and v2 of the writer are not
   touched.
2. `python scripts/verify_spine_013.py --phase before` **before**, `--phase after` **after** (catalog in a read-only
   session, plus a call through the public door that is rolled back with `Prefer: tx=rollback`).
3. **Deploy the code** with the operator's deploy tool, as every release is. No new environment variable.
4. `python scripts/verify_spine_013.py --phase live`: at least one row in the last hour carries a door, and no MCP row
   lacks one. Then **run the migration once more**: it copies the old `door=` / `pv=` detail text into the columns for
   rows the previous image wrote between steps 1 and 3 (it only fills NULLs, and only from text that is exactly the shape
   the server wrote).

If the code is deployed ahead of the migration, nothing is lost but the three new fields: the writer steps down from
`usage_events_insert_v3` to `_v2` (every outcome column still recorded) and then `_v1`, for ten minutes at a time, and logs
`usage_log_v3_missing` at ERROR each time it does. **Rollback:** the code needs no database change (the previous image
never calls v3); the migration needs none (an unused column and an unused function).

## Reading it: `scripts/door_usage_report.py`

Read-only (it refuses any session that is not read-only), five queries:

- **organic callers per day**: distinct (address hash, user agent) pairs that sent an MCP request to a *live* door,
  excluding our own infrastructure's user agents (`OWN_INFRA_UAS`; keep it in step with `OWN_INFRA_UA_EXACT` in the
  operations repo's `mcp_traffic_audit.py`; `--own-ua` extends it); and the subset that did more than discover. This is
  the verdict's "one daily organic caller figure". It is a floor on distinct sources, not a head count.
- **new clients in the window**: clients not seen in the previous 30 days that called a live door in the last hour, which
  is the "first Muse, Dots or Grok call, visible within the same hour" view.
- **rows by day, door and protocol version**, **result counts by tool** (external callers only; the success rate of a
  tool is read here once its upstream is healthy), and **instrumentation gaps**: rows in the window that should carry a
  door and do not.

"Live door" means a capability door or `agent-broker`, or NULL on an MCP row (every row written before the migration has
none, and the retired doors wrote no rows before it, so such a row came from a live door). `retired:%` and `unknown` are
excluded, and so are `method = 'http'` rows: a failure at the HTTP layer is not a caller doing MCP. (Existing HTTP-layer
rows are classified `anon_agent` by the older rule; this report does not read `session_kind` for them.)

## Proof

- `tests/unit/test_door_labels_and_counts.py`, `tests/unit/test_door_instrumentation.py`: the labels, the version, the
  count, the dispatcher, the HTTP layer, every retired door (all six, both path spellings, GET and POST, batches,
  notifications, refusals), the writer and its ladder.
- `tests/unit/test_spine_013_usage_events_door.py`: the migration read as text (additive, one new function, old writers
  untouched, least privilege, refuses the wrong role).
- `tests/unit/test_spine_013_usage_events_door_pg.py`: the migration run on a real PostgreSQL (the `postgres:18.6` image,
  built to look like the live spine: same roles, default privileges and `usage_events` table, with the real migrations 009
  and 010 applied first). Opt-in, because it starts a throwaway container: `SPINE_PG_TESTS=1 python -m pytest
  tests/unit/test_spine_013_usage_events_door_pg.py`. It never pulls an image and never touches a real database.

## Not covered (so nobody assumes it is)

- **Successful `/ops/*` REST calls** write no row (only their failures, through the HTTP layer, with no door). They are a
  REST twin of the MCP tools, not an MCP door.
- **Retail Broker and Warrant** are separate services with their own code; the verdict asked for them to write through the
  same function. The migration is the database half: a writer there can call `usage_events_insert_v3` with its own door
  label (any lower-case identifier up to 63 characters is accepted). The writers are not in this repository.
- **The Cloudflare edge worker** is not in the live path and was not changed.
- Existing HTTP-layer rows stay `anon_agent` under the old classification; changing that would move a number other
  reports read.
