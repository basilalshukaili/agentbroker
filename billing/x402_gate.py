"""
x402 payment gate — standard scheme via the Coinbase CDP facilitator.

This replaces the worker's non-standard, tx-hash x402 (which real x402 agents
could not pay) with the STANDARD scheme that Coinbase AgentKit and `@x402/mcp`
clients actually speak:

  1. Agent calls a paid MCP tool with no payment.
  2. We return an MCP "payment required" result (is_error + structuredContent =
     PaymentRequired) listing what to pay: USDC on Base, amount, payTo, plus a
     Bazaar discovery extension so facilitators can catalog the tool.
  3. Agent signs an EIP-3009 authorization and retries the tool call with
     `_meta["x402/payment"]` set to the signed payload.
  4. We VERIFY then SETTLE the payment through the CDP facilitator, run the
     tool, and return the result with the settlement in `_meta`.

The first SETTLED payment auto-lists us in the x402 Bazaar — the agent-native
distribution channel.

Design notes
------------
* The whole thing is OFF unless `enabled()` (X402_ENABLED + receiver + CDP keys).
  When off, the MCP dispatcher runs paid tools free (unchanged behavior), so a
  misconfiguration can never take the server down.
* One shared, lazily-initialized `x402ResourceServer`. `initialize()` does a
  blocking GET /supported against CDP, so we run it once in a thread executor.
* CDP auth is an EdDSA (Ed25519) JWT minted per request, injected via the SDK's
  `create_headers` hook. Validated working against CDP /supported and /verify.
* Pricing mirrors the (now-retired) edge table exactly — agent-friendly, cheap.
  call_business is intentionally NOT payable (the voice path is disabled).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import config
from agent_interface.manifest_server import get_operation

log = logging.getLogger("smb_broker.x402")

# Canonical pricing (USD strings — kept as strings to avoid float repr drift).
# Mirrors the edge PRICING_ATOMIC: $X = atomic / 1e6. Reads are free (absent).
_PRICING_USD: dict[str, str] = {
    "send_message": "0.02",
    "capture_lead": "0.05",
    "schedule_appointment": "0.15",
    "send_transactional_confirmation": "0.02",
    "handle_inbound": "0.03",
    "escalate_to_human": "0.20",
    # import_booking_url is intentionally FREE — it is the adoption wedge (turn
    # any booking URL into a callable business). Agents import for free, then pay
    # for the action (schedule_appointment / send_message). "Don't be greedy."
    # call_business ($0.50) is also omitted — the voice path is disabled, so
    # charging for it would take money then return voice_not_provisioned.
}

# Receipt statuses that mean "the paid action did NOT happen" → do NOT settle
# (never charge for a failed/rejected call). Anything not in this set is treated
# as chargeable (the action ran). Listing failures (not successes) avoids
# accidentally giving away a legitimate success whose status string we forgot.
_FAILURE_STATUSES = frozenset({
    "failure", "failed", "error", "errored", "rejected", "denied", "blocked",
    "compliance_blocked", "invalid", "not_found", "unsupported",
    "unsupported_platform", "unprocessable", "timeout", "cancelled", "canceled",
    # CRITICAL-2 fix: pending_async is a non-terminal status — the Celery worker
    # has been dispatched but the booking is NOT yet confirmed. Charging before
    # the booking completes is dishonest billing (agent pays; booking may fail
    # silently later). Adding pending_async here causes _receipt_is_error() to
    # return True, so the SDK skips CDP settlement on async dispatch.
    # Settlement deferred to a future Celery-completion webhook refactor.
    "pending_async",
    # MEDIUM-2 fix: partial means the operation was partially attempted but no
    # real external action completed (e.g. capture_lead with no CRM write).
    # Do not charge for stub/incomplete work.
    "partial",
})


def _receipt_is_error(receipt: Any) -> bool:
    """True if the tool clearly failed (so the wrapper must NOT settle/charge)."""
    if not isinstance(receipt, dict):
        return False
    status = str(receipt.get("status", "")).strip().lower()
    return status in _FAILURE_STATUSES

MAINNET = "eip155:8453"
TESTNET = "eip155:84532"

# Lazy singletons (built on first paid call).
_server: Any = None
_server_lock = asyncio.Lock()
_wrappers: dict[str, Callable] = {}
_accepts_cache: dict[str, list] = {}
_ed25519_key: Any = None


# ---------------------------------------------------------------------------
# Public predicates (cheap, pure, never raise)
# ---------------------------------------------------------------------------

# HIGH-1 FOUNDER ACTION REQUIRED — Dual x402 gate risk (cannot be fixed in code here).
#
# There are TWO x402 implementations:
#   1. Edge (Cloudflare Worker): edge/src/mcp-edge.ts + edge/src/x402.ts
#      Triggered by X402_RECEIVER_ADDRESS being set as a CF Worker secret.
#      Uses a hand-rolled "exact-evm" + X-PAYMENT-PROOF scheme (NOT standard CDP).
#      Does NOT call CDP /settle — only marks a KV nonce.
#   2. Origin (this file): standard CDP x402 SDK, triggered by X402_ENABLED=true.
#
# If BOTH are active simultaneously an agent agent gets a 402 from the edge
# (hand-rolled scheme), pays, and the edge proxies to origin — which then issues
# ANOTHER 402 (CDP scheme). The agent is double-charged or the flow breaks.
#
# REQUIRED FOUNDER ACTION before setting X402_ENABLED=true on Render:
#   - Log in to the Cloudflare dashboard.
#   - Go to Workers & Pages → agent-broker-edge → Settings → Variables.
#   - DELETE (or leave unset) the X402_RECEIVER_ADDRESS Worker secret.
#   - This disables the edge x402 gate entirely. All tools/call requests proxy
#     to origin where the single, CDP-backed gate here handles payment.
#   - Only after confirming the edge secret is unset: set X402_ENABLED=true on Render.
#
# Do NOT set X402_RECEIVER_ADDRESS on the Cloudflare Worker at the same time
# as X402_ENABLED=true on Render. One path only.


def enabled() -> bool:
    """True only when x402 is fully configured. Safe to call on every request."""
    return bool(
        config.X402_ENABLED
        and config.X402_RECEIVER_ADDRESS
        and config.CDP_API_KEY_ID
        and config.CDP_API_KEY_SECRET
    )


def is_paid_tool(name: str) -> bool:
    """True if the tool requires payment. Read tools are absent → free."""
    return name in _PRICING_USD


def price_usd(name: str) -> Optional[str]:
    return _PRICING_USD.get(name)


# ---------------------------------------------------------------------------
# CDP facilitator auth — EdDSA JWT per endpoint
# ---------------------------------------------------------------------------

def _private_key():
    global _ed25519_key
    if _ed25519_key is None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        seed = base64.b64decode(config.CDP_API_KEY_SECRET)[:32]
        _ed25519_key = Ed25519PrivateKey.from_private_bytes(seed)
    return _ed25519_key


def _mint_jwt(uri: str) -> str:
    """Mint a short-lived CDP bearer token whose `uris` claim binds it to one
    METHOD+host+path (e.g. 'POST api.cdp.coinbase.com/platform/v2/x402/verify')."""
    import jwt  # PyJWT
    now = int(time.time())
    claims = {
        "sub": config.CDP_API_KEY_ID,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
        "uris": [uri],
    }
    return jwt.encode(
        claims,
        _private_key(),
        algorithm="EdDSA",
        headers={"kid": config.CDP_API_KEY_ID, "nonce": secrets.token_hex(16)},
    )


def _cdp_create_headers() -> dict[str, dict[str, str]]:
    """SDK `create_headers` hook → per-endpoint auth headers.

    Returns the dict shape the SDK's CreateHeadersAuthProvider expects:
    keys 'verify' / 'settle' / 'supported', each a header dict. Only called
    when CDP keys are present (public facilitators need no auth)."""
    if not (config.CDP_API_KEY_ID and config.CDP_API_KEY_SECRET):
        return {}
    u = urlparse(config.X402_FACILITATOR_URL)
    host = u.netloc
    base = u.path.rstrip("/")
    return {
        "verify": {"Authorization": f"Bearer {_mint_jwt(f'POST {host}{base}/verify')}"},
        "settle": {"Authorization": f"Bearer {_mint_jwt(f'POST {host}{base}/settle')}"},
        "supported": {"Authorization": f"Bearer {_mint_jwt(f'GET {host}{base}/supported')}"},
    }


# ---------------------------------------------------------------------------
# Resource server (lazy singleton)
# ---------------------------------------------------------------------------

async def _ensure_server():
    global _server
    if _server is not None:
        return _server
    async with _server_lock:
        if _server is not None:
            return _server
        from x402 import x402ResourceServer
        from x402.http import HTTPFacilitatorClient
        from x402.mechanisms.evm.exact import ExactEvmServerScheme

        facilitator = HTTPFacilitatorClient(
            {"url": config.X402_FACILITATOR_URL, "create_headers": _cdp_create_headers}
        )
        srv = x402ResourceServer(facilitator)
        srv.register(MAINNET, ExactEvmServerScheme())
        if config.X402_ENABLE_TESTNET:
            srv.register(TESTNET, ExactEvmServerScheme())

        # initialize() does a blocking GET /supported — keep the event loop free.
        await asyncio.get_event_loop().run_in_executor(None, srv.initialize)
        _server = srv
        log.info("x402 resource server initialized (facilitator=%s testnet=%s)",
                 config.X402_FACILITATOR_URL, config.X402_ENABLE_TESTNET)
        return _server


def _build_accepts(srv, tool: str) -> list:
    """PaymentRequirements the agent may satisfy. Mainnet always; testnet too
    when explicitly enabled (validation only)."""
    from x402 import ResourceConfig
    price = f"${_PRICING_USD[tool]}"
    accepts = list(srv.build_payment_requirements(
        ResourceConfig(scheme="exact", network=MAINNET,
                       pay_to=config.X402_RECEIVER_ADDRESS, price=price)
    ))
    if config.X402_ENABLE_TESTNET:
        accepts += list(srv.build_payment_requirements(
            ResourceConfig(scheme="exact", network=TESTNET,
                           pay_to=config.X402_RECEIVER_ADDRESS, price=price)
        ))
    return accepts


def _get_accepts(srv, tool: str) -> list:
    """Cached per-tool PaymentRequirements (build once; reused by the MCP wrapper
    and the HTTP /ops 402 response)."""
    a = _accepts_cache.get(tool)
    if a is None:
        a = _build_accepts(srv, tool)
        _accepts_cache.setdefault(tool, a)
        a = _accepts_cache[tool]
    return a


def _bazaar_extension(tool: str) -> dict:
    """Bazaar MCP discovery declaration for a tool (so facilitators index it)."""
    from x402.extensions.bazaar import (
        declare_mcp_discovery_extension, DeclareMcpDiscoveryConfig,
    )
    op = get_operation(tool) or {}
    return declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
        tool_name=tool,
        description=op.get("description") or tool,
        input_schema=op.get("input_schema") or {"type": "object", "properties": {}},
        transport="streamable-http",
        example=_bazaar_example(tool),
    ))


def _bazaar_example(tool: str) -> Optional[dict]:
    """A small example argument set so the Bazaar entry ranks well in semantic
    search. Best-effort — pulled from the manifest's first example if present."""
    op = get_operation(tool) or {}
    for ex in (op.get("examples") or []):
        call = ex.get("agent_call") or {}
        if call.get("tool") == tool and isinstance(call.get("arguments"), dict):
            return call["arguments"]
    return None


