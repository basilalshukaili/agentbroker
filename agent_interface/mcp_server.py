"""
MCP (Model Context Protocol) server endpoint.

MCP is the protocol Claude Desktop, Cursor, Continue, and other agent IDEs
use to discover and call tools. Agents that speak MCP will find us via
the standard `tools/list` and `tools/call` JSON-RPC methods.

Spec: https://spec.modelcontextprotocol.io/

This module exposes a JSON-RPC 2.0 endpoint at /mcp that handles:
  - initialize           → handshake + server capabilities
  - tools/list           → returns all operations as MCP tools
  - tools/call           → invokes an operation
  - resources/list       → exposes the manifest as a resource
  - resources/read       → returns manifest content
  - prompts/list         → suggested prompts for common workflows

Streaming (SSE) is supported for async operations: pending_async results
emit a notification when the underlying Celery job completes.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import config as _config
from agent_interface.manifest_server import get_full_manifest, get_operation


# ---------------------------------------------------------------------------
# MCP server metadata
# ---------------------------------------------------------------------------

SERVER_NAME = "agent-broker"
# Derived, never restated. A hardcoded version here reported 0.1.0 on the
# origin long after the build was 0.2.x - and the edge snapshot, refreshed
# FROM the origin, would have inherited the regression (2026-08-26).
SERVER_VERSION = _config.SERVICE_VERSION
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Tool registry — all operations exposed as MCP tools
# ---------------------------------------------------------------------------

def _build_tool_list() -> list[dict]:
    """Convert manifest operations to MCP tool descriptors."""
    manifest = get_full_manifest()
    tools = []
    for op in manifest.get("operations", []):
        input_schema = op.get("input_schema", {"type": "object"})
        if op["name"] in _WRITE_TOOLS_REQUIRING_AUTH:
            # Advertise the retry contract on every write tool: optional
            # idempotency_key -> replaying the same key within 24h returns the
            # original receipt with no re-execution and no second charge.
            import copy as _copy
            input_schema = _copy.deepcopy(input_schema)
            props = input_schema.setdefault("properties", {})
            props.setdefault("idempotency_key", {
                "type": "string",
                "maxLength": 128,
                "description": (
                    "Optional client-supplied key for safe retries. Replaying "
                    "the same key within 24h returns the original receipt - "
                    "the operation is NOT re-executed and NOT re-charged."
                ),
            })
        tool = {
            "name": op["name"],
            "description": _format_description_for_llm(op),
            "inputSchema": input_schema,
        }
        # MCP annotations help client UIs render tools better. idempotentHint
        # must be tool-specific — retrying a non-idempotent write tool would
        # double-bill via x402 AND duplicate the side effect (e.g. two SMS
        # sends). Set True only for tools where a repeat call has no
        # observable additional effect.
        tool["annotations"] = {
            "title": op["name"].replace("_", " ").title(),
            "readOnlyHint": op["name"] in {
                "find_business", "verify_business", "get_status",
                "get_outcome", "preview_cost", "self_test",
                "check_booking_link", "check_compliance", "get_conversation",
                "verify_company_record", "screen_sanctions",
                "map_trade_restriction",
            },
            "destructiveHint": op["name"] in {
                "send_message", "schedule_appointment",
                "send_transactional_confirmation", "escalate_to_human",
                "call_business",
            },
            "idempotentHint": op["name"] in {
                # Safe to retry — same input yields same observable result:
                "find_business", "verify_business",
                "get_status", "get_outcome",
                "preview_cost", "self_test",
                "import_booking_url",  # idempotent by design (returns same smb_id)
                "check_booking_link",  # pure classification, no side effects
                "check_compliance",    # pure gate preview, no send, no audit write
                "verify_company_record",  # read-only live registry lookup
                "screen_sanctions",    # read-only live sanctions lookup
                "map_trade_restriction",  # read-only compliance snapshot
            },
            "openWorldHint": op["name"] in {
                "send_message", "schedule_appointment", "call_business",
            },
        }
        tools.append(tool)
    return tools


def _format_description_for_llm(op: dict) -> str:
    """Build an LLM-optimized description that combines all selection signals.

    The order matters: the LLM is most likely to act on the first 200 tokens.
    Lead with WHEN-TO-USE patterns and example user queries because that's
    what the LLM matches against when picking a tool from `tools/list`.
    """
    parts = [op["description"], ""]

    # User-query examples block first — LLMs are massively few-shot driven and
    # match against natural-language user phrases when picking a tool.
    user_examples = op.get("user_query_examples") or [
        ex for ex in (op.get("examples") or []) if ex.get("user_says")
    ]
    if user_examples:
        parts.append("EXAMPLE USER QUERIES THAT MATCH THIS TOOL:")
        for ex in user_examples[:4]:
            user_says = ex.get("user_says")
            if not user_says:
                continue
            parts.append(f"  user: \"{user_says}\"")
            call = ex.get("agent_call") or {}
            if call.get("tool"):
                parts.append(f"  -> call {call['tool']}({json.dumps(call.get('arguments', {}))})")
            if ex.get("then_call"):
                nxt = ex["then_call"]
                parts.append(f"  -> then {nxt.get('tool')}({json.dumps(nxt.get('arguments', {}))})")
        parts.append("")

    parts.append(f"WHEN TO USE: {op['when_to_use']}")
    if op.get("when_not_to_use"):
        parts.append(f"WHEN NOT TO USE: {op['when_not_to_use']}")

    cost = op.get("cost_model", {})
    if cost:
        cost_basis = cost.get("basis", "per_call")
        # Manifest uses `unit_price_usd` on most ops, `amount_usd` on one — accept either.
        cost_amount = cost.get("unit_price_usd", cost.get("amount_usd"))
        # Channel- or outcome-variable pricing (send_message, schedule_appointment): point at preview_cost.
        has_variable = (
            cost_basis == "per_call_variable"
            or any(k in cost for k in ("voice_premium_usd", "success_bonus_usd",
                                       "tiers", "max_price_usd"))
        )
        if cost_basis == "freemium_daily_quota":
            # Neither "free" nor a flat price is true here: free within the
            # daily quota, billed after it. Say the actual rule.
            parts.append(
                f"COST: free within the daily quota, then ${cost_amount} per call")
        elif cost_basis == "free":
            parts.append("COST: free")
        elif cost_amount is not None and not has_variable:
            parts.append(f"COST: ${cost_amount} {cost_basis}")
        elif cost_amount is not None and has_variable:
            parts.append(f"COST: from ${cost_amount} {cost_basis} (see preview_cost for exact)")
        else:
            parts.append("COST: see preview_cost")

    slo = op.get("slo", {})
    if slo:
        # Manifest uses `p50_ms` on most ops, `p50_latency_ms` on one — accept either.
        latency = (
            slo.get("p50_ms")
            or slo.get("p50_latency_ms")
            or slo.get("p95_ms")
            or slo.get("max_latency_ms")
        )
        if latency is not None:
            parts.append(f"LATENCY: ~{latency}ms")

    profile = op.get("execution_profile", "sync")
    if profile != "sync":
        parts.append(f"EXECUTION: {profile} (use get_outcome to retrieve result)")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------

@dataclass
class JsonRpcResponse:
    id: Any
    result: Optional[dict] = None
    error: Optional[dict] = None

    def to_dict(self) -> dict:
        d: dict = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            d["error"] = self.error
        else:
            d["result"] = self.result
        return d


def _error(code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


# Standard JSON-RPC error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


async def handle_mcp_request(payload: dict, headers: Optional[dict] = None) -> dict:
    """
    Main JSON-RPC dispatcher. Returns a dict suitable to be JSON-encoded
    and sent back to the client.

    `headers` (case-insensitive dict) carries the HTTP request headers so the
    auth guard inside `tools/call` can pull `x-agent-identity` and gate
    write-tool dispatch when REQUIRE_AUTH=true. Optional for backwards-compat
    with any caller still invoking us with the single-arg signature.
    """
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {}) or {}
    # Normalize header keys to lower-case so callers don't have to.
    norm_headers: dict = {}
    if headers:
        for k, v in dict(headers).items():
            norm_headers[k.lower()] = v

    if not method:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INVALID_REQUEST, "Missing 'method' field"),
        ).to_dict()

    handler = _METHOD_HANDLERS.get(method)
    if not handler:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_METHOD_NOT_FOUND, f"Method '{method}' not found"),
        ).to_dict()

    try:
        # Only `tools/call` needs to see headers (for the per-tool auth gate);
        # every other method's signature stays `(params) -> dict`.
        if method == "tools/call":
            result = await handler(params, norm_headers)
        else:
            result = await handler(params)

        # Fire-and-forget usage telemetry — never blocks, never raises.
        # FIX 1 (cred hygiene): pass the PARSED agent_id to fire_log_usage,
        # never a slice of the raw bearer token.
        try:
            tool_name = params.get("name") if method == "tools/call" else None
            arguments = params.get("arguments") if method == "tools/call" else None
            ip = norm_headers.get("x-forwarded-for", norm_headers.get("x-real-ip", ""))
            ua = norm_headers.get("user-agent", "")
            raw_key = norm_headers.get("x-agent-identity", "")
            key_id = _agent_id_from_token(raw_key)
            from billing.usage_logger import fire_log_usage
            fire_log_usage(method, tool_name, arguments, ip, ua, key_id)
        except Exception:  # noqa: BLE001
            pass  # telemetry must never break the response

        return JsonRpcResponse(id=rpc_id, result=result).to_dict()
    except _ToolError as te:
        # Tool-execution failure -> isError RESULT (the model sees it and can
        # branch on error_code: authenticate / pay / back off / fix args).
        # For non-tools/call methods fall back to a typed protocol error.
        if method == "tools/call":
            return JsonRpcResponse(id=rpc_id, result=te.to_result()).to_dict()
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INVALID_PARAMS, str(te),
                         data={"error_code": te.error_code,
                               "retriable": te.retriable,
                               "how_to_resolve": te.how_to_resolve}),
        ).to_dict()
    except _ParamError as pe:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INVALID_PARAMS, str(pe),
                         data={"error_code": "invalid_argument",
                               "retriable": False,
                               "how_to_resolve": {
                                   "call": "tools/list",
                                   "hint": "check the tool name and inputSchema; argument names are exact",
                               }}),
        ).to_dict()
    except KeyError as ke:
        # A missing required argument is the caller's problem to fix, not an
        # internal fault. Before 2026-08-04 this surfaced as
        # "Internal error: 'vertical'", which tells an agent nothing and ends
        # the session — the most expensive kind of error on a discovery
        # marketplace. Name the argument and point at the schema instead.
        missing = str(ke).strip("'\"")
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(
                ERR_INVALID_PARAMS,
                f"missing required argument '{missing}'. Call tools/list and use "
                f"the inputSchema for this tool - argument names are exact.",
            ),
        ).to_dict()
    except Exception as exc:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INTERNAL, f"Internal error: {exc}"),
        ).to_dict()


class _ParamError(ValueError):
    pass


class _ToolError(Exception):
    """A TOOL-EXECUTION failure an agent can act on programmatically.

    Unlike _ParamError (a protocol-level -32602), a _ToolError is returned as a
    normal tools/call RESULT with isError:true plus typed fields - so the MODEL
    sees it (many MCP clients hide raw JSON-RPC errors from the model entirely,
    which meant an agent never saw the 'get a free key' recovery path).
    error_code vocabulary: auth_required | payment_required | rate_limited |
    invalid_argument | compliance_violation | upstream_unavailable.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retriable: bool,
        how_to_resolve: Optional[dict] = None,
        retry_after_ms: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retriable = retriable
        self.how_to_resolve = how_to_resolve or {}
        self.retry_after_ms = retry_after_ms

    def to_result(self) -> dict:
        body = {
            "status": "failure",
            "reason_code": self.error_code,
            "error_code": self.error_code,
            "retriable": self.retriable,
            "human_message": str(self),
            "how_to_resolve": self.how_to_resolve,
        }
        if self.retry_after_ms is not None:
            body["retry_after_ms"] = self.retry_after_ms
        return {
            "content": [{"type": "text", "text": json.dumps(body, indent=2, default=str)}],
            "isError": True,
        }


