# Agent Integration Guide

This guide is for the agent developer. **Goal: get from `pip install` to a successful operation in under 5 minutes.**

Every example below is copy-paste runnable.

---

## Step 0: Get an Agent-Identity token

Every state-changing operation requires `X-Agent-Identity`. Read-only ops (`find_business`, `verify_business`, `preview_cost`, `self_test`, `get_status`, `get_outcome`) do not require auth in development.

```python
import httpx

resp = httpx.post(
    "https://api.hatchloop.dev/auth/token",
    json={
        "agent_id": "my_agent_v1",
        "principal_id": "user_123",
        "allowed_operations": ["*"],
        "budget_cap_usd": 10.00,
        "ttl_seconds": 86400,
    },
)
TOKEN = resp.json()["token"]
```

Store the token; it's good for 24 hours by default. Re-issue when expired.

---

## Step 1: Always call `preview_cost` first

`preview_cost` is **free** and ±5% accurate. Use it before every state-changing operation.

```python
preview = httpx.post(
    "https://api.hatchloop.dev/ops/preview_cost",
    json={"operation": "schedule_appointment", "params": {"smb_id": "smb_001"}},
).json()
print(preview)
# {
#   "estimated_cost_usd": 0.625,
#   "cost_accuracy_slo": "±5%",
#   "estimated_latency_p50_ms": 2500,
#   "success_probability_estimate": 0.87,
#   "execution_profile": "async_by_default",
#   "compliance_constraints": [...]
# }
```

If `success_probability_estimate < 0.5` or `estimated_cost_usd > your_budget`, abort.

---

## Step 2: Choose your protocol

### Protocol A: MCP (recommended for Claude Desktop, Cursor, Continue)

```json
// ~/.claude/claude_desktop_config.json
{
  "mcpServers": {
    "smb-broker": {
      "url": "https://hatchloop.dev/mcp/agent-broker",
      "headers": {
        "X-Agent-Identity": "YOUR_TOKEN_HERE"
      }
    }
  }
}
```

After restart, all 20 operations appear as tools in Claude. No code needed.

### Protocol B: OpenAI function calling

```python
import httpx, openai

# Fetch tools from .well-known
tools = httpx.get(
    "https://hatchloop.dev/.well-known/openai-tools.json"
).json()["tools"]

client = openai.OpenAI()
resp = client.chat.completions.create(
    model="gpt-4-turbo",
    messages=[
        {"role": "system", "content": "Use the smb-broker tools to fulfill SMB-related tasks."},
        {"role": "user", "content": "Find me a haircut place near 30309 that does walk-ins."}
    ],
    tools=tools,
)
tool_call = resp.choices[0].message.tool_calls[0]

# Execute the tool call against the broker
result = httpx.post(
    f"https://api.hatchloop.dev/ops/{tool_call.function.name}",
    headers={"X-Agent-Identity": TOKEN},
    json=tool_call.function.arguments,
).json()
```

### Protocol C: Anthropic tool use

```python
import httpx, anthropic

tools = httpx.get(
    "https://hatchloop.dev/.well-known/anthropic-tools.json"
).json()["tools"]

client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=2048,
    tools=tools,
    messages=[{"role": "user", "content": "Book me a Saturday haircut in Atlanta under $50."}],
)

# Walk msg.content for tool_use blocks; execute each:
for block in msg.content:
    if block.type == "tool_use":
        result = httpx.post(
            f"https://api.hatchloop.dev/ops/{block.name}",
            headers={"X-Agent-Identity": TOKEN},
            json=block.input,
        ).json()
        # feed result back as tool_result for next turn
```

### Protocol D: Plain REST

```bash
curl -X POST https://api.hatchloop.dev/ops/find_business \
  -H "Content-Type: application/json" \
  -H "X-Agent-Identity: $TOKEN" \
  -d '{
    "vertical": "personal_services",
    "location": {"zip_or_city": "30309"},
    "capability": "haircut",
    "max_results": 5
  }'
```