async def _notify_first_payment(ctx: Any) -> None:
    """on_after_settlement hook — fire a Telegram push the moment a real payment
    SETTLES on-chain. This is the "a buyer came" signal the founder waits on.

    Fires on every settled payment (the first one is, by definition, the first
    alert received). Never raises — an alert failure must not undo a settlement.
    """
    try:
        from billing.telegram_revenue_alerts import send_telegram_alert
        tool = getattr(ctx, "tool_name", "?")
        settle = getattr(ctx, "settlement", None)
        reqs = getattr(ctx, "payment_requirements", None)
        tx = (getattr(settle, "transaction", "") or "") if settle is not None else ""
        network = ""
        if settle is not None:
            network = getattr(settle, "network", "") or ""
        if not network and reqs is not None:
            network = getattr(reqs, "network", "") or ""
        usd = price_usd(tool) or "?"
        explorer = f"https://basescan.org/tx/{tx}" if tx else ""
        lines = [
            "*Agent Broker* — an AI agent just PAID (x402 / USDC).",
            f"Tool: `{tool}`",
            f"Amount: *${usd}* USDC ({network or 'base'})",
        ]
        if tx:
            lines.append(f"Tx: `{tx}`")
        if explorer:
            lines.append(explorer)
        lines.append("Settled via Coinbase CDP -> funds in your Binance USDC-Base account.")
        await send_telegram_alert("\n".join(lines))
        try:
            from telemetry.metrics import record_payment_settled
            record_payment_settled()
        except Exception:  # noqa: BLE001
            pass
        log.info("x402 settlement alert sent tool=%s tx=%s", tool, tx)
    except Exception as e:  # noqa: BLE001 — never let an alert failure bubble
        log.warning("x402 settlement alert failed: %s", e)