def _agent_id_from_token(raw_token: str) -> str:
    """Extract the PARSED agent_id from an X-Agent-Identity bearer token.

    Returns 'anonymous' for empty, missing, or invalid tokens.
    This is the sole safe way to derive a loggable identity — it never stores
    any slice of the raw bearer token value.
    """
    if not raw_token or raw_token in ("", "anonymous"):
        return "anonymous"
    try:
        from agent_interface.identity import validate_token
        result = validate_token(raw_token)
        if result.valid and result.identity:
            return result.identity.agent_id
    except Exception:  # noqa: BLE001
        pass
    return "anonymous"


# ---------------------------------------------------------------------------
# Method: initialize
# ---------------------------------------------------------------------------

async def _h_initialize(params: dict) -> dict:
    op_count = len(get_full_manifest().get("operations", []))
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
            "prompts": {"listChanged": False},
            "logging": {},
        },
        "instructions": (
            f"SMB Transaction & Communication Broker. Use tools/list to see all {op_count} operations. "
            "Most operations require an X-Agent-Identity token in the underlying HTTP request. "
            "For state-changing operations (send_message, schedule_appointment), call preview_cost "
            "first to confirm the budget impact."
        ),
    }


# ---------------------------------------------------------------------------
# Method: tools/list
# ---------------------------------------------------------------------------

async def _h_tools_list(params: dict) -> dict:
    return {"tools": _build_tool_list()}


