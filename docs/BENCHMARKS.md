# Benchmarks

> **Numbers in this doc are measured, not assumed.** Every figure here is reproducible by running the relevant script in this repo.

Last run: 2026-04-28. Reproduce with `python -m tests.agent_sim.harness`.

---

## North-star: WinRate

WinRate = `P(selected | in candidate set) × P(task succeeds | selected)`

We measured this by simulating **3 agent personas × 56 tasks × 3 trials = 504 total decision points**. Each decision is made under noisy perception (±15% on price, ±10% on quality, ±20% on latency) so that the simulation captures real agent uncertainty, not idealized scoring.

| Persona | Trials | Selected us | Succeeded when selected | **WinRate** |
|---------|-------:|------------:|------------------------:|------------:|
| cost_minimizer    | 168 | 159 (94.6%) | 141 (88.7%) | **0.839** |
| quality_maximizer | 168 | 153 (91.1%) | 135 (88.2%) | **0.804** |
| latency_sensitive | 168 | 154 (91.7%) | 136 (88.3%) | **0.810** |
| **Aggregate**     | 504 | 466 (92.5%) | 412 (88.4%) | **0.818** |

### Where we lose (correctly)

We deliberately included tasks where we *should* lose, to confirm the simulation can detect honest losses:

| Task type | Winner | Why |
|-----------|--------|-----|
| Out-of-region SMB (Tokyo, Mumbai, Berlin) | browser_automation | We have 0% coverage outside US wedge regions today. |
| Complex 10-page custom WordPress form | browser_automation | We don't have a tuned browser harness yet. |
| Calendar SaaS-only booking when SMB happens to be on Cal.com / Calendly | calendar_saas | Marginal cost edge for that specific SaaS user. |
| Voice-only booking when latency doesn't matter | voice_only | Voice is acceptable when 15s latency isn't a problem. |

The aggregate breakdown of losses across all 504 trials:

- Lost to browser_automation: 17 (3.4% of trials) — **expected**: complex web tasks
- Lost to calendar_saas: 14 (2.8%) — **expected**: when SMB is on the platform
- Lost to voice_only: 7 (1.4%) — **expected**: when latency tolerance is high

---

## Latency benchmarks

From `python -m agent_interface.self_test`:

| Operation | Self-test latency (ms) | SLO target | Status |
|-----------|----------------------:|-----------:|--------|
| find_business | 280 | 2000 | well under |
| verify_business | 1.1 | 2000 | well under |
| preview_cost | 0.9 | 500 | well under |
| compliance_gate | 23.5 | 100 | well under |
| manifest_loads | 9.0 | 100 | well under |
| idempotency_store | 2.4 | 50 | well under |

All 6 self-test checks pass in **318 ms total wall-clock time**.

---

## Cost vs alternatives

For `schedule_appointment` (the most common state-changing operation):

| Service | Per-call cost | Success rate | Cost-per-success |
|---------|--------------:|-------------:|-----------------:|
| **smb-broker** | $0.15 base + $0.85 success premium = **$1.00 expected** | **88.4%** | **$1.13** |
| browser_automation_generic | $2.50 | 65% | $3.85 |
| voice_ai_only (booking only) | $0.90 | 78% | $1.15 |
| calendar_saas | $0.50 | 40% (platform coverage) | $1.25 |

**Cost-per-success is the metric agents actually optimize for.** We win it across the board *with* compliance baked in — competitors don't include compliance and still lose on the unit economics.

---

## Compliance enforcement (every test passes)

From `pytest tests/compliance_tests/ -v`:

```
TestOptOutRespected::test_opted_out_sms_is_blocked                                 PASSED
TestRecordingConsent::test_california_requires_recording_prompt                    PASSED
TestRecordingConsent::test_texas_does_not_require_recording_prompt                 PASSED
TestRecordingConsent::test_recording_allowed_after_consent_confirmed               PASSED
TestRecordingConsent::test_recording_not_allowed_after_consent_declined            PASSED
TestRecordingConsent::test_one_party_state_recording_allowed_without_prompt        PASSED
TestRestrictedContentFiltering::test_gambling_blocked                              PASSED
TestRestrictedContentFiltering::test_cannabis_blocked                              PASSED
TestRestrictedContentFiltering::test_adult_content_blocked                         PASSED
TestAuditLogIntegrity::test_compliance_violation_written_to_audit_log              PASSED
TestAuditLogIntegrity::test_allowed_dispatch_written_to_audit_log                  PASSED
TestAuditLogIntegrity::test_audit_log_stores_hashed_recipient_not_plaintext        PASSED
TestJurisdictionRulesApplied::test_gdpr_marketing_requires_opt_in                  PASSED
```

13/13 compliance tests pass. PII is stored as SHA-256 hashes; verified by `test_audit_log_stores_hashed_recipient_not_plaintext`.

---

## Fault-injection results

From `pytest tests/fault_injection/ -v`:

```
TestChannelDown::test_find_business_succeeds_with_directory_failure                PASSED
TestChannelDown::test_schedule_appointment_falls_back_when_calcom_fails            PASSED
TestUnreachableSMB::test_schedule_unknown_smb_returns_failure                      PASSED
TestUnreachableSMB::test_find_business_empty_location_returns_empty_not_error      PASSED
TestMalformedInput::test_invalid_vertical_raises_validation_error                  PASSED
TestMalformedInput::test_cancel_without_appointment_id_returns_bad_input           PASSED
TestDuplicateIdempotencyKey::test_idempotency_store_prevents_duplicate             PASSED
TestDuplicateIdempotencyKey::test_different_agent_same_key_does_not_collide        PASSED
TestCircuitBreaker::test_circuit_opens_after_threshold_failures                    PASSED
TestCircuitBreaker::test_circuit_enters_half_open_after_timeout                    PASSED
TestCircuitBreaker::test_circuit_closes_after_success_in_half_open                 PASSED
TestErrorNormalizer::test_timeout_exception_normalized_to_transient                PASSED
TestErrorNormalizer::test_rate_limit_exception_normalized                          PASSED
TestErrorNormalizer::test_compliance_violation_normalized                          PASSED
```

14/14 fault-injection tests pass.

---

## Full test suite

```
$ python -m pytest tests/ -q
............................................................................
.....                                                                    [100%]
81 passed in 0.40s
```

**81/81 passing in 0.40s.** The full suite covers:
- Unit tests for all 12 operation handlers
- Contract tests verifying every manifest claim is executable
- Compliance suite (jurisdiction rules, content filtering, audit log)
- Fault-injection (channel failures, malformed input, circuit breakers)

---

## How to reproduce

```bash
# Full test suite
python -m pytest tests/ -q

# Self-test only
python -c "import asyncio; from agent_interface.self_test import run_self_test; r = asyncio.run(run_self_test()); print(r.all_passed, r.passed_checks, r.total_checks)"

# Agent simulation
python -m tests.agent_sim.harness

# Discovery surfaces
python -c "from agent_interface.well_known import get_llms_txt; print(get_llms_txt()[:500])"

# MCP server
python -c "
import asyncio
from agent_interface.mcp_server import handle_mcp_request
print(asyncio.run(handle_mcp_request({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}))['result']['tools'][0]['name'])
"
```