# Cooldown so a buyer retrying a payment several times doesn't spam Telegram.
# In-memory: resets on restart, which is fine — re-alerting after a restart on a
# genuine buyer is acceptable (better to over-signal a real buyer than miss one).
_buyer_intent_last_alert: dict[str, float] = {}
_BUYER_INTENT_COOLDOWN_S = 900.0  # 15 min per tool


async def _notify_buyer_intent(tool: str) -> None:
    """Fire a Telegram push the moment a REAL buyer attempts to pay (an x402
    payment payload arrived), BEFORE settlement. Crawlers/scorers never build
    signed payments, so this is the earliest high-signal "a real buyer is here"
    event — even if their payment later fails, the founder hears about it and can
    react. Never raises."""
    try:
        import time
        now = time.time()
        last = _buyer_intent_last_alert.get(tool, 0.0)
        if now - last < _BUYER_INTENT_COOLDOWN_S:
            return
        _buyer_intent_last_alert[tool] = now
        from billing.telegram_revenue_alerts import send_telegram_alert
        usd = price_usd(tool) or "?"
        await send_telegram_alert("\n".join([
            "*Agent Broker* — a real buyer is here. 👀",
            f"An AI agent attached an x402 payment for `{tool}` (${usd} USDC).",
            "Verifying + settling now — if it clears you'll get the PAID alert next.",
            "(Crawlers don't build signed payments, so this is a genuine buyer attempt.)",
        ]))
        log.info("x402 buyer-intent alert sent tool=%s", tool)
    except Exception as e:  # noqa: BLE001
        log.warning("x402 buyer-intent alert failed tool=%s err=%s", tool, e)


