# Discovery documents: what is served, where, and why it is true

> **Status: implemented on branch `fix/discovery-hygiene-20261004`, built on `4e46f8e` (release 1, build `48e8b62`). It is
> NOT deployed.** Nothing below is live until the origin is deployed through the gated wrapper and the Caddy change is
> installed; each section says which of the two it needs. Verdict item A8 of
> `docs/reviews/2026-10-03-mcp-focus-verdict.md` plus the release-1 leftover about `hatchloop.dev/.well-known/*`.

## What was measured

Production Caddy access logs on the box, 2026-09-30 to 2026-10-04 (13 files, counts only; no address kept), and public GETs
on 2026-10-04.

| Request | Answered | Count in 4 days | Who asks |
|---|---|---|---|
| `/.well-known/glama.json` | 404 | 268 | Glama's checker (empty User-Agent) |
| `/.well-known/x402.json` | 404 | 101 | a directory crawler, curl, node |
| `/.well-known/mcp/server-card.json` | 404 | 36 | four scanners, two directories |
| `/.well-known/mpp`, `/.well-known/payment-manifest` | 404 | 97 | one directory crawler |
| `/.well-known/oauth-protected-resource/mcp/<door>` on the site | 200, **wrong `resource`** | (404 before release 1) | scanners, directories, connector probes |
| retired doors | tombstone, 200 | was 1,849 x 410 | done in release 1 |

The verdict's wording ("serve glama.json, the server card and an x402.json alias") hid the exact paths. They are all under
`/.well-known/`.

## The rule

A document exists exactly when the thing it describes exists. A route that always answers 200 silences the scanner and
makes a claim while doing it, which is the opposite of what a directory score measures. So:

| Path | Served when | Otherwise | Needs |
|---|---|---|---|
| `/.well-known/mcp/server-card.json` | always (SEP-1649 draft shape, derived) | | origin deploy |
| `/.well-known/glama.json` | `GLAMA_CLAIM_TOKEN` is set and well formed | 404 | origin deploy **and the founder's token** |
| `/.well-known/x402.json` | `/.well-known/x402` is (the same answer: its own handler is run) | 404, the primary's own | origin deploy, **and** branch `feat/x402-advertise-20261003` for the document itself |
| `/.well-known/mpp`, `/.well-known/payment-manifest` | never | 404 | nothing: we implement neither |
| OAuth block in `mcp.json`, the discovery card, `llms.txt`, the card | `OAUTH_CONNECT_ENABLED` is not off | omitted | origin deploy |

### The server card

`GET /.well-known/mcp/server-card.json`, both hosts (the site proxies `/.well-known/*` to the origin). SEP-1649 is a draft and
is not part of the 2026-07-28 revision, whose in-protocol answer is `server/discover`; the card says so in `_meta`. Every
field is read from the code that makes it true: the protocol versions from the list `server/discover` answers, the identity
and capabilities from the handshake, the tool list and each tool's readiness through the same two steps `tools/list` takes
(manifest, then this deployment's delivery-channel status, so voice shows `unavailable` here exactly as `tools/list` says),
who needs a key from `core/tool_auth.py`, the sign-in from the OAuth router's own metadata. It carries no price, no credit
and no x402 wording (those belong to the payments block and the switches that decide them) and names no assistant.

### glama.json is a claim token, not a manifest

Glama issues a claim token to the account that owns the listing and re-checks that this exact file is served from the
connector's origin: `{"$schema": "https://glama.ai/mcp/schemas/connector.json", "claim": "glama_claim_<32 characters>"}`
and nothing else. The token is public by design and carries no personal data; if the file disappears the claim lapses after a
grace period. We hold no token, so until one is configured the answer is 404, and a value that is not shaped like a token is
never echoed (the log records its length only). The repository's own `glama.json` (maintainers, tags) is the repo-side file
and is unchanged.

### x402.json

The same answer as `/.well-known/x402`: the 200 document byte for byte, and the 404 body as well. The alias builds nothing: it
runs the primary route's own handler (`well_known_x402` in `main.py`, added by branch `feat/x402-advertise-20261003`, which asks
`billing.x402_gate.discovery_document` and raises a 404 unless the gate accepts payment), so there is one writer of both
answers and they cannot differ. On a build with no such route there is nothing to alias; the answer is the framework's own 404,
which is also what `/.well-known/x402` answers there, so the two still match. It starts answering the moment that branch is
merged, with no further change. If the handler fails for a reason of its own the alias is a 404 and the log records the
exception TYPE only (never its message). `mpp` and `payment-manifest` are deliberately not aliased.

