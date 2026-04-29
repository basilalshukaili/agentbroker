# Async Operations Contract

**Phase P3 Artifact | APIDesignAgent**

---

## Execution Profiles

| Profile | HTTP response | Terminal outcome | Operations |
|---|---|---|---|
| `sync` | 200 with terminal OutcomeReceipt | In same HTTP response | preview_cost, self_test, find_business, verify_business, get_status, get_outcome |
| `sync_fast` | 200 with terminal OutcomeReceipt OR 202 with pending_async if upstream slow | Within ~5s normally | send_message (text), send_transactional_confirmation, capture_lead |
| `async_by_default` | 202 with pending_async always | Via webhook + get_status polling | schedule_appointment, handle_inbound, escalate_to_human, send_message (voice) |

---

## Async Job Lifecycle

```
HTTP POST /schedule-appointment
  → 202 Accepted  { status: "pending_async", operation_id: "op_xyz", estimated_completion_time: "..." }

[Celery worker picks up job]
  → status: "executing" (observable via GET /operations/op_xyz/status)

[Terminal state reached]
  → status: "success" | "failure" | "partial"
  → OutcomeReceipt written to outcome_store
  → Signed webhook delivered to Webhook-URL (if registered)
  → GET /operations/op_xyz/outcome now returns full OutcomeReceipt
```

---

## Polling

- Use `GET /operations/{operation_id}/status` to poll.
- **Do not poll more than once per 10 seconds.**
- Prefer webhook delivery for real-time updates.
- Operations in `pending` or `executing` state for more than the `estimated_completion_time + 5 minutes` should be treated as timed out and can call `escalate_to_human`.

---

## Webhook Contract

Webhook deliveries are HTTP POST requests to the URL registered via `/webhooks/register`.

**Headers:**
```
Content-Type: application/json
X-SMBBroker-Signature: HMAC-SHA256(<body>, <shared_secret>)
X-SMBBroker-Idempotency-Key: <original_request_idempotency_key>
X-SMBBroker-Event: outcome
X-SMBBroker-Version: 0.1
```

**Body:** The final `OutcomeReceipt` JSON.

**Delivery:**
- Retried with exponential backoff (1s, 2s, 4s, 8s, ... up to 1h interval) for up to 24 hours.
- Receiver must respond `2xx` within 5 seconds to acknowledge.
- Non-2xx responses trigger retry.
- After 24 hours of failed delivery, the outcome is still queryable via `get_outcome`.

**Signature verification (receiver-side):**
```python
import hmac, hashlib
def verify_signature(body_bytes: bytes, header_sig: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)
```

---

## Estimated Completion Times (p95 by operation)

| Operation | Channel | p95 estimate |
|---|---|---|
| schedule_appointment | direct_api | 10s |
| schedule_appointment | voice_ai | 90s |
| handle_inbound | api | 5s |
| escalate_to_human | ticketing | 30s |
| send_message | voice | 45s |

These are seed estimates. They are replaced with real telemetry values at P8.