def _build_wrapper(srv, tool: str):
    # NOTE: import the *framework-agnostic* wrapper from server_async — it takes
    # a handler with signature (args, extra) where extra carries `_meta`. The
    # `from x402.mcp import create_payment_wrapper` shortcut instead resolves to
    # the FastMCP variant (keyword `accepts=`, needs the `mcp` package's
    # Context), which does not fit our hand-rolled JSON-RPC dispatcher.
    from x402 import ResourceInfo
    from x402.mcp.server_async import create_payment_wrapper, PaymentWrapperConfig
    from x402.mcp.types import PaymentWrapperHooks
    op = get_operation(tool) or {}
    description = op.get("description") or tool
    cfg = PaymentWrapperConfig(
        accepts=_get_accepts(srv, tool),
        # The Bazaar entry's `resource.url` is how a discovering agent learns
        # where to connect — it MUST be our real public MCP endpoint, not a
        # logical mcp://tool/... id. toolName (in the extension) disambiguates.
        resource=ResourceInfo(
            url=config.X402_PUBLIC_MCP_URL,
            description=description,
            mime_type="application/json",
        ),
        extensions=_bazaar_extension(tool),
        # Push a Telegram alert the instant a payment settles — the founder's
        # "a buyer came" signal.
        hooks=PaymentWrapperHooks(on_after_settlement=_notify_first_payment),
    )
    return create_payment_wrapper(srv, cfg)


