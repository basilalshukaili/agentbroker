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

from agent_interface.manifest_server import get_full_manifest, get_operation


# ---------------------------------------------------------------------------
# MCP server metadata
# ---------------------------------------------------------------------------

SERVER_NAME = "smb-broker"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Tool registry — all operations exposed as MCP tools
# ---------------------------------------------------------------------------

def _build_tool_list() -> list[dict]:
    """Convert manifest operations to MCP tool descriptors."""
    manifest = get_full_manifest()
    tools = []
    for op in manifest.get("operations", []):
        tool = {
            "name": op["name"],
            "description": _format_description_for_llm(op),
            "inputSchema": op.get("input_schema", {"type": "object"}),
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
        has_variable = any(
            k in cost for k in ("voice_premium_usd", "success_bonus_usd", "tiers")
        )
        if cost_basis == "free":
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
        return JsonRpcResponse(id=rpc_id, result=result).to_dict()
    except _ParamError as pe:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INVALID_PARAMS, str(pe)),
        ).to_dict()
    except Exception as exc:
        return JsonRpcResponse(
            id=rpc_id,
            error=_error(ERR_INTERNAL, f"Internal error: {exc}"),
        ).to_dict()


class _ParamError(ValueError):
    pass


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
    name = params.get("name")
    arguments = params.get("arguments", {}) or {}
    if not name:
        raise _ParamError("Missing 'name' parameter")

    op = get_operation(name)
    if not op:
        raise _ParamError(f"Unknown tool: '{name}'")

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

    receipt = await _dispatch_operation(name, arguments, headers or {})
    return {
        "content": [
            {"type": "text", "text": json.dumps(receipt, indent=2, default=str)}
        ],
        "isError": receipt.get("status") == "failure",
    }


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
    # Late import to avoid a hard cycle between main.py and mcp_server.py.
    from main import _get_identity
    from fastapi import HTTPException

    token = headers.get("x-agent-identity") if headers else None
    try:
        _get_identity(token, name)
    except HTTPException as he:
        # 401 unauthenticated or 403 insufficient scope — surface as a clear,
        # actionable JSON-RPC params error rather than a generic -32603.
        raise _ParamError(
            f"auth_required for tool '{name}' (status={he.status_code}): {he.detail}"
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

    elif name == "get_status":
        from core.status_outcome import handle_get_status
        return await handle_get_status(args["operation_id"])

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

    else:
        raise _ParamError(f"Tool '{name}' is registered but not yet routed in MCP dispatcher.")

    # Convert OutcomeReceipt → dict
    if hasattr(receipt, "model_dump"):
        return receipt.model_dump()
    return dict(receipt) if isinstance(receipt, dict) else receipt.__dict__


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