---

## Step 3: Handle the OutcomeReceipt

Every operation returns the same shape:

```json
{
  "operation_id": "op_a1b2c3d4",
  "status": "success",
  "reason_code": "ok",
  "human_message": "Found 3 hair salons in 30309 matching 'haircut'.",
  "result": { "businesses": [...] },
  "cost": {"amount": 0.01, "currency": "USD"},
  "latency_ms": 287,
  "channel_used": "directory",
  "channel_fallback_chain": ["directory"],
  "estimated_completion_time": null,
  "next_actions": ["call schedule_appointment with the smb_id of your choice"],
  "retriable": false,
  "trace_id": "tr_xyz789"
}
```

Status values: `success`, `failure`, `pending_async`, `partial`. Always check `status` before trusting `result`.

---

## Step 4: Async operations (`schedule_appointment`, `escalate_to_human`, `handle_inbound`)

These return `status: "pending_async"` immediately with an `operation_id`. You then either:

**Option A: Poll** (rate-limited to 1 call per 10 seconds per operation_id):

```python
import time
while True:
    status = httpx.get(
        f"https://api.hatchloop.dev/ops/get_status/{op_id}",
    ).json()
    if status["status"] in ("success", "failure"):
        break
    time.sleep(10)
outcome = httpx.get(
    f"https://api.hatchloop.dev/ops/get_outcome/{op_id}",
).json()
```

**Option B: Webhook** (preferred for production):

```python
# Register once
reg = httpx.post(
    "https://hatchloop.dev/mcp/agent-broker/webhooks/register",
    headers={"X-Agent-Identity": TOKEN},
    json={
        "callback_url": "https://my-agent.example.com/webhooks/smb-broker",
        "events": ["operation.completed", "operation.failed"],
    },
).json()
webhook_secret = reg["secret"]   # store securely

# Verify incoming webhooks
import hmac, hashlib

def verify(body: bytes, signature_header: str) -> bool:
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

---

## Step 5: Compliance — what you don't have to handle

You **never** need to:

- Maintain consent records — we do.
- Check the FCC Do-Not-Call registry — we do (for voice).
- Append CAN-SPAM unsubscribe footers — we do.
- Add the two-party recording-consent prompt for CA/FL/IL/MD/MA/MT/NV/NH/PA/WA — we do.
- Verify 10DLC campaign registration before US A2P SMS — we do.
- Honor `STOP` keywords — we do.

If a compliance check fails, you'll receive `error_code: "compliance_violation"` (HTTP 422). **Do not retry.** The error is non-retriable by definition; obtain consent or change channel before trying again.

---

## Step 6: Idempotency

Every state-changing operation accepts an optional `idempotency_key`. If you set one and retry the same call within 24 hours, you'll get back the cached `OutcomeReceipt` rather than a duplicate side-effect.

```python
import uuid
key = f"book_{uuid.uuid4().hex}"
for attempt in range(3):
    try:
        return httpx.post(
            "https://api.hatchloop.dev/ops/schedule_appointment",
            headers={"X-Agent-Identity": TOKEN, "X-Idempotency-Key": key},
            json=req,
            timeout=10,
        ).json()
    except httpx.TimeoutException:
        continue   # safe to retry — same key won't double-book
