# OAuth "Connect" sign-in (MCP authorization)

Branch `feat/oauth-connect-20261003`, built from the live commit `1f85885`. **Not deployed.** Evidence for it:
`docs/reviews/2026-10-03-mcp-demand-evidence.md` (934 OAuth-discovery probes, every one a 404; consumer assistants
offer only "no authentication" or OAuth) and verdict item A3 in `2026-10-03-mcp-focus-verdict.md`.

## What a person experiences

1. In Claude, ChatGPT, Grok or Muse they add AgentBroker by its URL, or find it in a directory. The free tools
   (the always-free ones and those free within a daily quota; `core/tool_auth.py` is the one place that counts them)
   work immediately with no sign-in at all.
2. They ask for something that needs an account (send a message, book, read a conversation). The assistant shows a
   **Connect** button.
3. A page opens on `api.hatchloop.dev` naming the app and asking for an email address. No password.
4. They open the link we mail (works once, 15 minutes), read what they are approving, press **Confirm**.
5. The assistant retries the same call with a key bound to their account. **Credits are bought on hatchloop.dev**
   (ChatGPT's app rules forbid selling them in the conversation); the assistant spends them from its next token refresh.

## Endpoints (all on the origin, `api.hatchloop.dev`)

| Path | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource[/<path>]` | RFC 9728. `resource` is exactly the URL the person typed (`/mcp`, `/mcp/<door>`, or the site's `/mcp/agent-broker` via `?host=hatchloop.dev`); `authorization_servers` is the issuer. Unknown paths and hosts are 404, never reflected. |
| `GET /.well-known/oauth-authorization-server` (and `/openid-configuration`, same OAuth document, no OIDC-only fields) | RFC 8414. `code_challenge_methods_supported: [S256]`, `token_endpoint_auth_methods_supported: [none]`, `client_id_metadata_document_supported: true`, `authorization_response_iss_parameter_supported: true`. |
| `POST /oauth/register` | RFC 7591 dynamic registration, public clients only. Rate-limited. |
| `GET /oauth/authorize`, `POST /oauth/authorize/email`, `POST /oauth/authorize/poll` | Start, mail the link, wait. |
| `GET/POST /oauth/verify` | The mailed link: GET shows what is being approved and spends nothing; POST is the deliberate press. |
| `POST /oauth/token` | `authorization_code` and `refresh_token` grants. Form or JSON. |
| `POST /oauth/revoke` | RFC 7009; ends a refresh chain. |

## How a refused call becomes a Connect button

Claude starts sign-in **only** on an HTTP 401 with `WWW-Authenticate: Bearer ... resource_metadata`; ChatGPT starts it
from a normal tool result carrying `_meta["mcp/www_authenticate"]` and does not re-trigger from a 401. Both are
implemented (`agent_interface/oauth/challenge.py`), chosen per client (`OAUTH_CHALLENGE_STYLE`, default `auto`):

* **`auto` sends the 401 only to callers known to open a sign-in from one**: a caller whose User-Agent contains
  `claude-user`, `claude-code` or `claude-ai` (measured in real traffic; extend with `OAUTH_CHALLENGE_401_CLIENTS`,
  comma-separated, once an assistant has been walked through live), and any caller that already sent a bearer token
  (it speaks OAuth, and an expired token must get the 401 so it refreshes). ChatGPT gets the normal result with `_meta`.
  **Everyone else gets the same normal result plus the `_meta` hint**, because an agent with no OAuth support raises on an
  HTTP 401 before it reads the body that tells it how to get a key, buy credits or pay per call (the official Python SDK
  without an auth provider does exactly that - measured by the review). An OAuth-capable client that is not on the list can
  still connect through the discovery documents; it is just not prompted.
* The 401 **keeps the previous JSON-RPC body**, so a caller that reads bodies sees what it always saw.
* A call is challenged only when the dispatcher itself refused it for lack of an account (`auth_required` for the write
  tools, `identity_required` for `get_conversation`) **and** the request carried no valid credential, or a Bearer token
  that did not validate (so an expired access token gets a 401 and the client refreshes). A bad key sent the old way
  (`X-Agent-Identity` / `X-Api-Key`) keeps the old answer and its diagnosis. A valid key whose scope is too narrow keeps
  the old answer. Keyless tools, the handshake and notifications are never touched. `call_business` answers
  `channel_unavailable` (voice is not provisioned) and is therefore not challenged: signing in cannot make it work.
* `tools/list` carries ChatGPT's per-tool `securitySchemes` (`noauth` / `oauth2` / both), derived from
  `core/tool_auth.py`, at request time and only when the sign-in can complete.
* If the spine cannot answer a readiness probe (migration not applied, database down) **nothing changes**: no 401, no
  `securitySchemes`. `OAUTH_CONNECT_ENABLED=0` switches the whole feature off (404 on every route, previous behaviour
  byte for byte); `OAUTH_CHALLENGE_STYLE=off` keeps the endpoints but never signals and never adds `securitySchemes`.

## Who the key belongs to

The access token **is** an Agent-Identity key: the same signed value `/keys/verify` emails, validated by the same
`identity.validate_token`, accepted wherever `Authorization: Bearer` already was, metered by the same code. It lives one
hour, carries `aud` (the resource it was issued for; tokens for another audience are refused, tokens without `aud` -
every key issued before - are untouched), and carries no email.

* An email that has **bought credits** is the `sub_<polar customer>` account. The Polar order webhook records the link
  (`billing/polar_webhook.py` -> `agent_interface/oauth/link.py` -> `oauth_account_link`, first writer wins), so the
  next refresh mints a paid identity with that plan's scope. A refunded (revoked) customer is refused at the next
  refresh and the token itself stops validating.
* Any other email is `free_<first 16 hex of sha256(lower(email))>` - the id `/keys/verify` and the portal already
  derive, so the same address lands on the same account and the same 100-operation daily allowance everywhere.

## Security decisions (and the residual risk of each)

* **Public clients only, PKCE S256 mandatory.** A client that cannot do PKCE cannot connect. `private_key_jwt` clients
  (ChatGPT's metadata document declares it) are served as public clients; the intersection both sides support is `none`.
* **The code goes to the browser that started the sign-in**, proved by a poll secret held only by that page and by an
  `HttpOnly` cookie (`hl_oauth_<request id>`, one per sign-in) that is set **by the response that loads the sign-in page
  and nowhere else**. The email form must present that cookie, and its `poll_secret` field must equal it; the form can name
  any request id, so it is never allowed to assert "this browser started it". (The first version set the cookie in the
  reply to the email form from a form-supplied secret: a cross-site post could plant it in a victim's browser, the victim's
  own link then skipped the match code, and one press of Confirm handed the code to the attacker - the review's
  account-takeover finding, now pinned by `test_oauth_connect_gate_fixes.py`.) `Sec-Fetch-Site`/`Origin` from another
  site is refused in front of that (`Origin: null` is allowed: the pages are `no-referrer`). A browser that refuses the
  cookie is told so; sign-in needs it. The mailed link can be opened on any device and hands nothing to whoever presses Confirm.
* **Match code (consent phishing).** Anyone can start a sign-in with *your* address and *their* client; you receive a
  genuine email. A link opened outside the starting browser therefore asks for a 4-digit code only the starting page
  shows. Found by the adversarial review; pinned by tests and by a mutation check. *Residual:* a person talked into
  reading the code to the attacker still loses. Nothing a link can do prevents that.
* **A link is never spent by opening it** (mail scanners open every link); only the Confirm press spends it, atomically.
* **Digests only.** Codes, links, poll secrets and refresh tokens are stored as SHA-256; no email address is stored
  anywhere in the OAuth tables (a digest and a masked hint, `j***@x.org`). Tests assert this against the real tables.
* **Refresh tokens rotate** and replay revokes the whole chain; replaying a spent *code* revokes what it started;
  another client presenting a token is refused without burning it. *Residual:* a client that submits the same refresh
  token twice concurrently loses its session (RFC 6749 section 10.4 trade-off).
* **Client metadata documents** are fetched with an SSRF guard (https/443 + name only, every resolved address must be
  globally routable, the connection goes to the address that was checked, no redirects, an uncompressed body of at most
  64 KiB, and a TOTAL deadline of 6 s for resolve-to-last-byte - a per-read timeout alone is satisfied by a host that
  sends a byte every 3 s). The per-caller and per-host fetch ceilings are spent only by fetches that really leave the box,
  never by a sign-in answered from the cache, and simultaneous first requests for one client share one fetch. A document
  that was good is still used for up to an hour past its freshness when a refresh is refused or fails for a reason that is
  not about the document, so spending a vendor's host budget with junk paths cannot lock the real vendor out.
  Redirect URIs match exactly; loopback ports are ignored (RFC 8252); private-use schemes are allowed
  for dynamic registration only and are labelled on the consent page. The page names the **host** of the client_id,
  never the self-chosen name alone.
* **Mail abuse.** Per source address (an IPv6 caller counts by its /64), per recipient digest, global, and - inside the
  database where it cannot be raced - a resend gap and a ceiling per sign-in. A mailbox only ever receives mail for an
  address someone typed. **The email names the app only by the host of a metadata-document client_id; a self-chosen name
  (a registered client's) never appears in it** ("An app that registered itself", plus where it will return to), so we are
  not the courier of a stranger's sentence under our own domain.
* **Rate limits are in memory, one table per limiter.** A limiter that overflows drops its own least-recently-used keys;
  it can never clear another limiter's counters (the first version kept one shared table and cleared it at 20,000 keys, and
  the poll endpoint counted a key per made-up request id, so a stranger could post 20,000 ids and switch every ceiling
  off). The poll endpoint has a per-address ceiling checked before any key the caller chose. The token and registration
  endpoints are called by vendors from shared egress addresses, so their per-address ceilings are abuse-only (3,000 and 120
  per hour). *Residual:* the counters reset when the container restarts; the per-sign-in send ceiling in the database does not.
* **Pages** are served with a nonce'd CSP, `frame-ancestors 'none'`, `no-store`, `no-referrer`; ASCII-only addresses.
* *Residual:* free accounts multiply by plus-addressing (`a+1@`, `a+2@`), exactly as they already do through `/keys/request`.
  *Residual:* the `oauth_*` functions are executable by `anon`, which is safe only while the database anon credential is
  a secret held by the service (the same invariant as migration 009); `oauth_account_link` is as sensitive as `credit_grant`.

## State (spine, `migrations/spine/011_oauth_connect.sql`)

Five tables (`oauth_clients`, `oauth_requests`, `oauth_codes`, `oauth_refresh_tokens`, `oauth_account_links`), RLS on,
**closed to anon/authenticated/public**; sixteen `SECURITY DEFINER` functions with a pinned `search_path`, executable by
`anon` and `service_role` only. Additive and idempotent; the running image never calls any of it. Old rows are swept by
sign-in creation (bounded), so nothing grows without a scheduler.

## Operating it

Order (the same as migration 010): **1.** apply `011` to the spine as `spine_owner` (`scripts/apply_sql.py`);
**2.** `python scripts/verify_spine_011.py --phase after` (catalog + public-door boundary, writes rolled back);
**3.** deploy through the gated wrapper (no new required environment variable - checked with
`check_deploy_env.derive_required_env_vars`: 57 on the live commit, 57 here); **4.** post-deploy probes:

```
curl -s https://api.hatchloop.dev/.well-known/oauth-authorization-server | python -m json.tool
curl -si -X POST https://api.hatchloop.dev/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_conversation","arguments":{}}}'   # 401 + WWW-Authenticate
curl -s -X POST https://api.hatchloop.dev/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_quota","arguments":{}}}'        # 200, unchanged
```

then one real sign-in from a browser with an inbox you control (the only step that sends mail).
Variables (all optional): `OAUTH_CONNECT_ENABLED` (default on), `OAUTH_CHALLENGE_STYLE` (`auto`), `OAUTH_ISSUER`
(defaults to `PUBLIC_BASE_URL`, then `https://api.hatchloop.dev`), `OAUTH_SITE_HOST` (`hatchloop.dev`),
`RESEND_API_KEY`, `KEY_VERIFY_SECRET` / `JWT_SIGNING_SECRET` and `PUBLIC_BASE_URL` are the existing ones (the match code is derived from the verification-link secret).
Log lines to watch (never contain an address, link, code or token): `oauth_signin_email_sent`, `oauth_signin_email_unavailable`,
`oauth_token_issued`, `oauth_refresh_refused reason=...`, `oauth_challenge style=... verdict=...`.
Rollback: `OAUTH_CONNECT_ENABLED=0` (no redeploy of code needed beyond the env), or `ops/vps/deploy_agentbroker_vps.py --rollback-only`.

**Site-host discovery (optional, a Caddy change).** `hatchloop.dev/.well-known/*` still goes to Next.js, so a client
that probes the *site* URL's well-known path (ChatGPT does, at connector creation) finds nothing there. Until a route is
added, connect assistants to **`https://api.hatchloop.dev/mcp`** (every discovery path is served by the origin), or add
inside the `hatchloop.dev` site block, next to the existing `mcp_direct` handles:

```
@oauth_prm path /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/*
handle @oauth_prm {
    reverse_proxy 127.0.0.1:8010 { header_up X-Forwarded-Proto https
                                   header_up -X-Real-IP }
}
```

## Not done here

Live walk-throughs in Claude, ChatGPT, Grok and Muse (`docs/grocery-mcp/INSTALL-PATHS.md` section 9: nothing is quoted
publicly before they pass - which is why `llms-install.md`, the registry manifests and the agent card are **unchanged**);
the `/connect` page; reviewer accounts and the directory submissions (item A1); `usage_events` rows that record a
challenge (the existing row says HTTP 200 for a response that left as 401); Arabic copy on the pages.

## What was checked about the email path (2026-10-03)

The brief asked to verify the existing key-request email path actually sends. Findings, none of which sent a message:
* The live container has `RESEND_API_KEY` set (presence and length only) and the sender domain `hatchloop.dev` is
  `verified` at Resend (read-only `GET /domains`).
* The release smoke test's `POST /keys/request` -> 503 `onboarding_unavailable` on 2026-10-01 was **Resend refusing the
  test address**: the container log has one `resend_send_failed status=422 ... domains like example.com`. It was not an
  outage. The old path folds "this address was refused" into the same 503 as "we could not send"; the Connect sender
  reports three outcomes (sent / rejected address / unavailable), so a mistyped address is a correctable error.
* Not exercised: a real message to a real inbox. That is the one post-deploy step that sends mail (see "Operating it").

## How it was reviewed

Five single-file adversarial reviews by DeepSeek v4-pro (router, client fetch, challenge, tokens, migration), then a
second round on the router after the match code was added. Real findings: consent phishing (fixed, section above) and a
cookie that named the right sign-in but proved nothing (fixed: the poll secret must hash to the sign-in's). The rest were
claims the code and tests refute (the database operations are atomic - ten simultaneous attempts, one winner - and the
store itself rejects a wrong poll secret). jev scored the consent text, the email and the confirmation page on typed
questions; the email scored 0.69 for "a stranger can safely ignore it" and was rewritten (0.93). 25 safeguards were
removed one at a time and a test failed each time. The official MCP SDK's OAuth client completes the flow against a live
server, and the OAuth tests also pass on the production-pinned stack (FastAPI 0.111.0, Starlette 0.37.2, httpx 0.27.0).

## Third review round (2026-10-03) and what it changed

A DeepSeek + jev pass, an independent reviewer who ran every claim, and the integration gate (which re-ran the repros)
found one P1 and five P2s; all are fixed and pinned by `tests/unit/test_oauth_connect_gate_fixes.py`, each of which
failed on the reviewed commit:
cross-site originator-cookie fixation (P1, account takeover: above); one flooded limiter clearing all of them; the
metadata fetch with no total deadline; cache hits spending the per-vendor fetch budget (two throwaway addresses could lock
every Claude user out); a bare 401 for agents with no OAuth support (they lost the free-key / credits / pay-per-call answer);
and a self-chosen app name printed in our email. Also fixed: `OAUTH_CHALLENGE_STYLE=off` now really adds nothing, and the
vendor-sized ceilings. *Not fixed, by decision:* the paid-account link is first-writer-wins from the buyer's unverified
checkout email, so someone who pays with another person's address claims that address's paid link (needs a real paid
order; fixing it means a pending state in the spine, left for the next migration).

## Tests

`tests/unit/test_oauth_connect_*.py`: flow (every scenario twice - in-memory store and the production `SpineStore` against
the real SQL), units, challenge, purchase link, SQL, and the official MCP SDK's own OAuth client against a live uvicorn
server (discovery by 401, registration or metadata document, PKCE, `resource`, refresh). The database-backed tests start
a throwaway postgres container and are opt-in: `OAUTH_PG_TESTS=1`.