# ---------------------------------------------------------------------------
# Method: tools/call
# ---------------------------------------------------------------------------

async def _h_tools_call(params: dict, headers: Optional[dict] = None) -> dict:
    """Idempotency wrapper around the real tools/call handler.

    Delivers the advertised retry contract: a write tool called with an
    `idempotency_key` (argument, popped before handler validation, or an
    X-Idempotency-Key header) is deduped per (agent scope, tool, key).
    Replay -> the ORIGINAL response verbatim (no re-execution, no second
    side effect, NO second charge). Same key + different args ->
    idempotency_conflict. Only successful responses are stored, so retries
    after transient failures run again. Wrapping here (above the impl)
    covers every billing branch: bypass, x402, credits, quota, free.
    """
    name = params.get("name")
    arguments = params.get("arguments")
    idem_key: Optional[str] = None
    if isinstance(arguments, dict) and "idempotency_key" in arguments:
        _v = arguments.pop("idempotency_key")  # pop -> handlers never see it
        idem_key = str(_v)[:128] if _v else None
    if not idem_key:
        _hv = (headers or {}).get("x-idempotency-key", "")
        idem_key = str(_hv)[:128] if _hv else None

    _scope: Optional[str] = None
    if idem_key and name in _WRITE_TOOLS_REQUIRING_AUTH:
        _raw_tok = (headers or {}).get("x-agent-identity", "")
        if _raw_tok:
            _aid = _agent_id_from_token(_raw_tok)
            if _aid != "anonymous":
                _scope = _aid
            else:
                import hashlib as _hl
                _scope = "tok_" + _hl.sha256(_raw_tok.encode()).hexdigest()[:16]

    if _scope and idem_key:
        from agent_interface import idempotency_gate as _ig
        _ah = _ig.args_hash(arguments if isinstance(arguments, dict) else {})
        _hit = await _ig.get(_scope, name, idem_key)
        if _hit is not None:
            if _hit.get("args_hash") != _ah:
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "status": "failure",
                        "reason_code": "idempotency_conflict",
                        "human_message": (
                            "This idempotency_key was already used with different "
                            "parameters. Use a new key for a different request."
                        ),
                        "retriable": False,
                    }, indent=2)}],
                    "isError": True,
                }
            return _hit["response"]
        resp = await _h_tools_call_impl(params, headers)
        if isinstance(resp, dict) and not resp.get("isError"):
            await _ig.put(_scope, name, idem_key, _ah, resp)
        return resp

    return await _h_tools_call_impl(params, headers)


async def _h_tools_call_impl(params: dict, headers: Optional[dict] = None) -> dict:
    name = params.get("name")
    arguments = params.get("arguments", {}) or {}
    if not name:
        raise _ParamError("Missing 'name' parameter")

    op = get_operation(name)
    if not op:
        raise _ParamError(f"Unknown tool: '{name}'")

    # -----------------------------------------------------------------------
    # DATA TOOL BYPASS (DATA_METERING_ENABLED=false, which is the default)
    # -----------------------------------------------------------------------
    # When DATA_METERING_ENABLED is off, the 3 premium data tools run free
    # and unmetered -- exactly as before this feature. We short-circuit here
    # so that even when X402_ENABLED or CREDITS_ENABLED is on, those billing
    # gates never fire for data tools while metering is off.
    # When DATA_METERING_ENABLED=true, we skip this block and fall through to
    # the x402/credits gates and the data-quota gate below.
    import os as _os_dm
    _data_metering_on = _os_dm.getenv("DATA_METERING_ENABLED", "").lower() in (
        "1", "true", "yes"
    )
    if name in _PREMIUM_DATA_TOOLS and not _data_metering_on:
        _bypass_receipt = await _dispatch_operation(name, arguments, headers or {})
        return {
            "content": [
                {"type": "text",
                 "text": json.dumps(_bypass_receipt, indent=2, default=str)}
            ],
            "isError": _bypass_receipt.get("status") == "failure",
        }
    # -----------------------------------------------------------------------

    # x402 payment gate. When enabled, paid write tools must carry a settled
    # USDC-on-Base payment (standard x402, settled via the Coinbase CDP
    # facilitator). The gate runs the tool only after the agent's payment
    # verifies, and settles only if the tool succeeds. Read tools — and the
    # case where x402 is disabled/misconfigured — fall through to the free
    # dispatch below, so the server never breaks on an x402 problem.
    from billing import x402_gate
    if x402_gate.enabled() and x402_gate.is_paid_tool(name):
        async def _dispatch() -> dict:
            # Payment is the authorization here — bypass the identity gate.
            return await _dispatch_operation(name, arguments, headers or {}, skip_auth=True)
        return await x402_gate.run_paid_tool(
            name, arguments, params.get("_meta") or {}, _dispatch
        )

    # --- SLICE 3: Credits payment gate ---
    # Runs AFTER the x402 branch (ONE rail: x402-paying calls never reach here).
    # Activates ONLY when CREDITS_ENABLED=true. When false: zero behavior change.
    # Path: paid tool + funded non-free credit account -> run_metered_tool
    #   -> reserve(MAX) first; if insufficient return honest failure (no dispatch)
    #   -> dispatch -> commit on success / release on failure
    # Free keys (free_*) keep the existing free-tier daily path unchanged
    # (limit lives in FREE_TIER_DAILY_LIMIT — never restate it here).
    # Reads and zero-cost ops bypass entirely (is_credit_paid_tool guard).
    import os as _os_credits
    if _os_credits.getenv("CREDITS_ENABLED", "").lower() in ("1", "true", "yes"):
        from billing import credits as _credits_mod
        if _credits_mod.is_credit_paid_tool(name):
            _cr_account = _credits_mod.resolve_account(headers or {})
            if _cr_account and not _credits_mod.is_free_key(_cr_account):
                # Grandfather: auto-create account on first encounter
                # (idempotent, fail-open -- never blocks a paid call)
                try:
                    await _credits_mod.ensure_grandfather(_cr_account)
                except Exception:
                    pass

                async def _credit_dispatch() -> dict:
                    return await _dispatch_operation(
                        name, arguments, headers or {}, skip_auth=True
                    )

                try:
                    _cr_result = await _credits_mod.run_metered_tool(
                        name, _cr_account, _credit_dispatch
                    )
                except RuntimeError:
                    # Supabase unreachable -- fail closed: refuse paid work
                    _cr_result = {
                        "status": "failure",
                        "reason_code": "billing_unavailable",
                        "human_message": (
                            "Credits billing temporarily unavailable. "
                            "Please retry in a moment."
                        ),
                    }

                _is_cr_err = (
                    _cr_result.get("status") == "failure"
                    or "reason_code" in _cr_result
                )
                return {
                    "content": [
                        {"type": "text",
                         "text": json.dumps(_cr_result, indent=2, default=str)}
                    ],
                    "isError": _is_cr_err,
                }
    # --- END SLICE 3 ---

    # -----------------------------------------------------------------------
    # DATA QUOTA GATE (DATA_METERING_ENABLED=true only)
    # -----------------------------------------------------------------------
    # At this point, x402-paying agents have already returned via the x402
    # gate above. Credit-account holders have already returned via the credits
    # gate above. Remaining callers are: email-verified free-key holders and
    # anonymous callers.  Apply per-caller daily quota here.
    #
    # Within quota  -> call proceeds free (quota decremented, cost=0).
    # Beyond quota  -> honest failure, status=failure, reason_code=free_quota_exceeded,
    #                  cost=0, tool NOT dispatched.
    if name in _PREMIUM_DATA_TOOLS and _data_metering_on:
        from billing import data_quota as _dq
        _dm_token = (headers or {}).get("x-agent-identity", "")
        # Prefer the leftmost IP from X-Forwarded-For (closest real client).
        _dm_fwd = (headers or {}).get("x-forwarded-for", "") or ""
        _dm_ip = (_dm_fwd.split(",")[0].strip()
                  if _dm_fwd else
                  (headers or {}).get("x-real-ip", ""))
        # Defense-in-depth: outer 2.5s hard wall-clock timeout at the call site.
        # Even a total hang inside consume_data_quota (e.g. asyncio.wait_for
        # inside data_quota.py fires but re-raises unexpectedly) costs at most
        # 2.5s then the gate fails-open.  asyncio.TimeoutError is a subclass of
        # Exception so (Exception,) covers both branches.
        try:
            _quota_check = await asyncio.wait_for(
                _dq.consume_data_quota(
                    name=name, token=_dm_token, ip=_dm_ip, headers=headers or {}
                ),
                timeout=2.5,
            )
        except (asyncio.TimeoutError, Exception) as _qe:
            import logging as _qlog
            _qlog.getLogger("smb_broker.mcp_server").warning(
                "data_quota gate timed out or errored name=%s err=%s -- failing open",
                name, type(_qe).__name__,
            )
            _quota_check = {"allowed": True, "remaining": -1}
        if not _quota_check["allowed"]:
            _qr = _quota_check["response"]
            return {
                "content": [
                    {"type": "text", "text": json.dumps(_qr, indent=2, default=str)}
                ],
                "isError": True,
            }
        # Within quota -- fall through to free dispatch below.
    # -----------------------------------------------------------------------

    receipt = await _dispatch_operation(name, arguments, headers or {})

    # FIX 5 (2026-08-23): Inject quota block into every gated tool response so
    # callers can see how many free-tier ops remain without a separate API call.
    # Added for gated tools only (write tools that consume daily budget).
    if name in _WRITE_TOOLS_REQUIRING_AUTH:
        token = (headers or {}).get("x-agent-identity", "")
        _inject_quota_block(receipt, token)

    return {
        "content": [
            {"type": "text", "text": json.dumps(receipt, indent=2, default=str)}
        ],
        "isError": receipt.get("status") == "failure",
    }