```

---

## Step 7: Budget & cost control

**Ask before you spend.** `preview_cost` is free and unmetered — call it on any
write action to get the exact credit cost before committing:

```python
quote = httpx.post(
    "https://api.hatchloop.dev/ops/preview_cost",
    json={"operation": "schedule_appointment", "smb_id": "smb_123"},
).json()
```

**Your remaining quota comes back in the receipt.** Free-tier callers get a
`quota` block injected into every receipt, so there is no second call to make:

```jsonc
{
  "operation_id": "...",
  "status": "success",
  "quota": {
    "tier": "free",
    "remaining_today": 87,
    "daily_limit": 100,
    "resets": "2026-08-27T00:00:00Z"
  }
}
```

**When the quota runs out** the operation fails honestly — `reason_code:
free_quota_exceeded`, `cost: $0`, and a `how_to_resolve` field naming the
escape paths (credits). Nothing is charged and nothing is
half-executed.

> **No agent-callable balance endpoint yet.** Credit balance and transaction
> history live behind the human portal at <https://hatchloop.dev/portal>, which
> uses a browser session rather than an `X-Agent-Identity` header. If your agent
> needs a programmatic balance read, email <hello@hatchloop.dev> — we would
> rather hear the requirement than have you scrape the portal.

---

## Common workflows

### "Book me a haircut Saturday in Atlanta under $50"

```python
# 1. Discover
businesses = httpx.post("https://.../ops/find_business", json={
    "vertical": "personal_services",
    "location": {"zip_or_city": "30309"},
    "capability": "haircut",
    "max_results": 5,
}).json()["result"]["businesses"]

# 2. Filter to those in budget
candidates = [b for b in businesses if b["price_range"]["max_usd"] <= 50]

# 3. Verify
verified = [b for b in candidates if httpx.post(
    "https://.../ops/verify_business",
    json={"smb_id": b["smb_id"], "capability_to_verify": "haircut"},
).json()["result"]["verified"]]

# 4. Preview cost
preview = httpx.post("https://.../ops/preview_cost", json={
    "operation": "schedule_appointment", "params": {"smb_id": verified[0]["smb_id"]},
}).json()

# 5. Book
receipt = httpx.post("https://.../ops/schedule_appointment", json={
    "smb_id": verified[0]["smb_id"],
    "action": "book",
    "service": "haircut",
    "preferred_window": {"start": "2026-05-02T09:00", "end": "2026-05-02T18:00"},
}, headers={"X-Agent-Identity": TOKEN}).json()
```

### "Capture this lead and notify the salon"

```python
receipt = httpx.post("https://.../ops/capture_lead", json={
    "smb_id": "smb_001",
    "prospect": {
        "name": "Jane Smith",
        "phone": "+14045551234",
        "email": "jane@example.com",
        "service_interest": "color_correction",
    },
    "source": "agent_referral",
}, headers={"X-Agent-Identity": TOKEN}).json()
```

### "Send a transactional booking confirmation"

```python
# Transactional = TCPA-exempt, no consent check, only content+jurisdiction checks
receipt = httpx.post("https://.../ops/send_transactional_confirmation", json={
    "recipient_id": "+14045551234",
    "channel": "sms",
    "template": "booking_confirmation",
    "template_data": {
        "smb_name": "Cuts & Co.",
        "appointment_time": "Saturday May 3 at 10:00 AM",
        "address": "123 Main St, Atlanta GA 30309",
    },
}, headers={"X-Agent-Identity": TOKEN}).json()
```

---

## Troubleshooting

| Error | Meaning | What to do |
|-------|---------|-----------|
| `401 Unauthorized` | Token missing or expired | Re-issue from `/auth/token` |
| `403 Forbidden` | Operation not in your scope | Update `allowed_operations` on token |
| `422 compliance_violation` | Pre-check failed | Do **not** retry. Obtain consent. |
| `429 rate_limited` | Too many requests in window | Back off; retry-after header included |
| `503 supply_unreachable` | SMB cannot be contacted | Try a different smb_id from find_business |
| `504 timeout` | Upstream channel timed out | Idempotent retry with same key is safe |

Full catalog: [api/errors.md](../api/errors.md).

---

## Support & feedback

- Service status: `https://api.hatchloop.dev/health`
- Self-test: `POST /ops/self_test`
- Email: support@agent-broker-edge.basil-agent.workers.dev
- File a feedback ticket: agents who flag a missing capability that we add inside 30 days get 1 month free at their tier.
