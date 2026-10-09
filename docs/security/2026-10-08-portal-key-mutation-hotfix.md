# Portal key mutation hotfix — source-only handoff, 2026-10-08

Status: **source-only; rotation and portal key creation intentionally unavailable**.
Implementation revision: `d89e18b2ebbba8ed7983233a809e0bc6e5328e46`.
Exact base: protected live `b3c7906ccbf4f12b14857aef9e24c2adb2dda455`.
Branch: `security/portal-key-rotation-20261008`.
Isolated checkout: `C:\TechMate\tmp\agentbroker-portal-key-rotation-20261008`.

This checkout was created from the named protected revision, not dirty main or
the screening candidate. The parent task explicitly selected a fail-closed
mutation gate after the existing storage contract could not establish atomic
replacement/revocation or peer visibility. No schema migration was authored or
applied. No production database, real keys, provider requests, messages, push,
deployment, release, payment switches or runtime ownership state were touched.

## Resulting behavior

- An authenticated `POST /portal/key/regenerate` returns HTTP 503,
  `ok: false`, `reason: key_mutation_unavailable`, before account lookup,
  issuance, revocation or any write. There is no environment switch to bypass
  the gate.
- `POST /portal/key/generate` preserves the existing read-only
  `ok: true, already: true` response for an account with a stored key. Missing
  account/key returns the same HTTP 503. It cannot create/overwrite an account,
  reset a balance, mint an orphan key or store agent_id as key_jti.
- `POST /portal/key/reveal` retains authenticated disclosure of the stored
  account key. No account retains `ok: false, reason: no_account`; an account
  without a key receives HTTP 503 and never mints/discloses an unpersisted key.
- Removed the independent portal PATCH helper and subscription issuance
  fallback. Thus free identities/scopes/caps cannot be promoted by portal
  rotation, and account-write failure cannot be reported as successful issuance.
- `identity.revoke_token` obtains the JTI from the verified signed token, never
  account metadata. `revoke_jti` confirms an exact matching insert result; a
  failed insert, duplicate or lost acknowledgement can succeed only after a
  strict read confirms exactly that JTI already exists. Retries preserve the
  original revocation reason/timestamp. Wrong/empty/malformed results and denied
  confirmation return false; the local rejection remains active. Durability
  is not inferred from a conflict code or the in-memory set.

Owned implementation/test paths: `agent_interface/portal.py`,
`agent_interface/identity.py`, `tests/unit/test_portal.py`,
`tests/unit/test_jti_revocation_durability.py`,
`tests/unit/test_portal_key_safety.py`, `tests/unit/test_jti_revocation_retry.py`.
All were reviewed by exact diff and committed explicitly. Existing four files
retain CRLF working-tree line endings; committed Python content is LF.

## Local verification

Final content at implementation revision: **278 passed, zero failed, zero
skipped**, in 2.75 seconds. One existing Starlette TestClient deprecation warning.
Actual runtime: Python 3.12.10, pytest 9.1.1, FastAPI 0.141.1. All six owned Python
files parse with Python 3.11 grammar (`ast.parse(feature_version=(3, 11))`). This
is a 3.11-compatible focused subset, **not proof of execution on Python 3.11**;
that runtime is unavailable locally. No full-suite or PostgreSQL proof is claimed.

Modules exercised: portal helpers/email builders, new portal HTTP safety tests,
JTI durability and retry fixtures, identity/customer revocation, complete
hydration, portal topup payment gate, honest key-request failures, machine mint,
and OAuth connection units. The existing billing/paid rails remain unchanged.

The guard strips all provider/application environment settings before imports,
uses only synthetic signing secrets and `.invalid` endpoints, denies external
socket connections and DNS, and permits loopback for Windows asyncio's socket
pair. New HTTP tests use in-process ASGI transport. Revocation transport tests
use `httpx.MockTransport` plus a synthetic dictionary with duplicate-key and
lost-ack responses; they never connect to a database. Forty concurrent rotation
requests across two application instances all return 503, without issuance,
revocation, account changes or replacement disclosure. These are proofs of
disabled mutation safety, not proofs of working cross-process rotation.

An initial overly strict guard denied Windows asyncio's own loopback socketpair
and produced 153 harness failures/125 passes. Its log is retained as
`C:\TechMate\tmp\agentbroker-portal-key-gate-initial-20261008.log`. Allowing only
loopback corrected the harness; the final guarded run above passes.

Reproduction runner: `C:\TechMate\tmp\agentbroker-portal-key-gate-20261008.py`
with the existing `C:\Users\basil\AppData\Local\Programs\Python\Python312\python.exe`.
Run from any cwd; it selects the exact isolated checkout and targets ten modules.
Final local evidence:

| Artifact | SHA-256 |
|---|---|
| `C:\TechMate\tmp\agentbroker-portal-key-gate-20261008.log` | `04478536b9810f551837abd80d3cd72ffb4cb2fdeacf627e1424c601e5dadedd` |
| `C:\TechMate\tmp\agentbroker-portal-key-gate-20261008.xml` | `d9a3191522807f7203575d5326a0390935422d89f13509cdcdb624aa589aec0e` |
| `C:\TechMate\tmp\agentbroker-portal-key-gate-20261008.py` | `cb7023f24d66b2bd5790cadfe63703aa462620e6ac8ecaa35614dc891d97ffec` |

`git diff --check` passed. The implementation checkout was clean after its
explicit-path commit, before this documentation-only handoff was added.

## Unresolved gates and next action

1. Keep key mutations disabled. Functional rotation requires a scoped atomic
   compare-and-swap transaction over the current stored key/JTI and durable
   revocation, exact account persistence confirmation, safe lost-ack retries,
   and explicit free entitlement preservation. Historical key_jti metadata may
   contain agent_id: derive/verify actual JTI from the signed stored token;
   malformed/ambiguous legacy state must fail closed. A reviewed schema/RPC
   decision and isolated PostgreSQL role/concurrency/crash fixtures are required
   before implementing or enabling that path.
2. Already-hydrated peers still latch the JTI revocation list once. This source
   patch deliberately does not claim global immediate revocation or change the
   authorization cache. Prove fresh peer visibility and fail-closed unavailable
   status checks before allowing replacement; a durable local write alone is
   insufficient. Current anon-only deployment may deny both the direct revoke
   write and strict readback. That must remain failure, never be bypassed with a
   credential or production permission change in this task.
3. Before any backend deployment, pair the candidate with reviewed portal UI
   changes in the separate website repository. The parent identified
   `web_hatchloop_v2/src/app/portal/page.tsx:359` confirming “Your current key will
   stop working immediately” and exposing Regenerate. Its buttons/copy and
   503 handling must reflect unavailability. That repository was not edited
   here. Verify the visitor-facing result and the established AEO audit within
   separately authorized shipping scope.
4. Independent source review, exact-candidate required checks, applicable
   publication/deployment authority and rollback planning remain release gates.
   This receipt grants none of them. Local compatibility checks do not replace
   the missing Python 3.11/Linux or PostgreSQL fixture gates.

No deployment occurred, so no runtime rollback is needed. For source rollback,
revert the owned implementation commit in an isolated reviewed branch, or
discard its use as a candidate while retaining this worktree/evidence. Reverting
to base `b3c7906` reintroduces the unsafe portal mutations; it is not a security
remediation. Any actual release/rollback uses the existing reviewed named-commit
deployment process and fresh live evidence, never this dirty/shared workspace.

Active handoff: source implementation complete; retain isolated worktree and
evidence for independent review. No founder requirement was hand-marked done.
Parent owns workspace/CEO handoff integration and separate website decisions.