# Premium data tools gated by DATA_METERING_ENABLED.
# When the flag is false (default prod state) these run completely free.
# When true they enter the freemium quota path (see _h_tools_call below).
_PREMIUM_DATA_TOOLS: frozenset[str] = frozenset({
    "verify_company_record",
    "screen_sanctions",
    "map_trade_restriction",
})

# Tools that mutate state or charge upstream credits. The MCP dispatcher must
# gate these the same way /ops/* gates them — otherwise a developer-tier
# customer can bypass scope-checks by tunneling write calls through /mcp.
# Read-only tools (find_business, verify_business, get_status, get_outcome,
# preview_cost, self_test) stay anonymous-accessible per the manifest's
# readOnlyHint annotation.
_WRITE_TOOLS_REQUIRING_AUTH = frozenset({
    "send_message",
    "schedule_appointment",
    "send_transactional_confirmation",
    "capture_lead",
    "handle_inbound",
    "escalate_to_human",
    "import_booking_url",
    "call_business",
})


def _inject_quota_block(receipt: dict, token: str) -> None:
    """
    FIX 5 (2026-08-23): Inject a `quota` block into the receipt dict for
    free-tier callers so they can see remaining daily ops without a separate
    call.  Mutates `receipt` in place.  Never raises.
    """
    try:
        if not token or token in ("", "anonymous"):
            return
        from agent_interface.key_request_logic import (
            is_free_key, get_free_daily_remaining, FREE_TIER_DAILY_LIMIT as _free_limit)
        from agent_interface.identity import validate_token
        from datetime import datetime, timezone, timedelta

        pre_check = validate_token(token)
        if not (pre_check and pre_check.valid):
            return
        aid = pre_check.identity.agent_id
        if not is_free_key(aid):
            return

        remaining = get_free_daily_remaining(aid)
        # Calculate UTC midnight (start of next day)
        now_utc = datetime.now(timezone.utc)
        midnight_utc = (now_utc + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        receipt["quota"] = {
            "tier": "free",
            "remaining_today": remaining,
            # Derive from the constant, never hardcode - a hardcoded quota
            # advertises a number the server does not actually enforce.
            "daily_limit": _free_limit,
            "resets": midnight_utc.strftime("%Y-%m-%dT00:00:00Z"),
        }
    except Exception:  # noqa: BLE001 — quota injection never breaks dispatch
        pass


def _mcp_gate_identity(name: str, headers: dict) -> None:
    """
    Apply the same auth gate /ops/* applies, expressed as a JSON-RPC -32602.

    When REQUIRE_AUTH=false (current production state), `_get_identity` returns
    None and this is a no-op. When REQUIRE_AUTH=true, missing / invalid /
    out-of-scope tokens raise HTTPException(401|403), which we translate to
    _ParamError so the JSON-RPC layer surfaces it as code -32602.
    """
    if name not in _WRITE_TOOLS_REQUIRING_AUTH:
        return
    # Advertise the limit we actually enforce - never a literal.
    from agent_interface.key_request_logic import FREE_TIER_DAILY_LIMIT as _free_limit_msg
    # Late import to avoid a hard cycle between main.py and mcp_server.py.
    from main import _get_identity
    from fastapi import HTTPException

    token = headers.get("x-agent-identity") if headers else None

    # Free-tier daily limit check: if the caller holds a free key, enforce
    # the 50-ops/day cap before hitting the full identity gate.
    if token and token != "anonymous":
        from agent_interface.key_request_logic import is_free_key, consume_free_daily, get_free_daily_remaining
        from agent_interface.identity import validate_token
        pre_check = validate_token(token)
        if pre_check.valid and is_free_key(pre_check.identity.agent_id):
            agent_id = pre_check.identity.agent_id
            if not consume_free_daily(agent_id):
                remaining = get_free_daily_remaining(agent_id)
                from datetime import datetime, timezone, timedelta as _td
                _now = datetime.now(timezone.utc)
                _midnight = (_now + _td(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).strftime("%Y-%m-%dT00:00:00Z")
                _ms_to_reset = int(
                    ((_now + _td(days=1)).replace(hour=0, minute=0, second=0,
                                                  microsecond=0) - _now
                     ).total_seconds() * 1000
                )
                raise _ToolError(
                    f"free_tier_daily_limit_exceeded for tool '{name}': "
                    f"your free key allows {_free_limit_msg} gated operations per day. "
                    f"Remaining today: {remaining}; resets {_midnight}. "
                    f"Buy credits at https://hatchloop.dev/pricing for no daily cap.",
                    error_code="rate_limited",
                    retriable=True,
                    retry_after_ms=_ms_to_reset,
                    how_to_resolve={
                        "wait_until": _midnight,
                        "upgrade": "https://hatchloop.dev/pricing",
                        "note": "credit packages remove the daily cap; x402 per-call also available",
                    },
                )
            # Free key with remaining budget — skip the full gate below
            return

    try:
        _get_identity(token, name)
    except HTTPException as he:
        # 401 unauthenticated or 403 insufficient scope — surface as a clear,
        # actionable JSON-RPC params error rather than a generic -32603.
        #
        # This error IS the storefront: it is the only moment an evaluating
        # agent is told how to become a paying one. Two paths are offered:
        # a free email-verified key (immediate, FREE_TIER_DAILY_LIMIT/day) and
        # the paid plan.
        import os as _os
        checkout = _os.getenv("POLAR_CHECKOUT_URL", "").strip()
        # Use PUBLIC_BASE_URL (the Render origin) for /keys/* routes — MCP_PUBLIC_URL
        # points at the edge worker which doesn't serve /keys/ endpoints.
        base_url = _os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")
        free_key_url = f"{base_url}/keys/request"
        how_to_buy = (
            f" To get access: Option 1 (free): get a verified free key ({_free_limit_msg} ops/day) at "
            f"{free_key_url} (just provide your email, no payment needed). "
            f"Option 2 (credits): buy a credit package (Starter $9/1,000 credits, Growth $29/3,500, "
            f"Scale $99/13,000) at https://hatchloop.dev/pricing; or agents may pay per-call via x402. "
            f"Both options email you an X-Agent-Identity token; send it as a header on every call. "
            f"Read-only tools (find_business, verify_business, preview_cost, get_status) stay free."
            if checkout else
            f" Get a free API key at {free_key_url} ({_free_limit_msg} ops/day, email verification required). "
            f"Credit packages from $9/1,000 credits at https://hatchloop.dev/pricing."
        )
        raise _ToolError(
            f"auth_required for tool '{name}' (status={he.status_code}): "
            f"{he.detail}{how_to_buy}",
            error_code="auth_required",
            retriable=False,
            how_to_resolve={
                "free_key": {"url": free_key_url,
                             "note": f"email-verified, {_free_limit_msg} ops/day, no payment"},
                "credits": {"url": "https://hatchloop.dev/pricing",
                            "note": "packages from $9/1,000 credits"},
                "x402": {"note": "agents can pay per-call in USDC on Base"},
                "header": "X-Agent-Identity",
            },
        )


async def _dispatch_operation(
    name: str, args: dict, headers: Optional[dict] = None, skip_auth: bool = False
) -> dict:
    # Auth gate — runs before any side-effecting handler. Read-only tools
    # bypass the gate by virtue of not being in _WRITE_TOOLS_REQUIRING_AUTH.
    #
    # skip_auth=True is set by the x402 paid path: a settled USDC payment IS the
    # authorization for an autonomous agent, so also demanding an X-Agent-Identity
    # token would defeat the whole no-signup premise. Identity-token auth still
    # applies to the (free) non-x402 path when REQUIRE_AUTH is on.
    if not skip_auth:
        _mcp_gate_identity(name, headers or {})
    """Route an operation call to the underlying handler. Returns dict, not OutcomeReceipt."""
    if name == "find_business":
        from core.find_business import handle_find_business
        from core.models import FindBusinessRequest
        # Pass the raw vertical string through to FindBusinessRequest so its
        # model_validator(mode="before") can alias natural terms (plumbing,
        # dentist, haircut, ...) into the three macro buckets before enum
        # coercion happens. The previous code called Vertical(args[...])
        # directly, which 500'd on every fine-grained input the validator was
        # designed to fix.
        req = FindBusinessRequest(
            vertical=args["vertical"],
            location=args.get("location", {"zip_or_city": "Atlanta"}),
            capability=args.get("capability"),
            max_results=args.get("max_results", 5),
        )
        receipt = await handle_find_business(req)

    elif name == "verify_business":
        from core.verify_business import handle_verify_business
        from core.models import VerifyBusinessRequest
        req = VerifyBusinessRequest(
            smb_id=args["smb_id"],
            capability_to_verify=args.get("capability_to_verify"),
        )
        receipt = await handle_verify_business(req)

    elif name == "preview_cost":
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        req = PreviewCostRequest(
            operation=args["operation"],
            params=args.get("params", {}),
        )
        resp = await handle_preview_cost(req)
        return resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__

    elif name == "schedule_appointment":
        from core.schedule_appointment import handle_schedule_appointment
        from core.models import ScheduleAppointmentRequest, AppointmentAction
        req = ScheduleAppointmentRequest(
            smb_id=args["smb_id"],
            action=AppointmentAction(args.get("action", "book")),
            service=args.get("service"),
            existing_appointment_id=args.get("existing_appointment_id"),
        )
        receipt = await handle_schedule_appointment(req)

    elif name == "capture_lead":
        from core.capture_lead import handle_capture_lead
        from core.models import CaptureLeadRequest, ProspectData
        prospect_data = args.get("prospect", {})
        req = CaptureLeadRequest(
            smb_id=args["smb_id"],
            prospect=ProspectData(**prospect_data),
            source=args.get("source", "agent"),
        )
        receipt = await handle_capture_lead(req)

    elif name == "call_business":
        from core.call_business import handle_call_business
        from core.models import CallBusinessRequest
        req = CallBusinessRequest(
            business_phone=args.get("business_phone"),
            smb_id=args.get("smb_id"),
            objective=args["objective"],
            extract_fields=args.get("extract_fields", []),
            country_code=args.get("country_code"),
            on_behalf_of=args.get("on_behalf_of"),
            max_duration_seconds=args.get("max_duration_seconds", 180),
        )
        receipt = await handle_call_business(req)

    elif name == "self_test":
        from agent_interface.self_test import run_self_test
        report = await run_self_test()
        return {
            "all_passed": report.all_passed,
            "passed": report.passed_checks,
            "failed": report.failed_checks,
            "total": report.total_checks,
            "latency_ms": report.latency_ms,
        }

    elif name == "check_booking_link":
        # Read-only pre-flight: classify a booking URL before the agent spends
        # money on import_booking_url + schedule_appointment. No network, no
        # state change. Returns a full OutcomeReceipt (dict), handled below.
        from core.check_booking_link import handle_check_booking_link
        receipt = await handle_check_booking_link(args["url"])

    elif name == "check_compliance":
        # Read-only pre-flight: run the outbound compliance gate in preview mode
        # (no send, no audit write) before the agent pays for send_message /
        # call_business. Returns a full OutcomeReceipt (dict), handled below.
        from core.check_compliance import handle_check_compliance
        receipt = await handle_check_compliance(
            recipient_id=args["recipient_id"],
            content=args["content"],
            channel=args.get("channel"),
            message_type=args.get("message_type", "transactional"),
            country_code=args.get("country_code"),
            state_code=args.get("state_code"),
        )

    elif name == "get_status":
        from core.status_outcome import handle_get_status
        return await handle_get_status(args["operation_id"])

    elif name == "get_conversation":
        from core.get_conversation import handle_get_conversation
        receipt = await handle_get_conversation(
            conversation_id=args.get("conversation_id"),
            reference=args.get("reference"),
            business_number=args.get("business_number"),
            agent_id=_agent_id_from_token((headers or {}).get("x-agent-identity", "")),
        )
        return receipt.model_dump() if hasattr(receipt, "model_dump") else receipt

    elif name == "get_outcome":
        from core.status_outcome import handle_get_outcome
        receipt = await handle_get_outcome(args["operation_id"])

    elif name == "send_message":
        from core.send_message import handle_send_message
        from core.models import (
            SendMessageRequest, ChannelPreference, MessageContent,
            Recipient, RecipientIdType, MessageType,
        )
        # Accept either the canonical schema (recipient: {id_type, id_value,
        # country_code}) or the legacy flat shape (recipient_id + recipient_type
        # + country_code) so an agent calling with the older surface still
        # works while the public manifest migrates.
        if "recipient" in args and isinstance(args["recipient"], dict):
            recipient = Recipient(**args["recipient"])
        else:
            legacy_type = args.get("recipient_type") or "smb_id"
            # Old "smb" alias maps to canonical "smb_id".
            if legacy_type == "smb":
                legacy_type = "smb_id"
            recipient = Recipient(
                id_type=RecipientIdType(legacy_type),
                id_value=args.get("recipient_id", ""),
                country_code=args.get("country_code"),
            )
        content = args.get("content") or args.get("message") or {}
        if not isinstance(content, dict):
            content = {"body": str(content)}
        req = SendMessageRequest(
            recipient=recipient,
            message_type=MessageType(args.get("message_type") or "transactional"),
            content=MessageContent(**content),
            preferred_channel=ChannelPreference(
                args.get("preferred_channel")
                or args.get("channel_preference")
                or "auto"
            ),
        )
        receipt = await handle_send_message(req)

    elif name == "send_transactional_confirmation":
        from core.send_transactional_confirmation import handle_send_transactional_confirmation
        from core.models import SendTransactionalConfirmationRequest
        req = SendTransactionalConfirmationRequest(**args)
        receipt = await handle_send_transactional_confirmation(req)

    elif name == "handle_inbound":
        from core.handle_inbound import handle_inbound as _handle_inbound
        from core.models import HandleInboundRequest
        req = HandleInboundRequest(**args)
        receipt = await _handle_inbound(req)

    elif name == "escalate_to_human":
        from core.escalate_to_human import handle_escalate_to_human
        from core.models import EscalateToHumanRequest
        req = EscalateToHumanRequest(**args)
        receipt = await handle_escalate_to_human(req)

    elif name == "import_booking_url":
        # The differentiator. Turns any public booking URL into a callable smb_id.
        from supply.booking_page_importer import import_from_booking_url, ImportRequest
        from core.models import Vertical
        vertical = None
        if args.get("vertical"):
            try:
                vertical = Vertical(args["vertical"])
            except Exception:
                vertical = None
        req = ImportRequest(
            booking_url=args["booking_url"],
            business_name=args.get("business_name"),
            vertical=vertical,
            country_code=args.get("country_code"),
            contact_phone=args.get("contact_phone"),
            contact_email=args.get("contact_email"),
            capabilities=args.get("capabilities", []),
        )
        result = await import_from_booking_url(req)
        return {
            "status": result.status.value,
            "smb_id": result.smb_id,
            "platform": result.platform.value if result.platform else None,
            "message": result.message,
            "next_steps": result.next_steps,
        }

    elif name == "verify_company_record":
        # Free read-only demand probe: live registry lookup via GLEIF + SEC EDGAR.
        from core.verify_company_record import handle_verify_company_record
        receipt = await handle_verify_company_record(
            name=args["name"],
            country=args.get("country"),
            lei=args.get("lei"),
        )

    elif name == "screen_sanctions":
        # Free read-only sanctions screening: OFAC SDN + OpenSanctions (40+ lists).
        from core.screen_sanctions import handle_screen_sanctions
        receipt = await handle_screen_sanctions(
            name=args["name"],
            country=args.get("country"),
            entity_type=args.get("type"),
        )

    elif name == "map_trade_restriction":
        # Free read-only cross-border trade-compliance snapshot: OFAC embargo
        # map + party sanctions screening (OpenSanctions + OFAC SDN) + tariff
        # guidance links.  Never fabricates a rate or a clear.
        from core.map_trade_restriction import handle_map_trade_restriction
        receipt = await handle_map_trade_restriction(
            product=args["product"],
            destination_country=args["destination_country"],
            hs_code=args.get("hs_code"),
            origin_country=args.get("origin_country"),
            parties=args.get("parties"),
        )

    else:
        raise _ParamError(f"Tool '{name}' is registered but not yet routed in MCP dispatcher.")

    # Durable billing — fire-and-forget meter record for every tool call.
    # Works whether x402 is on or off. amount_usd=0 when x402 disabled (now).
    #
    # FIX 1 (cred hygiene): use _agent_id_from_token so we record the PARSED
    # agent_id (e.g. "free_4f19be4f44ef1e0f") and never a slice of the raw
    # bearer token (which would start with "eyJ...").
    _raw_token = (headers or {}).get('x-agent-identity') or ''
    _agent_id = _agent_id_from_token(_raw_token)
    try:
        from billing.durable_meter import get_durable_meter
        _cost = getattr(receipt, 'cost', None)
        _amount = float(_cost.amount) if _cost and hasattr(_cost, 'amount') else 0.0
        _basis = _cost.basis if _cost and hasattr(_cost, 'basis') else 'per_call'
        _ch = getattr(receipt, 'channel_used', None)
        _op_id = getattr(receipt, 'operation_id', name)
        _success = getattr(receipt, 'status', None)
        _success_bool = str(_success) != 'failure' if _success else True
        get_durable_meter().record(
            agent_id=_agent_id,
            operation=name,
            operation_id=str(_op_id),
            amount_usd=_amount,
            basis=_basis,
            channel_used=_ch,
            success=_success_bool,
        )
    except Exception:
        pass  # billing must never break tool dispatch

    # Convert OutcomeReceipt → dict
    if hasattr(receipt, 'model_dump'):
        receipt_dict = receipt.model_dump()
    else:
        receipt_dict = dict(receipt) if isinstance(receipt, dict) else receipt.__dict__

    # FIX 2 (audit-trail): read-only operations (get_status, get_outcome, etc.)
    # must NEVER upsert over the existing durable row — that would overwrite the
    # originating tool name with the reader name (e.g. tool="get_outcome" over
    # tool="handle_inbound").  Only state-changing tools write a row.
    _PERSIST_SKIP_TOOLS = frozenset({
        "get_status", "get_outcome",
        "find_business", "verify_business", "preview_cost", "self_test",
        "check_booking_link", "check_compliance", "verify_company_record",
        "screen_sanctions", "map_trade_restriction",
    })

    # FIX 1 (durable store) + FIX 5 (quota strip): persist operation to durable
    # store so get_status/get_outcome can retrieve it across requests.
    # Strip the transient `quota` block before persisting — quota belongs to the
    # response envelope only, not to the durable record.
    # Fail-open: a store error must never break the tool call.
    if name not in _PERSIST_SKIP_TOOLS:
        try:
            from storage.outcome_store import get_outcome_store
            _persist_op_id = receipt_dict.get('operation_id')
            if _persist_op_id:
                # Copy and strip quota so the async upsert never serialises it
                # even if _inject_quota_block mutates receipt_dict after we return.
                _persist_receipt = {k: v for k, v in receipt_dict.items() if k != 'quota'}
                get_outcome_store().set_complete(
                    _persist_op_id,
                    _persist_receipt,
                    tool=name,
                    agent_id=_agent_id if _agent_id != 'anonymous' else None,
                )
        except Exception:
            pass

    return receipt_dict


# ---------------------------------------------------------------------------
# Method: resources/list & resources/read
# ---------------------------------------------------------------------------

async def _h_resources_list(params: dict) -> dict:
    return {
        "resources": [
            {
                "uri": "agent-broker://manifest",
                "name": "Capability Manifest",
                "description": "Full manifest with all operations and their schemas.",
                "mimeType": "application/json",
            },
            {
                "uri": "agent-broker://booking_platforms",
                "name": "Supported Booking Platforms",
                "description": "12 booking platforms import_booking_url accepts (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) with regex patterns and example URLs. Use this resource to teach end-users which URL formats are acceptable.",
                "mimeType": "application/json",
            },
            {
                "uri": "agent-broker://errors",
                "name": "Error Code Catalog",
                "description": "All 16 error codes with retry semantics.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "agent-broker://compliance/jurisdictions",
                "name": "Jurisdiction Rules",
                "description": "Compliance rules by country/state — TCPA, GDPR, CASL, recording consent. 26 jurisdictions + INTERNATIONAL fallback.",
                "mimeType": "application/json",
            },
            {
                "uri": "agent-broker://cookbook",
                "name": "Tool-chain cookbook",
                "description": "Common multi-tool flows: 'book-from-url', 'find-then-book', 'compliant-transactional-message', 'async-poll'. Read this if you are unsure which tool to call first.",
                "mimeType": "text/markdown",
            },
        ]
    }


async def _h_resources_read(params: dict) -> dict:
    uri = params.get("uri")
    # Accept both legacy `smb-broker://` and canonical `agent-broker://` schemes
    norm = uri.replace("smb-broker://", "agent-broker://", 1) if isinstance(uri, str) else uri

    if norm == "agent-broker://manifest":
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(get_full_manifest(), indent=2),
            }]
        }
    if norm == "agent-broker://booking_platforms":
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps({
                    "platforms": [
                        {"name": "Cal.com",     "example": "https://cal.com/peer"},
                        {"name": "Calendly",    "example": "https://calendly.com/acme/intro"},
                        {"name": "Doctolib",    "example": "https://www.doctolib.fr/dentiste/paris/jean-dupont"},
                        {"name": "Booksy",      "example": "https://booksy.com/en-us/123_jane-salon"},
                        {"name": "Fresha",      "example": "https://fresha.com/a/jane-salon"},
                        {"name": "OpenTable",   "example": "https://www.opentable.com/r/acme-bistro"},
                        {"name": "Setmore",     "example": "https://setmore.com/jane-salon"},
                        {"name": "Square",      "example": "https://jane.square.site"},
                        {"name": "Acuity",      "example": "https://app.acuityscheduling.com/schedule.php?owner=12345"},
                        {"name": "Schedulista", "example": "https://www.schedulista.com/jane-salon"},
                        {"name": "Squarespace", "example": "https://jane.squarespace-scheduling.com"},
                        {"name": "BookMyCity",  "example": "https://bookmycity.com/jane-salon"},
                    ],
                    "import_tool": "import_booking_url",
                    "next_tool": "schedule_appointment",
                }, indent=2),
            }]
        }
    if norm == "agent-broker://errors":
        from pathlib import Path
        path = Path(__file__).parent.parent / "api" / "errors.md"
        if path.exists():
            return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                                  "text": path.read_text()}]}
    if norm == "agent-broker://compliance/jurisdictions":
        from compliance.jurisdiction_rules import _RULES
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps({k: vars(v) for k, v in _RULES.items()},
                                   indent=2, default=str),
            }]
        }
    if norm == "agent-broker://cookbook":
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/markdown",
                "text": (
                    "# Agent Broker tool-chain cookbook\n\n"
                    "## When the user provides a booking URL\n"
                    "1. `import_booking_url(booking_url=<url>)` -> returns `smb_id`\n"
                    "2. `schedule_appointment(smb_id=<above>, action='book', preferred_time=...)`\n"
                    "3. (optional) `send_transactional_confirmation(...)` to email the receipt\n\n"
                    "## When the user describes a business by category + city\n"
                    "1. `find_business(vertical, location, capability, max_results=5)`\n"
                    "2. If `result.businesses` is non-empty, pick one and `schedule_appointment(smb_id=...)`\n"
                    "3. If empty AND the user can supply a URL, fall through to `import_booking_url`\n\n"
                    "## When sending an outbound message\n"
                    "`send_message` supports five `message_type` values: transactional, "
                    "reminder, follow_up, notification, marketing. Marketing requires a "
                    "valid `consent_record_id` referencing a recorded opt-in in the "
                    "consent_store; the compliance gate verifies at send time and rejects "
                    "any marketing send without recorded consent (TCPA / GDPR / CASL / PDPL "
                    "across 22 jurisdictions). Cold outreach, drip campaigns, bulk lists, "
                    "and A/B sends are out of scope and rate-limited regardless.\n"
                    "1. (optional) `POST /compliance/check` to preview legality for the jurisdiction\n"
                    "2. `send_message(...)` — gate runs again at send time\n"
                    "3. Treat ComplianceViolationError as a stop signal, not retry\n\n"
                    "## Async polling\n"
                    "Long ops return `OperationStatus.PENDING`. Poll `get_status(operation_id)` "
                    "every 1-3 seconds, then fetch `get_outcome(operation_id)` once status is "
                    "`success` or `failure`.\n"
                ),
            }]
        }
    raise _ParamError(f"Unknown resource URI: {uri}")