# ---------------------------------------------------------------------------
# Entry point used by the MCP dispatcher
# ---------------------------------------------------------------------------

def _mcp_result_to_jsonrpc(result: Any) -> dict:
    """Translate the SDK's MCPToolResult into a JSON-RPC tools/call result dict."""
    out: dict[str, Any] = {
        "content": result.content or [],
        "isError": bool(result.is_error),
    }
    if result.structured_content is not None:
        out["structuredContent"] = result.structured_content
    if result.meta:
        out["_meta"] = result.meta
    return out


async def run_paid_tool(
    tool: str,
    arguments: dict,
    meta: dict,
    dispatch: Callable[[], Awaitable[dict]],
) -> dict:
    """Gate a paid MCP tool call through x402.

    `dispatch` is a no-arg coroutine that actually runs the tool and returns the
    receipt dict. It is only awaited AFTER the agent's payment verifies, and the
    settlement only fires if the tool succeeds.

    Returns a JSON-RPC tools/call result dict (either a payment-required result
    when no/!valid payment, or the tool result with settlement in `_meta`).
    """
    # Funnel telemetry: a paid tool was invoked. If the agent also attached an
    # x402 payment, that's a REAL buyer (crawlers/scorers don't build signed
    # payments) — record it and fire a buyer-intent alert immediately, before
    # we even know if settlement succeeds, so the founder hears about the first
    # real buyer the moment they touch a paid surface.
    try:
        from telemetry.metrics import record_paid_tool_attempt, record_payment_attempt
        record_paid_tool_attempt()
        if isinstance(meta, dict) and meta.get("x402/payment"):
            record_payment_attempt()
            await _notify_buyer_intent(tool)
    except Exception as e:  # noqa: BLE001 — telemetry/alert must never block a sale
        log.warning("x402 funnel telemetry failed tool=%s err=%s", tool, e)

    srv = await _ensure_server()
    wrapper = _wrappers.get(tool)
    if wrapper is None:
        built = _build_wrapper(srv, tool)
        _wrappers.setdefault(tool, built)  # first writer wins under a race
        wrapper = _wrappers[tool]

    async def handler(args: dict, _ctx: Any) -> dict:
        receipt = await dispatch()
        return {
            "content": [{"type": "text", "text": json.dumps(receipt, default=str)}],
            "structuredContent": receipt,
            # Only settle (charge) when the tool actually did the work. A failed/
            # rejected call returns is_error=True, which the SDK wrapper uses to
            # SKIP settlement — the agent is never charged for a failure.
            "isError": _receipt_is_error(receipt),
        }

    wrapped = wrapper(handler)
    result = await wrapped(arguments, {"_meta": meta or {}, "toolName": tool})
    return _mcp_result_to_jsonrpc(result)


async def http_payment_required(tool: str) -> dict:
    """Build a standard x402 PaymentRequired body for the REST surface (/ops/*).

    The paid tools' REST routes must not be a free bypass of the /mcp x402 gate.
    They return this 402 body; agents complete payment through the MCP endpoint
    (resource.url points there). Never raises — falls back to a minimal body.
    """
    try:
        srv = await _ensure_server()
        from x402 import ResourceInfo
        op = get_operation(tool) or {}
        pr = await srv.create_payment_required_response(
            _get_accepts(srv, tool),
            ResourceInfo(
                url=config.X402_PUBLIC_MCP_URL,
                description=op.get("description") or tool,
                mime_type="application/json",
            ),
            ("Payment required. This tool is paid via x402. Complete payment at the "
             "MCP endpoint (POST /mcp, JSON-RPC tools/call, with the signed payment "
             "in params._meta['x402/payment'])."),
            _bazaar_extension(tool),
        )
        return pr.model_dump(by_alias=True)
    except Exception as e:  # noqa: BLE001
        log.warning("http_payment_required build failed tool=%s err=%s", tool, e)
        return {
            "x402Version": 2,
            "error": "payment_required",
            "detail": "This tool is paid via x402. Pay through POST /mcp (tools/call) "
                      "with the payment in params._meta['x402/payment'].",
        }
