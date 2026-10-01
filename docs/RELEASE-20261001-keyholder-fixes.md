# Release 2026-10-01: key-holder fixes

Branch `feat/keyholder-fixes-20261001`, built from the live commit `cd1b9a9`. Driven by the 2026-09-30
key-holder audit (the founder asked: do the people holding keys get what they want, and where do they
fail?). Six fixes. The database part is already applied to the spine; the code and the Caddy change are
**not** deployed yet.

| # | Audit fix | What changed | Where |
|---|---|---|---|
| 1 | Log every outcome, not only successes | One `usage_events` row per MCP request with outcome, error code, HTTP status, latency, client name, key state, argument **names** (never values). HTTP 429 / bad body / unknown door / crash are logged at the HTTP layer, 429s throttled to one row per caller per 30 s. | `billing/usage_logger.py`, `agent_interface/mcp_server.py` (`_finish_request`), `agent_interface/request_observer.py`, `main.py` (middleware), migration 009 |
| 2 | Tell a caller whose key is wrong | Key state is classified `valid / invalid / expired / placeholder / none`. A presented key that fails gets an `auth_warning` on `initialize`, `tools/list` and every tool result (in the text the model reads and in `_meta`), and the `auth_required` text names the key as the problem. Reads stay free. The presented value is never echoed or stored. | `agent_interface/key_state.py`, `mcp_server.py` |
| 3 | `/mcp/*` off the Next.js hop; per-key rate limit | Caddy sends `hatchloop.dev/mcp/agent-broker` and the five capability doors straight to the container (same public URLs). The app believes `X-Forwarded-For` only from its own proxy and reads it from the right; a correctly signed key gets its own rate-limit bucket. | `deploy/caddy/*`, `core/client_ip.py`, `main.py`, `identity.peek_agent_id` |
| 4 | Stop logging the key header | `X-Agent-Identity` and `X-Api-Key` are redacted in the `hatchloop.dev` log filter; `api.hatchloop.dev` gets an access log with the same filter; `remote_scrub_logs.py` blanks values already on disk. | `deploy/caddy/*` |
| 5 | Durable compliance writes and opt-outs | `compliance_audit` inserts, `pending_keys` store/consume, opt-out **load at start** and opt-out **writes** (STOP link, WhatsApp STOP, `handle_inbound`) go through six narrow `SECURITY DEFINER` functions. Direct table grants for `anon` are removed. | `migrations/spine/009_*.sql`, `compliance/optout_store.py`, `compliance/audit_log.py`, `agent_interface/key_request_logic.py`, `key_requests.py`, `unsubscribe.py`, `whatsapp_webhook.py`, `core/handle_inbound.py`, `main.py` |
| 6 | Channel honesty | `send_message`, `send_transactional_confirmation` and `call_business` answer `channel_unavailable` first thing - before the credit hold, before the free-quota decrement, before the compliance gate - when the deployment has no such channel. `tools/list` marks them; `initialize` says which channels are down. Availability is read from the environment per request. | `core/channel_status.py`, `mcp_server.py` |

## Order of operations

1. **Migration 009 - DONE 2026-10-01, additive and idempotent.** Applied to the spine as `spine_owner`.
   Evidence: `scripts/verify_spine_009.py --phase before|after`. The running container (`cd1b9a9`) is
   unaffected: its `usage_events_insert` is untouched, and the direct-table calls it makes to
   `compliance_audit` / `pending_keys` were already refused and are still refused (now 403 rather than an
   RLS-filtered 200 `[]`). If the spine is ever rebuilt with `resync`, apply this file again.
2. **Push the branch** (never `main`) after scanning the commits for secrets, then **deploy** with
   `ops/vps/deploy_agentbroker_vps.py <sha>`. No new environment variable is required
   (`TRUSTED_PROXY_CIDRS` has a non-empty default, so `check_deploy_env.py` does not demand it).
3. **Prove the start-up log** on the new container: `docker logs techmate-agentbroker 2>&1 | grep -i optout`
   must show `hydrated N durable opt-outs` and no `OPTOUT_HYDRATION_FAILED`. Before deploying, the same
   boot can be rehearsed against the spine with the container's own credential:
   `python scripts/verify_optout_hydration_live.py` (prints counts only).
4. **Caddy** (after the deploy, so the app already understands the forwarded address):
   `python deploy/caddy/install_mcp_direct.py plan ...` (read-only; shows the diff and runs `caddy adapt` on the
   box), then `install --yes` (backup, `caddy validate`, fix log ownership, **reload - never restart**, probe the
   public URLs, restore automatically on any failure), then `scrub` (dry run) and `scrub --apply --yes`.
   `deploy/caddy/mcp_direct.patch` is the same change as a zero-context (additions-only) diff; the full-context patch against the live file at planning time is kept in the private ops tree (`ops/vps/caddy/mcp_direct_20261001.patch`) because the live Caddyfile's comments describe internal systems and this repo is public.
5. **Verify** `usage_events`: `select outcome, count(*) from usage_events where ts > now() - interval '1 hour' group by 1`.

## Rollback