# ---------------------------------------------------------------------------
# Method: prompts/list — suggested prompts for common agent workflows
# ---------------------------------------------------------------------------

async def _h_prompts_list(params: dict) -> dict:
    return {
        "prompts": [
            {
                "name": "book_from_any_url",
                "description": (
                    "PRIMARY FLOW. The user gave you a Cal.com / Calendly / "
                    "Doctolib / Booksy / Fresha / OpenTable / Setmore / Square / "
                    "Acuity / Schedulista / Squarespace / BookMyCity URL. Do this: "
                    "1) call import_booking_url with the URL. "
                    "2) call schedule_appointment with the returned smb_id. "
                    "3) (optional) call send_transactional_confirmation. "
                    "Total: 2 tool calls for a complete booking. Use this whenever "
                    "the user mentions a booking URL — it is faster and more reliable "
                    "than find_business + manual lookup."
                ),
                "arguments": [
                    {"name": "booking_url", "description": "Any URL on a supported booking platform.", "required": True},
                    {"name": "preferred_time", "description": "When the user wants to be booked (free-form, e.g. 'next Tuesday at 3pm PT').", "required": False},
                ],
            },
            {
                "name": "find_then_book",
                "description": (
                    "Use when the user describes a business by category + location but does not provide a URL. "
                    "1) call find_business(vertical, location, capability). "
                    "2) if results exist, call schedule_appointment with the chosen smb_id. "
                    "3) if NO results, call import_booking_url with any URL the user CAN provide, then schedule_appointment. "
                    "Total: 2-3 tool calls."
                ),
                "arguments": [
                    {"name": "vertical", "description": "personal_services | home_services | professional_services | restaurants | healthcare | fitness", "required": True},
                    {"name": "location", "description": "ZIP or city or country.", "required": True},
                    {"name": "capability", "description": "Service needed (e.g. haircut, plumbing, dental cleaning).", "required": True},
                ],
            },
            {
                "name": "compliant_transactional_message",
                "description": (
                    "Send a consumer-initiated transactional SMS / email / voice with full "
                    "TCPA / GDPR / CASL pre-check. ONLY use when the end-user explicitly "
                    "asked the agent to communicate with a named SMB on their behalf — "
                    "confirming a booking, replying to a quote, following up on an inbound "
                    "the SMB sent first. This flow is NOT for marketing, cold outreach, or "
                    "prospecting; those are rejected at schema validation. "
                    "1) (optional) call /compliance/check first to preview legality. "
                    "2) call send_message — the gate runs again at send time and blocks any "
                    "non-compliant send. Use country_code so the right jurisdiction rules apply."
                ),
                "arguments": [
                    {"name": "recipient", "description": "Phone (E.164) or email of the SMB the consumer named, or the consumer themselves for a transactional confirmation.", "required": True},
                    {"name": "message_type", "description": "transactional | marketing | reminder | follow_up | notification. Marketing requires a valid consent_record_id; the gate verifies and rejects unrecorded consent.", "required": True},
                    {"name": "country_code", "description": "ISO 3166-1 alpha-2 (e.g. 'US', 'DE'). Auto-inferred from phone if omitted.", "required": False},
                ],
            },
            {
                "name": "cost_estimation",
                "description": "Get a cost estimate before committing to a paid operation. Free.",
                "arguments": [
                    {"name": "operation", "description": "Operation name (any tool from tools/list).", "required": True},
                ],
            },
        ]
    }


