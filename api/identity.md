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

Tokens are issued by our `/auth/token` endpoint (not in v0.1 scope — use static test tokens from `/auth/dev-token` in dev/test environments).

In v0.1, agent registration is manual. The `agent_id` and issuer key are provisioned by the IntegrationAgent during onboarding.

---

## Implementation reference

See `/agent_interface/identity.py` for verification logic.
See `/compliance/audit_log.py` for the immutable decision log.
