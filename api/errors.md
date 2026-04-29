# Error Taxonomy

**Phase P3 Artifact | APIDesignAgent**

Every error returned by any operation MUST be machine-readable and actionable.
All errors follow the `APIError` schema defined in `/core/models.py`.

---

## Error Code Reference

| Code | Category | Retriable | Description | next_action |
|---|---|---|---|---|
| `bad_input` | client_error | false | Request schema validation failed or a required field is missing | Fix the request body per the schema for this operation |
| `missing_capability` | client_error | false | The requested service capability is not available from this SMB or in this vertical | Use find_business to locate an SMB with this capability |
| `rate_limited` | client_error | true | Per-agent or global rate limit hit | Respect retry_after_ms; use preview_cost before batch operations |
| `upstream_failure` | server_error | true | An external channel (Twilio, Vapi, Cal.com, etc.) returned an error | Retry after retry_after_ms; if persistent, try a different channel via preferred_channel |
| `policy_blocked` | policy_error | false | Agent-Identity scope does not cover this operation, vertical, or budget | Include reason sub-field; obtain scope from issuer covering this operation+vertical |
| `budget_exceeded` | policy_error | false | Request cost exceeds the Budget-Cap header or the agent's configured budget | Reduce Budget-Cap or use preview_cost to understand expected cost |
| `idempotency_conflict` | client_error | false | An Idempotency-Key was reused with different parameters | Generate a new Idempotency-Key for the different request |
| `transient` | server_error | true | Temporary internal error (DB timeout, network blip) | Retry after retry_after_ms; if persistent, call self_test |
| `internal` | server_error | false | Unexpected internal error | Report to support with trace_id |
| `supply_unreachable` | server_error | true | The target SMB could not be reached via any available channel | Try again after retry_after_ms; consider escalate_to_human |
| `supply_unverified` | client_error | false | The SMB exists in the directory but its capability/availability could not be confirmed | Call verify_business before proceeding |
| `out_of_supply_network` | client_error | false | No SMBs in the supply network match the given criteria | Expand search radius or try a different vertical/capability |
| `compliance_violation` | compliance_error | false | The request was blocked by a compliance pre-check | See violation_detail field; obtain required consent or adjust the message/channel |
| `out_of_scope` | policy_error | false | The operation is outside the authorized scope in the Agent-Identity JWT | Update scope in the Agent-Identity JWT; see required_scope field |
| `consent_missing` | compliance_error | false | No valid consent record found for this recipient+channel+use_case | Obtain explicit consent and record it before sending |
| `recording_consent_missing` | compliance_error | false | Voice recording requested for a two-party-consent jurisdiction without confirmed consent | Provide recording_consent_confirmed=true only after presenting the consent prompt to the recipient |

---

## Error Response Shape

```json
{
  "code": "compliance_violation",
  "category": "compliance_error",
  "retriable": false,
  "message": "Recipient +14045550200 has not opted in to marketing SMS. TCPA prior express written consent is required.",
  "next_action": "Obtain TCPA-compliant written consent for this recipient before sending marketing messages to this number.",
  "violation_detail": {
    "rule": "TCPA_marketing_consent",
    "recipient_id": "+14045550200",
    "channel": "sms",
    "jurisdiction": "US"
  },
  "trace_id": "tr_abc123xyz"
}
```

---

## Failure Class Taxonomy (for OutcomeOptimizationAgent attribution)

| Failure class | Meaning |
|---|---|
| `discovery_miss` | Agent could not find the right SMB — supply coverage or find_business ranking issue |
| `selection_miss` | Agent chose a competitor instead of us — manifest/description issue |
| `param_misuse` | Agent called us with wrong parameters — schema or example issue |
| `execution_failure` | We found the right SMB but the operation failed — channel or logic issue |
| `outcome_rejected` | Agent received the result but rejected it as not good enough — quality issue |
| `supply_unreachable` | SMB not reachable via any channel — supply network gap |
| `compliance_violation` | Request blocked by compliance pre-check — consent/jurisdiction issue |
| `environmental` | External factor (carrier outage, upstream API down) caused failure |