# ---------------------------------------------------------------------------
# Method: prompts/get — return the actual prompt body for a given name
# ---------------------------------------------------------------------------

async def _h_prompts_get(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments", {}) or {}
    if name == "book_from_any_url":
        url = args.get("booking_url", "<URL>")
        time = args.get("preferred_time", "the user's preferred time")
        text = (
            f"Step 1: call import_booking_url with booking_url={url}. "
            f"Step 2: take the returned smb_id and call schedule_appointment "
            f"with that smb_id, action=book, and the user's preferred time ({time}). "
            f"Step 3: if booking succeeds, optionally call send_transactional_confirmation "
            f"to email the receipt. Total: 2-3 tool calls."
        )
    elif name == "find_then_book":
        text = (
            "Step 1: call find_business(vertical, location, capability). "
            "Step 2: if result.businesses is non-empty, pick one and call "
            "schedule_appointment(smb_id, action='book', preferred_time=...). "
            "Step 3: if result.businesses is empty AND the user provided a URL, "
            "call import_booking_url first, then schedule_appointment with the "
            "newly-imported smb_id."
        )
    elif name == "compliant_transactional_message":
        text = (
            "ONLY use this for consumer-initiated transactional flows — the end-user explicitly "
            "asked the agent to message a named business on their behalf. Cold outreach, "
            "marketing, and prospecting are out of scope and rejected at schema validation. "
            "Step 1 (optional preview): POST /compliance/check with the recipient + "
            "message_type (transactional | reminder | follow_up | notification) + content + "
            "country_code. Returns {legal: bool, rule, remediation}. "
            "Step 2: call send_message with the same args. The compliance gate runs again "
            "at send time, so a non-compliant send raises ComplianceViolationError "
            "instead of leaking. Treat any non-200 from the gate as a stop signal."
        )
    elif name == "cost_estimation":
        text = (
            "Call preview_cost(operation=<name>, params=<the args you would pass>). "
            "Returns {estimated_amount_usd, currency, basis}. Free, idempotent."
        )
    else:
        raise _ParamError(f"Unknown prompt name: {name}")
    return {
        "messages": [
            {"role": "user", "content": {"type": "text", "text": text}},
        ]
    }


# ---------------------------------------------------------------------------
# Method: ping
# ---------------------------------------------------------------------------

async def _h_ping(params: dict) -> dict:
    return {}


# ---------------------------------------------------------------------------
# Method dispatch table
# ---------------------------------------------------------------------------

_METHOD_HANDLERS = {
    "initialize": _h_initialize,
    "ping": _h_ping,
    "tools/list": _h_tools_list,
    "tools/call": _h_tools_call,
    "prompts/get": _h_prompts_get,
    "resources/list": _h_resources_list,
    "resources/read": _h_resources_read,
    "prompts/list": _h_prompts_list,
}
