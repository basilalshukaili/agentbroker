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

## Which rule a compliance refusal names

`violation_detail.rule` names the law that was applied, and only where the gate implements it:
`TCPA_marketing_consent`, `TCPA_quiet_hours` and `10DLC_campaign_not_registered` for US recipients,
`GDPR_marketing_consent` for the EU/UK states modeled, `CASL_marketing_consent` for Canada.

For every other country the gate applies the service's own conservative default and names it as such:
`sms_marketing_consent` (recorded opt-in is required for marketing SMS), `voice_marketing_consent`,
`email_marketing_consent`, `quiet_hours` (a 08:00-21:00 local solicitation window unless one is modeled). The
message then says that no statute of that country was applied, and `check_compliance` returns the same fact as
`rule_set` (`basis: "conservative_default"`, `statutes_modeled: []`). Marketing on WhatsApp needs a recorded
opt-in on WhatsApp in every country (`whatsapp_marketing_consent`; an opt-in given for SMS, email or calls does
not cover it), and marketing on any other channel the gate has no rule for is refused the same way
(`marketing_consent`) rather than allowed. 10DLC carrier registration is a US rule and is applied only to a US
recipient or to a +1 number whose country is not settled, never to a number that cannot be American.

`jurisdiction_required` means the country could not be determined: pass `country_code`, or an E.164 recipient
number whose country calling code names it (a +1 or +7 number needs `country_code`). When the recipient number
names exactly one country and `country_code` names another, what happens depends on the message. For a marketing
or follow-up message the gate does not choose between them: it refuses with `jurisdiction_conflict` (the answer
names both countries and `rule_set.basis` is `undecided`), because calling hours and carrier rules follow where
the recipient is, which two different answers do not settle. For any other message type the number's country is
used and the answer says so in `jurisdiction_conflict`. `check_compliance`, `send_message` (in its refusal and in
its success result) and the public HTTP check all report it, and the HTTP check also returns `rule_basis`. Common
spellings of a country (`UK`, `USA`, the three-letter ISO codes) are read as the country they name.

---

## Argument errors on MCP `tools/call`

A call whose arguments have the wrong JSON type is refused before anything is run, held or charged, as JSON-RPC
`-32602` with `data.error_code: "invalid_argument"` and `data.retriable: false`. The message names each argument,
the type it must have and the type that arrived (never the value); `data.invalid_fields` lists them and
`data.expected_types` maps each argument path (`name`, `prospect.name`, `parties[1]`) to its declared type.
Retrying unchanged fails identically. `integer` accepts `5` and `5.0`, never `true`, `1.5`, `"5"` or a
non-finite number. An explicit `null` for an optional argument means "not given"; for a required one it is a
type error. Every item of an array is checked, wherever it sits; a call that holds more than 50,000 values in
all (array items and fields together) is refused as too large rather than checked in part.

Three arguments are accepted in more than their declared type, on purpose, and nothing else is: `find_business`
interprets the shapes callers really write (for example `location` as a plain string) and answers the rest in
its own words; `send_message.content` may be a bare string, which is read as the message body; and
`mint_key.timestamp` may be a numeric string, which the tool converts and answers in its own words if it is not
a number.

The tool `name` of a `tools/call` must be a string (a list or an object is the same `-32602`), and the request's
`method` must be a string (`-32600`). On the write tools `idempotency_key` must be a non-empty string of at most
128 characters: any other type, an empty or blank string, or a longer one is refused as above, so a retry key is
never silently ignored or cut short; `null` means "not given". `params` must be a JSON object; an absent or
`null` `params`, and the empty values `{}`, `[]`, `0`, `false` and `""`, are treated as no parameters.

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
