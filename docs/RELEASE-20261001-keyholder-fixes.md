# Release 2026-10-01: key-holder fixes

Branch `feat/keyholder-fixes-20261001`, built from the live commit `cd1b9a9`. Driven by the 2026-09-30
key-holder audit (the founder asked: do the people holding keys get what they want, and where do they
fail?). Six fixes. The database part (migration 009) is applied to the spine before the code ships, because it is
additive and the previous image keeps working with it. Deployment status of the code and of the Caddy change is
recorded in the private operations record, not in this document.

| # | Audit fix | What changed | Where |
|---|---|---|---|
| 1 | Log every outcome, not only successes | One `usage_events` row per MCP request with outcome, error code, HTTP status, latency, client name, key state, argument **names** (never values). HTTP 429 / bad body / unknown door / crash are logged at the HTTP layer, 429s throttled to one row per caller per 30 s. | `billing/usage_logger.py`, `agent_interface/mcp_server.py` (`_finish_request`), `agent_interface/request_observer.py`, `main.py` (middleware), migration 009 |
| 2 | Tell a caller whose key is wrong | Key state is classified `valid / invalid / expired / placeholder / none`. A presented key that fails gets an `auth_warning` on `initialize`, `tools/list` and every tool result (in the text the model reads and in `_meta`), and the `auth_required` text names the key as the problem. Reads stay free. The presented value is never echoed or stored. | `agent_interface/key_state.py`, `mcp_server.py` |
| 3 | `/mcp/*` off the Next.js hop; per-key rate limit | Caddy sends `hatchloop.dev/mcp/agent-broker` and the five capability doors straight to the container (same public URLs, with and without a trailing slash). The app believes `X-Forwarded-For` only from its own proxy and reads it from the right; a correctly signed key gets its own rate-limit bucket. | `deploy/caddy/*`, `core/client_ip.py`, `main.py`, `identity.peek_agent_id` |
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
   `deploy/caddy/mcp_direct.patch` is the same change as a zero-context (additions-only) diff; the full-context patch against the live file at planning time is kept in the private operations record because the live Caddyfile's comments describe internal systems and this repo is public.
   The installer probes both spellings of every public URL (`/mcp/<door>` and `/mcp/<door>/`) and restores the backup automatically if any fails; the trailing slash is stripped in Caddy because the origin has no route for it (it answers 307 with an `http://` Location).
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

## Evidence

The measured evidence for this release (spine before/after catalog check, start-up log, throwaway-Caddy proofs,
log-exposure counts, test and mutation results, the live verification after deploy) is kept in the private
operations record rather than in this public repo, because it quotes live row counts, role names and internal
paths. Everything in it is re-runnable from the scripts that are in this repo:

* `scripts/verify_spine_009.py --phase before|after` - the catalog and public-door check for migration 009.
* `scripts/verify_optout_hydration_live.py` - boots the real lifespan against the spine with the container's own
  credential; must print `hydrated N durable opt-outs` and no failure line.
* `deploy/caddy/verify_caddy_claims.sh` - three claims proved on a throwaway Caddy: a client cannot choose its
  own forwarded address, the key header never reaches the access log, and a trailing slash on a door is removed
  before the origin sees it.
* `python -m pytest tests/ -q` - the only failure is the Windows line-ending artefact in the generated registry
  files (`test_every_generated_file_matches_the_registry`), which fails identically on the base commit.