An earlier version of the alias called `discovery_document` itself and answered its own `{"detail": "Not Found"}` for the 404
case. In a scratch merge with the x402 branch the primary's 404 body names the host, so the two differed, and the live
verifier (which compared bytes) failed a correct system. Found in review on 2026-10-04; the tests now register the primary
route exactly as that branch writes it and compare status and bytes unconditionally, in every state, and the verifier
compares bytes only where there is a document (two 404s are the same answer whatever their bodies say).

## Release 1 reaches the other documents

Release 1 added OAuth Connect and MCP 2026-07-28. `/llms.txt`, `/.well-known/mcp.json` and `/.well-known/agent-service`
still said "get a key by email" and named no protocol version. They now carry one derivation from
`agent_interface/discovery_auth.py`: the sign-in (endpoints, grants, PKCE method, registration styles and scopes read from
the router's metadata; the tools that need an account from `core/tool_auth.py`) and the protocol versions (the list
`server/discover` answers, split into the modern era and the legacy era that opens with `initialize`).

Which document carries which: the sign-in is in `/.well-known/mcp.json` (`auth.oauth2`), the discovery card
`/.well-known/agent-service` (`auth.oauth2`), the server card and `/llms.txt`; the protocol versions are in
`/.well-known/mcp.json` (`protocol_versions`), the server card and `/llms.txt`. **The discovery card carries the sign-in only:
it has no protocol-version field**, and `live_verify_discovery.py --only documents` does not look for one there.

The sentences added to the card and to `/llms.txt` say "a tool marked `requiresKey`" and "every other tool", never "the free
tools": free (costs nothing) and keyless (needs no key) are different sets (`core/tool_auth.py`), and two tools are in the
first but not the second (`get_conversation`, `import_booking_url`).

Deliberately **not** done, because it would be a claim the system cannot back: no document names Claude, ChatGPT, Grok or
Muse (docs/OAUTH-CONNECT.md "Not done here": nothing is quoted about an assistant before it has been walked through live),
and the generated registry files (`server.json`, `smithery.yaml`, `glama.json`, `registry/*`) are unchanged, because the
registry schema has no place for OAuth and their counts were re-checked against the running server (23 tools, 14 usable
without a key). The edge snapshot `edge/src/snapshots/mcp.json` was regenerated with
`scripts/refresh_edge_snapshots.py --local-routes mcp.json`; the edge worker is not in the live path.

**Before the edge worker is ever put in the path:** the snapshot is compiled with `OAUTH_CONNECT_ENABLED` at its default (on)
and carries `auth.oauth2` and `protocol_versions` as static text, and the worker serves its embedded snapshot when its KV is
empty. If it were in the path while the origin ran with `OAUTH_CONNECT_ENABLED=0`, it would advertise a sign-in the origin
answers 404 for. Either compile the snapshot per switch state, or have the refresh script refuse to compile `oauth2` unless
the target deployment's switch is on. Today public GETs to `api.hatchloop.dev/.well-known/mcp.json`, `/llms.txt` and
`/.well-known/agent-service` carry no `x-edge-source` header (only `Via: Caddy`), so the worker is not serving them.

### OAUTH_ISSUER must equal PUBLIC_BASE_URL

The card's `protected_resource_metadata_url` is built from the OAuth issuer; its `transport.endpoint` is built from the public
base URL. They are the same host in every deployment so far (`OAUTH_ISSUER` is unset and defaults to the API host), and the
release-1 401 challenge builds its metadata URL from the issuer by the same design. A deployment that set `OAUTH_ISSUER` to a
different host would publish a protected-resource document whose `resource` differs from the URL the card tells a client to
connect to, which RFC 9728 says to refuse. Treat setting them apart as a conscious change that moves the card with it;
`test_the_cards_sign_in_url_is_on_the_host_it_tells_a_client_to_connect_to` fails the day the defaults stop matching.

## The site-host decision: Caddy route, not a Next.js rewrite change

**Finding (public GETs, 2026-10-04).** The release-1 receipt said `hatchloop.dev/.well-known/*` "still goes to Next.js, so a
client probing the SITE URL finds no OAuth metadata". It does find it: `web_hatchloop_v2/next.config.ts` has had a rewrite
`/.well-known/:path*` to `https://api.hatchloop.dev/.well-known/:path*` all along. What it finds is wrong for the doors. That
proxy replaces the Host header with the destination's, the origin builds a protected-resource document's `resource` from Host,
and so

```
GET https://hatchloop.dev/.well-known/oauth-protected-resource/mcp/sanctions-screening
  now:  "resource": "https://api.hatchloop.dev/mcp/sanctions-screening"
  must: "resource": "https://hatchloop.dev/mcp/sanctions-screening"
```

A client that connected to the site URL and validates `resource` against it (RFC 9728 section 3.3; the MCP authorization
specification says it MUST) refuses the metadata and never offers a sign-in. This affects all five doors and the bare path
(6 of the 7 site paths checked); the full server's URL works only because the origin special-cases `/mcp/agent-broker`. The 401 a connector gets from a
door carries an explicit metadata URL with `?host=hatchloop.dev`, so that path never depended on the rewrite; discovery by
probing, which is what ChatGPT does when a connector is created, does. `scripts/live_verify_discovery.py --only prm` shows it
today (6 of 7 site paths wrong, the API host right on all 7).

