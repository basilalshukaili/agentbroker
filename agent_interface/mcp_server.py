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

# Tool arguments are parsed into pydantic models, so a caller's bad argument
# arrives here as ValidationError. It is caught explicitly in the dispatcher -
# see the handler below for why it must not fall through to ERR_INTERNAL. The
# fallback keeps this module importable if pydantic ever moves; `except
# _Unraisable` then simply never matches, which degrades to the old behaviour
# instead of breaking every request.
try:
    from pydantic import ValidationError
except Exception:  # noqa: BLE001 - pragma: no cover
    class ValidationError(Exception):  # type: ignore[no-redef]
        """Never raised - a placeholder so the except clause stays valid."""

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

def _total_tool_count() -> int:
    """Derived, not typed. I hardcoded "12 of the 23 tools" into the
    instructions string one commit after building a CI gate that fails the
    build for exactly that - the habit is stronger than the rule, which is
    why the rule has to be a function rather than a reminder."""
    try:
        from agent_interface.manifest_server import get_full_manifest
        return len(get_full_manifest().get("operations") or [])
    except Exception:                           # noqa: BLE001
        return 0


def _keyless_count() -> int:
    """Tools callable with no key: total minus the write tools that need one."""
    total = _total_tool_count()
    return max(0, total - len(_WRITE_TOOLS_REQUIRING_AUTH)) if total else 0


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
                "map_trade_restriction", "lookup_us_contracts",
                "check_quota",
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
                "lookup_us_contracts",  # read-only USASpending.gov contract search
                "check_quota",         # read-only quota state — never consumes quota
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
            # "COST: free" ALONE IS NOT ENOUGH, because free and keyless are two
            # different things and we were conflating them.
            #
            # `import_booking_url` costs zero credits AND requires a free
            # email-verified key. Labelled just "COST: free" it reads as "call
            # it now", so an agent's first attempt fails on auth - and a careful
            # buyer counting our free tools got 13 from tools/list while the
            # pricing page said 12, concluded our surfaces contradict each
            # other, and was right that something was wrong even though both
            # numbers were defensible.
            #
            # Twelve tools need no key. Thirteen cost nothing. Say which is
            # which on the tool itself rather than making the reader reconcile
            # two counts.
            if op.get("name") in _WRITE_TOOLS_REQUIRING_AUTH:
                parts.append("COST: free (no credits) - but requires a free "
                             "email-verified key")
            else:
                parts.append("COST: free - no key required")
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


async def handle_mcp_request(payload: dict, headers: Optional[dict] = None,
                             profile: Optional[str] = None) -> dict:
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
    if profile is not None:
        # Which door this request came through. Never taken from the
        # payload - a caller must not be able to widen its own profile
        # by sending _profile itself, so the server-side value always
        # overwrites whatever arrived.
        params = {**params, "_profile": profile}
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
        # FIX (2026-09-01): also pass principal_type so classify_session_kind
        # can emit 'verified_agent_key' vs 'verified_human_key' correctly.
        try:
            tool_name = params.get("name") if method == "tools/call" else None
            arguments = params.get("arguments") if method == "tools/call" else None
            ip = norm_headers.get("x-forwarded-for", norm_headers.get("x-real-ip", ""))
            ua = norm_headers.get("user-agent", "")
            raw_key = norm_headers.get("x-agent-identity", "")
            if not raw_key:
                auth = norm_headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    raw_key = auth[7:].strip()
            key_id = _agent_id_from_token(raw_key)
            principal_type = _principal_type_from_token(raw_key)
            from billing.usage_logger import fire_log_usage
            fire_log_usage(method, tool_name, arguments, ip, ua, key_id,
                           principal_type=principal_type)
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
    except ValidationError as ve:
        # THE SAME BUG THE KeyError HANDLER ABOVE WAS WRITTEN TO FIX, through a
        # hole that handler did not cover.
        #
        # Tool arguments are parsed into pydantic models. A wrong TYPE, a
        # malformed nested object, or a field missing from a nested model
        # raises ValidationError rather than KeyError - so it fell to the
        # generic handler below and came back as `-32603 Internal error: 1
        # validation error for ProspectData...`.
        #
        # For an agent those two codes mean opposite things. ERR_INTERNAL says
        # "the server is broken" - back off, retry, or abandon the task. The
        # truth was "your arguments are wrong" - fixable on the next call, and
        # only by the caller. An agent that retries an unfixed request burns
        # its budget and gives up on a marketplace that was working fine.
        #
        # So: name every bad field, and say plainly that retrying unchanged
        # will not help.
        try:
            fields = []
            for err in ve.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                fields.append(f"{loc or '<root>'} ({err.get('msg', 'invalid')})")
        except Exception:  # noqa: BLE001 - never let error reporting itself fail
            fields = []
        detail = "; ".join(fields[:6]) or str(ve)[:200]
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(
                ERR_INVALID_PARAMS,
                f"invalid arguments: {detail}. Call tools/list and use the "
                f"inputSchema for this tool - names, types and nesting are exact.",
                data={"error_code": "invalid_argument",
                      "retriable": False,
                      "invalid_fields": fields[:6],
                      "how_to_resolve": {
                          "call": "tools/list",
                          "hint": "fix the named fields; retrying unchanged "
                                  "will fail identically",
                      }}),
        ).to_dict()
    except Exception as exc:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INTERNAL, f"Internal error: {exc}"),
        ).to_dict()


