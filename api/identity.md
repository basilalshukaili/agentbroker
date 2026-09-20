# Agent-Identity Specification

**Phase P3 Artifact | APIDesignAgent**

---

## Overview

Every API request carries an `Authorization: Bearer <jwt>` header.
The JWT is called the **Agent-Identity token**. It is verified at the edge
before any handler runs.

---

## JWT Claim Structure

```json
{
  "agent_id":  "<our-registry-assigned ID for this agent>",
  "principal": {
    "kind": "consumer" | "business",
    "id":   "<external identifier of the human or org on whose behalf the agent acts>"
  },
  "scope": {
    "operations": ["schedule_appointment", "send_message"],
    "budget_cap":  50.00,
    "verticals":   ["personal_services", "home_services"]
  },
  "expiry":  "2026-04-28T00:00:00Z",
  "issuer":  "<key-id from issuer registry>"
}
```

### Field rules

| Field | Required | Notes |
|---|---|---|
| `agent_id` | Always | Assigned when agent registers with us |
| `principal` | Required for state-changing ops | Not required for read-only ops (find_business, verify_business, preview_cost, self_test, get_status, get_outcome) |
| `scope.operations` | Always | Operations the agent is permitted to call; use `["*"]` to allow all |
| `scope.budget_cap` | Always | Maximum USD the agent may spend in this token's lifetime |
| `scope.verticals` | Optional | Restrict to specific verticals; omit for all verticals |
| `expiry` | Always | Max 24h from issuance for state-changing scopes |
| `issuer` | Always | Key ID in our issuer registry; used to select the verification public key |

---

## Authorization Rules

1. **Signature**: HMAC-SHA256 or RS256. Key selected from issuer registry by `issuer` claim.
2. **Expiry**: Tokens past `expiry` are rejected with `policy_blocked: expired_token`.
3. **Scope enforcement**:
   - The requested operation must be in `scope.operations` (or `*`).
   - The requested vertical (inferred from SMB) must be in `scope.verticals` (if set).
   - The estimated cost must not exceed `scope.budget_cap`.
4. **Principal requirement**: Any operation that sends a message, charges money, books an appointment, or modifies state for a real recipient requires a `principal` claim. The principal is the legal party authorizing the agent's action. Liability flows to the principal under the authorized scope.
5. **Out-of-scope rejection**: Returns `policy_blocked` with `reason: out_of_scope` and a `required_scope` field describing what is missing.

---

## Audit Log

Every authorization decision — allow or deny — is written immutably to the compliance audit log with:
- `agent_id`
- `principal`
- `operation`
- `smb_id` (if applicable)
- `decision`: allow | deny
- `reason` (if deny)
- `timestamp`
- `token_hash` (SHA-256 of the JWT, not the JWT itself — PII-safe)

---

## Token Issuance

**Not `/auth/token`.** That route mints tokens for paying subscribers and is
gated by an `X-Admin-Secret` header no outside caller holds; it is disabled
outright when `ADMIN_SECRET` is unset on the server, which is production's
state today. It was never a public issuance endpoint, and `/auth/dev-token`
does not exist in this service — do not build against either.

The route an integrator actually uses is the free, email-verified key flow:
`POST /keys/request {"email": ...}`, then open the verification link that
arrives by email. That link's confirmation page shows the key (also emailed).
This currently requires a human to click the link — there is no
machine-mintable path in production today; `POST /keys/mint` (HMAC self-serve,
no email) exists in the code but returns `503 {"error": "not_configured"}` and
is not something to build against. If email delivery itself is down,
`/keys/request` answers `503 {"error": "onboarding_unavailable"}` rather than
a false `verification_sent` — treat that as "email onboarding requires an
operator right now" and contact hello@hatchloop.dev for manual provisioning.

Paid-plan tokens (`developer` / `business` / `enterprise`) are issued from
`/auth/token` by an operator after a completed Polar order, or automatically
by the Polar webhook — never by an integrator calling it directly.

---

## Implementation reference

See `/agent_interface/identity.py` for verification logic.
See `/compliance/audit_log.py` for the immutable decision log.