**Two fixes would work.** Add `?host=hatchloop.dev` to the Next.js rewrite, or route this one document family from Caddy
straight to the container with Host intact. **Caddy**, because it is the way this estate has fixed the same defect twice
(`mcp_direct`, `mcp_retired`: one installer, one probe list, automatic restore on failure, a rollback that needs no site
deploy); the site is a separate repository that auto-deploys its working tree every 30 minutes, so an edit there ships on a
timer instead of as a decision; it removes the Next.js hop from discovery (the hop that returned 502 whenever the site
restarted, before `mcp_direct`); and with Host left alone the origin sees the host the client actually asked on. (It builds
`resource` from that Host, or from a `?host=` hint which it honours first; either way only from a closed list of known hosts,
so nothing a caller sends can name a host that is not ours. The 401 challenge uses the hint on purpose.)

Scope is the protected-resource family only (`/.well-known/oauth-protected-resource` and `/*` under it): the only discovery
document that depends on Host. Every other `/.well-known/*` path is Host-independent and keeps going through the rewrite, which
stays as the fallback. No wildcard over `/.well-known/`, no `header_up Host`. Needs **no origin deploy**: build 48e8b62
already answers by Host, and Caddy already hands it the site's Host on the existing direct route (checked live on
2026-10-04: a refused `get_conversation` on `https://hatchloop.dev/mcp/agent-broker` answers 401 whose `resource_metadata` carries
`?host=hatchloop.dev`, while the same call on `https://api.hatchloop.dev/mcp` carries no such hint).

## Ship steps

**Caddy** (independent of the origin; the box and the founder's go-ahead are the only inputs):

```
python deploy/caddy/install_mcp_direct.py plan    --change oauth_prm --target root@<box> --key <key>     # read-only: diff + `caddy adapt`
python deploy/caddy/install_mcp_direct.py install --change oauth_prm --target root@<box> --key <key> --yes   # backup, validate, reload, probes, auto-restore
python scripts/live_verify_discovery.py --only prm                                                           # the fix, from outside
python deploy/caddy/install_mcp_direct.py rollback --target root@<box> --key <key> --yes                     # if ever needed
```

`plan` was run on 2026-10-04 against the real Caddyfile (sha256 `43c377cb9fe5999a...`, LF): the candidate adapts cleanly and the
diff is additions only, committed as `deploy/caddy/oauth_prm.patch` (zero context, so it quotes none of the live file). The
installer refuses to act if the live file's hash no longer matches the plan.

**Origin:** merge into the release branch, regenerate the edge snapshot after merging siblings that also change `mcp.json`
(`python scripts/refresh_edge_snapshots.py --local-routes mcp.json`), run the gates, deploy with
`ops/vps/deploy_agentbroker_gated.py <sha>`, then `python scripts/live_verify_discovery.py --only card,documents,glama,x402,not_implemented`.
`GLAMA_CLAIM_TOKEN` is read with a non-empty default (`none`), so no deploy requires it
(`scripts/check_deploy_env.py` treats an empty default as required).

**Glama (the founder, one login):** read the claim token off Glama's claim panel for the listing and put
`GLAMA_CLAIM_TOKEN=glama_claim_...` in the container's environment file, then swap the container; `live_verify_discovery.py --only glama`
checks the file from outside. Until then 404 is correct and the 67-a-day checker keeps getting it.

## Not done, on purpose

* `/.well-known/mcp` (SEP-1960), `/.well-known/ai-catalog.json` (62 requests), `/.well-known/ard.json`,
  `/.well-known/agent-directory.json`: drafts or private formats with no stable shape to be true to.
* `/mcp/<door>/.well-known/*` probes from one registry scanner (about 80 a day, 404): not a location any standard defines.
* Path-inserted authorization-server metadata (`/.well-known/oauth-authorization-server/mcp/<door>`): RFC 8414 requires the
  `issuer` in the document to equal the URL it was inserted into, which ours does not, so a 404 is the correct answer; the
  protected-resource document is how a client finds the issuer.
* The A2A card still declares `streaming` and `pushNotifications` that `message/send` does not honour (verdict item A6).
* The site's own `/llms.txt` and `/llms-install.md` are Next.js routes in the site repository, not this origin.
* An older sentence in this origin's `/llms.txt` install paragraph ("no key is needed for the free tools") predates this
  branch and carries the same free/keyless ambiguity; it is outside this item and was left as it is.
* "Tolerate junk after `/mcp/agent-broker`": those are pageviews on the site, answered by Next.js.