class _ParamError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Safe coercion. Bad input must not reach code that raises the wrong exception.
# ---------------------------------------------------------------------------
#
# Adding another `except` clause to the dispatcher was the tempting fix and the
# wrong one. TypeError and ValueError are raised by plenty of GENUINE internal
# faults, so catching them broadly would report our own bugs to the caller as
# their mistake, with `retriable: false` - telling an agent never to retry a
# transient server fault. That trade is worse than the bug.
#
# So the two shapes that actually failed are converted at the point of use into
# _ParamError, which already carries the typed invalid-argument contract:
#
#   Model(**value)  where value is a string -> "argument after ** must be a
#                   mapping, not str", surfaced as -32603 Internal error
#   Enum(value)     with an unknown member  -> "'x' is not a valid MessageType"
#
# `send_message` already guarded its nested objects with isinstance checks; the
# pattern simply had not been applied to the other five construction sites.


def _as_dict(value, field: str) -> dict:
    """A nested object argument, or a typed error naming the field."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _ParamError(
            f"'{field}' must be a JSON object, got {type(value).__name__}. "
            f"Check the inputSchema for this tool - nesting is exact.")
    return value


def _as_enum(enum_cls, value, field: str):
    """An enum argument, or a typed error LISTING THE VALID VALUES.

    Naming the options matters more than naming the error: an agent told
    "'marketing_blast' is not a valid MessageType" has to go and fetch the
    schema, while one told the four permitted values can fix the call now.
    """
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        allowed = [getattr(m, "value", m) for m in enum_cls]
        raise _ParamError(
            f"'{field}' must be one of {allowed}, got {value!r}.") from None


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


def _principal_type_from_token(raw_token: str) -> Optional[str]:
    """Extract the principal type ('system' or 'human') from a bearer token.

    Used by usage telemetry to distinguish AI agent sessions from human
    subscriber sessions — the two groups have different quota/billing semantics
    and must not be merged into a single 'verified_human_key' bucket.

    Returns None for anonymous/invalid tokens; the classifier treats None as
    the safe default ('verified_human_key'), preserving backward compatibility
    for older tokens that pre-date this field.
    """
    if not raw_token or raw_token in ("", "anonymous"):
        return None
    try:
        from agent_interface.identity import validate_token
        result = validate_token(raw_token)
        if result.valid and result.identity and result.identity.principal:
            # PrincipalKind maps: "business" ← JWT "system", "consumer" ← JWT "human"
            kind = getattr(result.identity.principal.kind, "value",
                           result.identity.principal.kind)
            # Normalise back to the raw JWT terminology used by classify_session_kind.
            if kind == "business":
                return "system"
            if kind == "consumer":
                return "human"
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Method: initialize
# ---------------------------------------------------------------------------

async def _h_initialize(params: dict) -> dict:
    """Introduce the endpoint the caller actually reached.

    A CAPABILITY DOOR MUST NOT INTRODUCE ITSELF AS THE WIDE SERVER. This
    returned `serverInfo: agent-broker` and "use tools/list to see all 20
    operations" on EVERY endpoint, including `/mcp/sanctions-screening`, which
    serves 8. Two things were wrong with that, one practical and one serious:

    - practical: an agent that connected to the narrow door was told to expect
      twenty tools and then shown eight, which reads as a broken server;
    - serious: it is what makes three endpoints look like one server wearing
      three names. The MCP registry's moderation policy treats "the same server
      submitted multiple times under different names" as spam, and an
      `initialize` that says `agent-broker` on all of them is the evidence for
      that reading. The doors ARE distinct - different tools, different
      refusals, different token cost - and the handshake has to say so.

    Found by an external review, before publishing. It would have been the one
    detail that made an honest listing look like a duplicate.
    """
    from agent_interface import profiles as _profiles
    profile = params.get("_profile") if isinstance(params, dict) else None
    if profile:
        try:
            spec = _profiles.PROFILES[profile]
            names = _profiles.tools_for(profile)
        except (KeyError, _profiles.ProfileError):
            profile = None

    if profile:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": profile, "version": SERVER_VERSION},
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
                "prompts": {"listChanged": False},
                "logging": {},
            },
            "instructions": (
                f"{spec['description']} This endpoint serves {len(names)} tools; "
                f"call tools/list to see them. It refuses anything outside that "
                f"set - the full {len(get_full_manifest().get('operations', []))}"
                f"-tool server is at /mcp/agent-broker. Write operations require "
                f"an X-Agent-Identity token; call preview_cost first to confirm "
                f"the budget impact."
            ),
        }

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
            # "MOST" WAS WRONG AND DISCOURAGING. 12 of the 21 tools need no
            # key at all - 9 outright and 3 up to a daily quota - and this is
            # the first sentence every connecting client reads. Telling an
            # evaluator that most of the product is gated, when most of it is
            # not, is a self-inflicted wound at the moment of first contact.
            f"{_keyless_count()} of the {_total_tool_count()} tools need no "
            f"key at all; the {len(_WRITE_TOOLS_REQUIRING_AUTH)} write tools "
            f"require an X-Agent-Identity token in the underlying HTTP request. "
            "For state-changing operations (send_message, schedule_appointment), call preview_cost "
            "first to confirm the budget impact. "
            # The differentiator, stated at first contact — and only because it
            # is true: both tools attach an Ed25519-signed receipt (see
            # core/compliance_receipt.py), verifiable offline against the
            # public key published on hatchloop.dev/agents.md.
            "screen_sanctions and check_compliance answer from live official "
            "sources (OFAC/EU/UK lists, GLEIF, SEC EDGAR) and return an "
            "Ed25519-signed compliance receipt you can verify offline against "
            "the public key at https://hatchloop.dev/agents.md. "
            "Need fewer tools in context? Narrow endpoints serve one "
            "capability each: /mcp/compliance-check, /mcp/company-verification, "
            "/mcp/sanctions-screening (all free, no key), "
            "/mcp/appointment-booking, /mcp/sms-whatsapp-messaging."
        ),
    }


# ---------------------------------------------------------------------------
# Method: tools/list
# ---------------------------------------------------------------------------

async def _h_tools_list(params: dict) -> dict:
    """The tool list, narrowed to the door the caller came through.

    The profile arrives in `params["_profile"]`, injected server-side by
    handle_mcp_request - NOT read from the caller's payload, so nobody can widen
    their own door by sending it themselves. The dispatcher calls this handler
    with `params` alone, so taking it as a second argument would have meant it
    silently never arrived and every door served all 23 tools.

    `_profile` absent means the full server, byte-identical to before."""
    from agent_interface import profiles as _profiles
    allowed = _profiles.tools_for(params.get("_profile"))
    tools = _build_tool_list()
    if allowed is not None:
        tools = [t for t in tools if t.get("name") in allowed]
    return {"tools": tools}


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

    # `arguments` MUST BE AN OBJECT, and this is checked before anything reads
    # it. Every handler below does `args["x"]` or `args.get("x")`, so a JSON
    # array or string here raised `list indices must be integers` deep in a
    # handler and came back as -32603 "Internal error" - the server telling the
    # caller IT was broken over a one-character mistake in their request.
    #
    # And it was reachable WITH NO CREDENTIALS AT ALL. The auth gate only
    # covers the eight write tools, so all twelve read-only tools - the ones an
    # evaluating agent or a catalogue scorer tries first - took this path
    # anonymously. I had claimed in a commit message that only an authenticated
    # caller could reach it; that was wrong, and an external reviewer was right
    # to check rather than believe it.
    if not isinstance(arguments, dict):
        raise _ParamError(
            f"'arguments' must be a JSON object, got "
            f"{type(arguments).__name__}. Pass the tool's parameters as named "
            f"fields - see the inputSchema from tools/list.")

    # THE CHECK THAT MAKES A NARROW DOOR REAL. A profile that lists four tools
    # but executes twenty is a wide server wearing a small sign.
    _profile = params.get("_profile")
    from agent_interface import profiles as _profiles
    if not _profiles.allows(_profile, name):
        raise _ParamError(
            f"'{name}' is not available on this endpoint. This is the "
            f"'{_profile}' server, which exposes only its own tools. The full "
            f"server with every tool is at https://hatchloop.dev/mcp/agent-broker")

    op = get_operation(name)
    if not op:
        # Name the nearest real tool. An agent that typos or shortens a tool
        # name ("verify_company" for "verify_company_record") gets one useless
        # round-trip from a bare "Unknown tool"; naming the close matches lets
        # it fix the call immediately instead of re-fetching tools/list.
        try:
            import difflib
            known = [o.get("name", "") for o in
                     get_full_manifest().get("operations", [])]
            close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
        except Exception:  # noqa: BLE001 - suggestion is best-effort
            close = []
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise _ParamError(f"Unknown tool: '{name}'.{hint} Call tools/list "
                          f"for the full catalog.")

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
    # ONLY WHEN THE CALLER ACTUALLY PRESENTS A PAYMENT.
    #
    # This branch used to fire on `enabled() and is_paid_tool(name)` alone, which
    # put x402 in FRONT of the free quota and credits. Switching the flag on
    # would therefore have made every advertised free tier instantly false: the
    # "free email-verified key, 100 ops/day" and the "premium data free up to
    # 500/day" that are printed on the website, the README, the manifest and the
    # Smithery listing. Every agent using us for free would have got a payment
    # demand instead, with no code change and no announcement.
    #
    # Checked against PRODUCTION rather than the defaults, which made it worse:
    # DATA_METERING_ENABLED is true in prod, so the bypass above does not fire
    # and even the three data tools were exposed.
    #
    # x402 is what the storefront message already calls it - an ESCAPE PATH for
    # agents that have run out of free quota or would rather pay per call. So:
    # a caller who attaches a payment is served here; everyone else falls
    # through to credits, then the free quota, and an agent that is genuinely
    # out of quota gets an honest failure naming this as one way to proceed.
    from billing import x402_gate
    _meta = params.get("_meta") or {}
    _offered_payment = bool(isinstance(_meta, dict) and _meta.get("x402/payment"))
    if x402_gate.enabled() and x402_gate.is_paid_tool(name) and _offered_payment:
        async def _dispatch() -> dict:
            # Payment is the authorization here — bypass the identity gate.
            return await _dispatch_operation(name, arguments, headers or {}, skip_auth=True)
        return await x402_gate.run_paid_tool(name, arguments, _meta, _dispatch)

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

                # `"reason_code" in _cr_result` WAS ALWAYS TRUE.
                #
                # _dispatch_operation returns receipt.model_dump(), and
                # reason_code is a declared field on OutcomeReceipt - so the
                # key is present on every result, including successes where it
                # is None. Every credits-billed call was therefore returned to
                # the MCP client with isError: true.
                #
                # Settlement is decided correctly inside run_metered_tool via
                # _receipt_is_error, so the customer WAS charged - and then
                # told the call failed. An agent retrying that is billed twice.
                # Live: CREDITS_ENABLED is true on this service.
                #
                # The clause was also redundant: the insufficient-credits
                # envelope sets status="failure" as well. This now matches the
                # free path one screen down, which had it right all along.
                _is_cr_err = _cr_result.get("status") == "failure"
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
                        "note": "credit packages remove the daily cap",
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
            # THE CRYPTO RAIL BELONGS HERE, and its absence was the defect.
            #
            # This is what a live agent is told at the exact moment it hits a
            # quota and is deciding how to pay - the most consequential payment
            # surface we have. x402 is the ONLY option on this list an
            # autonomous agent can complete without a human: the other two need
            # someone to read an email or type a card.
            #
            # The comment that used to sit here said the rail was "switched
            # off" for a legal reason. The founder lifted that restriction on
            # 2026-08-29 and the rail is enabled on this service (all four
            # x402 variables are set, and mcp_server dispatches through
            # x402_gate.run_paid_tool for any call that attaches payment). The
            # discovery document has advertised it since. This message did not,
            # so the one self-serve path was hidden at the only moment it
            # mattered.
            f"Scale $99/13,000) at https://hatchloop.dev/pricing. "
            f"Option 3 (pay per call, no signup): attach an x402 payment in "
            f"params._meta['x402/payment'] and this call is served without "
            f"a key - USDC on Base. This is the only option that needs no "
            f"human. "
            f"Options 1 and 2 email you an X-Agent-Identity token; send it as a header on every call. "
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
                # THE ONLY PATH AN AUTONOMOUS AGENT CAN COMPLETE ALONE, and it
                # was the one path this error did not mention.
                #
                # Both options above require an email inbox: the free key is
                # emailed after verification, and credits need a magic-link
                # portal and a card. An agent has neither. So the most
                # consequential error message we serve told every stranger
                # agent that it had two routes, and both were closed to it -
                # while the server would, on that very call, have accepted two
                # cents of USDC and answered.
                #
                # The comment that used to sit here said the rail was "built
                # and switched off". It was ON: X402_ENABLED=true with a live
                # receiver address, returning complete payment offers. Founder
                # decision 2026-08-29 removed the legal block on advertising
                # it. Derived from the gate so it disappears if the rail does.
                **({"x402": {
                    "note": "no account, no email: attach a signed x402 payment "
                            "as params._meta['x402/payment'] and retry this "
                            "call. The server replies with a priced offer "
                            "(USDC on Base) the first time.",
                    "how": "retry with _meta['x402/payment'] set to any value "
                           "to receive the payment offer",
                }} if _x402_live() else {}),
                "header": "X-Agent-Identity",
            },
        )



def _x402_live() -> bool:
    """Is the pay-per-call rail actually accepting payment right now?

    Read at call time, never cached into a message. The whole defect this
    exists to prevent was a hardcoded claim about the payment rail that stayed
    put while the rail changed underneath it - in both directions: it said
    "everything is free" while credits were live, then said crypto was "built
    and switched off" while it was on and answering.
    """
    try:
        from billing import x402_gate
        return x402_gate.enabled()
    except Exception:  # noqa: BLE001
        return False


def _handle_check_quota(token: str) -> dict:
    """
    check_quota handler — pure read, never consumes quota, never raises.

    Returns the caller's current quota state so an agent or human can inspect
    remaining ops without triggering a write tool first.  Three tiers:

      free        — email-verified free key (prefix 'free_'); daily write-tool cap
                    tracked by key_request_logic._free_key_daily.
      unlimited   — subscription token (principal_type 'system'/'business'/etc.);
                    scope.budget_cap_usd > 0 but no daily op cap.
      anonymous   — no valid token; no quota tracked server-side for write tools.

    DATA_METERING_ENABLED controls whether the premium-data-tool quota is
    included in the response.  When false (default prod state) that block is
    omitted entirely — it would be misleading to show a quota for tools running
    free and unmetered.
    """
    from datetime import datetime, timezone, timedelta

    now_utc = datetime.now(timezone.utc)
    midnight_utc = (now_utc + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    resets_str = midnight_utc.strftime("%Y-%m-%dT00:00:00Z")

    # Anonymous / missing token.
    if not token or token in ("", "anonymous"):
        return {
            "tier": "anonymous",
            "daily_limit": None,
            "used_today": 0,
            "remaining_today": None,
            "resets": resets_str,
            "key_id": None,
        }

    try:
        from agent_interface.identity import validate_token
        from agent_interface.key_request_logic import (
            is_free_key, get_free_daily_remaining,
            FREE_TIER_DAILY_LIMIT as _free_limit,
        )

        result = validate_token(token)
        if not (result and result.valid and result.identity):
            # Invalid/expired token — treat as anonymous.
            return {
                "tier": "anonymous",
                "daily_limit": None,
                "used_today": 0,
                "remaining_today": None,
                "resets": resets_str,
                "key_id": None,
            }

        aid = result.identity.agent_id

        if is_free_key(aid):
            remaining = get_free_daily_remaining(aid)
            used = _free_limit - remaining
            out: dict = {
                "tier": "free",
                "daily_limit": _free_limit,
                "used_today": used,
                "remaining_today": remaining,
                "resets": resets_str,
                "key_id": aid,
            }
        else:
            # Subscription / paid token — no per-day op cap on write tools.
            out = {
                "tier": "unlimited",
                "daily_limit": None,
                "used_today": 0,
                "remaining_today": None,
                "resets": resets_str,
                "key_id": aid,
            }

        # Premium data quota block — only present when DATA_METERING_ENABLED=true.
        import os as _os_dq
        if _os_dq.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes"):
            try:
                from billing.data_quota import (
                    _is_free_tier_key, get_free_key_data_remaining, _get_free_limit,
                )
                if _is_free_tier_key(aid):
                    data_limit = _get_free_limit()
                    data_remaining = get_free_key_data_remaining(aid)
                    out["data_quota"] = {
                        "daily_limit": data_limit,
                        "remaining_today": data_remaining,
                        "resets": resets_str,
                    }
            except Exception:  # noqa: BLE001
                pass  # data quota block is optional; never break the response

        return out

    except Exception:  # noqa: BLE001
        # Any unexpected failure: return anonymous shape so check_quota never breaks.
        return {
            "tier": "anonymous",
            "daily_limit": None,
            "used_today": 0,
            "remaining_today": None,
            "resets": resets_str,
            "key_id": None,
        }


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
            price_band=args.get("price_band"),
            availability_window=args.get("availability_window"),
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
        # EVERY FIELD, NOT THE ONES SOMEONE REMEMBERED.
        #
        # This constructor listed four of the seven parameters the manifest
        # advertises, so `requested_time`, `customer` and `notes` reached the
        # handler as None on every MCP call. requested_time is the time being
        # booked: an agent asked for 2pm Tuesday, the field was dropped here,
        # and the handler - which reads it in eight places - proceeded as
        # though no time had been given. The parameter was documented,
        # validated by the model, used by the handler, and never delivered.
        req = ScheduleAppointmentRequest(
            smb_id=args["smb_id"],
            action=AppointmentAction(args.get("action", "book")),
            service=args.get("service"),
            existing_appointment_id=args.get("existing_appointment_id"),
            customer=args.get("customer"),
            requested_time=args.get("requested_time"),
            notes=args.get("notes"),
        )
        receipt = await handle_schedule_appointment(req)

    elif name == "capture_lead":
        from core.capture_lead import handle_capture_lead
        from core.models import CaptureLeadRequest, ProspectData
        prospect_data = args.get("prospect", {})
        req = CaptureLeadRequest(
            smb_id=args["smb_id"],
            prospect=ProspectData(**_as_dict(prospect_data, "prospect")),
            source=args.get("source", "agent"),
        )
        # PASS THE CALLER'S IDENTITY. capture_lead now performs a REAL durable
        # write to the `leads` table, and the row carries agent_id — which was
        # NULL on every MCP-dispatched lead because this call site dropped it
        # (the dispatch-layer blindspot this codebase has hit before: the
        # handler advertises a parameter, the dispatcher never supplies it).
        # Without it the SMB cannot tell which agent sent a prospect.
        receipt = await handle_capture_lead(
            req,
            agent_id=_agent_id_from_token((headers or {}).get("x-agent-identity", "")),
        )

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
        # THE PUBLISHED SCHEMA DECLARED SIX FIELDS AND THIS RETURNED NONE OF
        # THEM. It advertised healthy, capabilities_verified, version,
        # supply_network_size, channel_status and degraded_channels; an agent
        # reading tools/list and branching on `r["healthy"]` got a KeyError,
        # and one checking degraded_channels to route around an SMS outage got
        # nothing at all. Four of those names appeared nowhere in the codebase
        # except the manifest.
        #
        # The schema now describes this shape - and `healthy` is included,
        # because it is the field an agent actually branches on and it is
        # derivable rather than invented. The ones we cannot compute honestly
        # are gone from the schema rather than faked here.
        from agent_interface.manifest_server import get_manifest_version
        out = {
            "healthy": report.all_passed,
            "all_passed": report.all_passed,
            "passed": report.passed_checks,
            "failed": report.failed_checks,
            "total": report.total_checks,
            "latency_ms": report.latency_ms,
        }
        try:
            _v = get_manifest_version()
            # get_manifest_version returns a DICT. The schema says string, and
            # an agent reading version as a string would get an object - the
            # same shape mismatch this block was written to remove.
            out["version"] = _v.get("version") if isinstance(_v, dict) else str(_v)
        except Exception:                       # noqa: BLE001
            pass
        failed = [c.name for c in (report.checks or []) if not c.passed]
        if failed:
            out["failed_checks"] = failed
        return out

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
                id_type=_as_enum(RecipientIdType, legacy_type, "recipient_type"),
                id_value=args.get("recipient_id", ""),
                country_code=args.get("country_code"),
            )
        content = args.get("content") or args.get("message") or {}
        if not isinstance(content, dict):
            content = {"body": str(content)}
        req = SendMessageRequest(
            recipient=recipient,
            message_type=_as_enum(MessageType,
                                  args.get("message_type") or "transactional",
                                  "message_type"),
            content=MessageContent(**_as_dict(content, "content")),
            preferred_channel=_as_enum(
                ChannelPreference,
                args.get("preferred_channel")
                or args.get("channel_preference")
                or "auto",
                "preferred_channel"),
            # on_behalf_of is WHO THE MESSAGE SAYS IT IS FROM - the handler
            # reads it in four places to build the sender disclosure a
            # transactional message is required to carry. It was advertised,
            # modelled, used, and dropped here.
            on_behalf_of=args.get("on_behalf_of"),
            business_id=args.get("business_id"),
            send_at_iso=args.get("send_at_iso"),
        )
        receipt = await handle_send_message(req)

    elif name == "send_transactional_confirmation":
        from core.send_transactional_confirmation import handle_send_transactional_confirmation
        from core.models import SendTransactionalConfirmationRequest
        req = SendTransactionalConfirmationRequest(**_as_dict(args, "arguments"))
        receipt = await handle_send_transactional_confirmation(req)

    elif name == "handle_inbound":
        from core.handle_inbound import handle_inbound as _handle_inbound
        from core.models import HandleInboundRequest
        req = HandleInboundRequest(**_as_dict(args, "arguments"))
        receipt = await _handle_inbound(req)

    elif name == "escalate_to_human":
        from core.escalate_to_human import handle_escalate_to_human
        from core.models import EscalateToHumanRequest
        req = EscalateToHumanRequest(**_as_dict(args, "arguments"))
        receipt = await handle_escalate_to_human(req)

    elif name == "import_booking_url":
        # The differentiator. Turns any public booking URL into a callable smb_id.
        from supply.booking_page_importer import import_from_booking_url, ImportRequest
        from core.models import Vertical
        vertical = None
        if args.get("vertical"):
            try:
                vertical = _as_enum(Vertical, args["vertical"], "vertical")
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
        # Free read-only sanctions screening: OFAC SDN, the EU Consolidated
        # list and the UK Sanctions List. No UN list, no API key.
        from core.screen_sanctions import handle_screen_sanctions
        receipt = await handle_screen_sanctions(
            name=args["name"],
            country=args.get("country"),
            entity_type=args.get("type"),
        )

    elif name == "map_trade_restriction":
        # Free read-only cross-border trade-compliance snapshot: OFAC embargo
        # map + party sanctions screening (OFAC SDN, EU, UK) + tariff
        # guidance links.  Never fabricates a rate or a clear.
        from core.map_trade_restriction import handle_map_trade_restriction
        receipt = await handle_map_trade_restriction(
            product=args["product"],
            destination_country=args["destination_country"],
            hs_code=args.get("hs_code"),
            origin_country=args.get("origin_country"),
            parties=args.get("parties"),
        )

    elif name == "lookup_us_contracts":
        # Free read-only US federal contract award lookup via USASpending.gov.
        # Probe A demand probe: "us import data api", "supplier lookup api",
        # "who has government contracts", "federal contractor search".
        from core.lookup_us_contracts import handle_lookup_us_contracts
        receipt = await handle_lookup_us_contracts(
            company_name=args["company_name"],
            max_results=int(args.get("max_results", 5)),
        )

    elif name == "check_quota":
        # Read-only quota visibility: returns the caller's current quota state
        # without consuming any ops. Free, no key required.
        # The token is drawn from headers (same path as _inject_quota_block) so
        # the result always reflects the actual caller even when REQUIRE_AUTH is off.
        _cq_token = (headers or {}).get("x-agent-identity", "")
        if not _cq_token:
            _cq_auth = (headers or {}).get("authorization", "")
            if _cq_auth.lower().startswith("bearer "):
                _cq_token = _cq_auth[7:].strip()
        _cq_result = _handle_check_quota(_cq_token)
        # Credit-balance visibility: a funded (non-free) credit account must be
        # able to see its remaining credits HERE, not only incidentally on a
        # paid-tool receipt. This is a read-only SELECT (billing.credits.get_balance
        # is fail-open and never writes), so it can NEVER turn check_quota into a
        # charged or failing call: any lookup error is swallowed and the base
        # quota response is returned unchanged. key_id is the same account_id the
        # paid path bills (resolve_account -> identity.agent_id).
        try:
            _cq_key = _cq_result.get("key_id")
            if _cq_key and _cq_result.get("tier") != "free":
                from billing.credits import (
                    get_balance as _cq_get_balance,
                    is_free_key as _cq_is_free,
                )
                if not _cq_is_free(_cq_key):
                    _cq_balance = await _cq_get_balance(_cq_key)
                    # None == no credit_accounts row (e.g. a pure subscription
                    # token) or a lookup error -> omit rather than show a
                    # misleading zero. A genuine spent-to-zero account returns
                    # int 0 and is shown honestly.
                    if _cq_balance is not None:
                        _cq_result["credit_balance"] = _cq_balance
                        # 1 credit == 1 US cent (billing.pricing single source of truth).
                        _cq_result["credit_balance_usd"] = round(_cq_balance / 100.0, 2)
        except Exception:  # noqa: BLE001 - balance visibility is best-effort; never break check_quota
            pass
        return _cq_result

    elif name == "mint_key":
        # Agent self-serve key issuance: HMAC-SHA256 proof of MACHINE_MINT_SECRET.
        # Returns the issued key directly.  503 until MACHINE_MINT_SECRET is set.
        from agent_interface.key_request_logic import handle_mint_key_mcp
        receipt = await handle_mint_key_mcp(
            agent_id=args.get("agent_id", ""),
            timestamp=args.get("timestamp", 0),
            nonce=args.get("nonce", ""),
            signature=args.get("signature", ""),
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

    # FEED THE NUMBER WE PUBLISH.
    #
    # preview_cost returns success_probability_estimate to three significant
    # figures, and docs/AGENT_INTEGRATION_GUIDE.md tells agents to ABORT below
    # 0.5. That number was a hardcoded constant. The machinery to measure it
    # already existed in telemetry/metrics_emitter.py - MetricCounters.record()
    # - and was called from nowhere in production: a producer with no caller
    # sitting directly behind a number customers make spend decisions on.
    #
    # This is the caller. One place, so every tool is counted.
    try:
        from telemetry.metrics_emitter import get_metrics
        get_metrics().record(
            operation=name,
            success=_success_bool,
            latency_ms=int(getattr(receipt, "latency_ms", 0) or 0),
            cost_usd=_amount,
        )
    except Exception:
        pass  # telemetry must never break tool dispatch

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
        "screen_sanctions", "map_trade_restriction", "lookup_us_contracts",
        "check_quota",  # read-only — never writes a durable row
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
                    "across 26 jurisdictions). Cold outreach, drip campaigns, bulk lists, "
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