* Code: `ops/vps/deploy_agentbroker_vps.py --rollback-only`. The migration needs no rollback: the old image
  ignores the new columns and functions, and the new image falls back to the 7-field insert (loudly, as
  `usage_log_v2_missing`) if `usage_events_insert_v2` is ever missing.
* Caddy: `install_mcp_direct.py rollback --yes` (restores the newest `Caddyfile.bak-mcp-direct-*` and reloads).

## Things this release deliberately does not do

* It does not make SMS or voice work. SMS needs a carrier account and a registered US 10DLC campaign;
  voice is provisioned (a Vapi number, status active) but a real call has not been placed to prove the
  request shape end to end. Both are now honest instead of failing late.
* It does not change `find_business`, `mint_key`, or anything else in the audit's ranked list.
* It does not route `/.well-known/*`, `/keys/*` or `/webhooks/*` off Next.js; those still take the old hop.

## Evidence (all taken 2026-10-01; counts and booleans only, no secret, address or header value)

**Migration 009 on the spine** (`scripts/verify_spine_009.py`, run as `spine_owner` plus through the public door
with the container's own anon credential, every write sent with `Prefer: tx=rollback`):

| | before | after |
|---|---|---|
| the six new functions | none exist; public door answers 404 `PGRST202` | all six exist, `SECURITY DEFINER`, owner `spine_owner` (`rolbypassrls`), executable by `anon` + `service_role` only |
| `usage_events` outcome columns | 0 of 10 present | 10 of 10 |
| direct table grants to `anon` / `authenticated` | `compliance_audit` and `pending_keys`: 7 each (SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER); a direct `GET` returned an empty-but-200 | none on all three tables; a direct `GET` returns 403 `42501` |
| `compliance_audit_insert` / `pending_keys_upsert` / `consent_optouts_record` through the public door | 404 | 200, and the row counts (compliance_audit 872, pending_keys 11, consent_optouts 5) are unchanged afterwards: nothing was left behind |
| `consent_optouts_hydrate` | 404 | 200, 5 rows |
| the live 7-argument `usage_events_insert` | 200 | 200 (untouched) |

The migration was applied twice (the second time after a comment-only edit), which is also the idempotency proof.
One side effect to know about: `alter table usage_events` takes a brief exclusive lock. Six usage-telemetry
inserts from the running container timed out between 14:40:36Z and 14:40:44Z; no caller saw an error, and the
lost rows are crawler traffic.

**Start-up log** (`scripts/verify_optout_hydration_live.py`, the real `main.lifespan`, the anon credential only):

* build `cd1b9a9`: `OPTOUT_HYDRATION_FAILED err=consent_optouts returned HTTP 403 ... WITHOUT durable opt-outs`
* this branch: `hydrated 5 durable opt-outs`, zero failure lines, and the in-memory set holds as many distinct
  (recipient, channel) pairs as the table does.

**Caddy claims, proved on the box with a throwaway Caddy on loopback ports** (`deploy/caddy/verify_caddy_claims.sh`;
touches no production file, port, certificate or log; removed itself):

* with no `trusted_proxies`, a request that sent `X-Forwarded-For: 9.9.9.9` and `X-Real-IP: 8.8.8.8` reaches the
  upstream with `X-Forwarded-For` = the real peer and no `X-Real-IP`;
* with `request>headers>X-Agent-Identity replace REDACTED` the fake key never appears in the access log; the
  header is logged as the string `"REDACTED"`. Note this differs from Caddy's built-in `Authorization` redaction,
  which logs a one-element array - the scrub tool handles the array form that is already on disk.

**Existing log exposure** (`install_mcp_direct.py scrub`, DRY RUN, counts only): 4 un-redacted key-header values in
11 `hatchloop.dev` log files (3 in one rolled file, 1 in the active log), **none key-shaped**; `api.hatchloop.dev` has no log.

**Voice, checked without placing a call** (read-only `GET`s on the voice provider): the account answers, and the
outbound number exists, status `active`, a provider-issued US (+1) number. Not proven: that an outbound call
request is accepted, and what the provider restricts on that class of number. Twilio has no credentials in the
container at all, so SMS is unavailable.

**Tests:** baseline at `cd1b9a9`: 1 failed, 1987 passed, 43 skipped, 1 xfailed. This branch: 1 failed, 2170 passed,
44 skipped, 1 xfailed. The one failure is identical on both and is a Windows line-ending artefact (all nine
generated registry files differ from their generator only by CRLF; `gen_manifests.py --check` reports the same).
Each fix was removed in turn (17 one-line mutations) and its tests failed every time.

## Record for `sql/agentbroker/APPLIED.md` (private repo; not edited from here)

> `009_keyholder_outcome_logging_and_compliance_rpcs.sql` (kept in the agentbroker repo at
> `migrations/spine/`) - **yes, 2026-10-01 ~14:40Z**, applied with `scripts/apply_sql.py` as `spine_owner`.
> BEFORE: the six functions absent (404 `PGRST202`), `usage_events` without outcome columns, `anon` holding all
> seven table privileges on `compliance_audit` and `pending_keys`. AFTER: see the table above;
> proof re-runnable with `scripts/verify_spine_009.py --phase after`.
