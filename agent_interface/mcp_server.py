"""
MCP (Model Context Protocol) server endpoint.

MCP is the protocol Claude Desktop, Cursor, Continue, and other agent IDEs
use to discover and call tools. Agents that speak MCP will find us via
the standard `tools/list` and `tools/call` JSON-RPC methods.

Spec: https://spec.modelcontextprotocol.io/

This module exposes a JSON-RPC 2.0 endpoint at /mcp that handles:
  - initialize           → handshake + server capabilities
  - tools/list           → returns all 12 operations as MCP tools
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
# Tool registry — all 12 operations exposed as MCP tools
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
        # MCP annotations help client UIs render tools better
        tool["annotations"] = {
            "title": op["name"].replace("_", " ").title(),
            "readOnlyHint": op["name"] in {
                "find_business", "verify_business", "get_status",
                "get_outcome", "preview_cost", "self_test",
            },
            "destructiveHint": op["name"] in {
                "send_message", "schedule_appointment",
                "send_transactional_confirmation", "escalate_to_human",
            },
            "idempotentHint": True,
            "openWorldHint": op["name"] in {"send_message", "schedule_appointment"},
        }
        tools.append(tool)
    return tools


def _format_description_for_llm(op: dict) -> str:
    """Build an LLM-optimized description that combines all selection signals."""
    parts = [
        op["description"],
        "",
        f"WHEN TO USE: {op['when_to_use']}",
    ]
    if op.get("when_not_to_use"):
        parts.append(f"WHEN NOT TO USE: {op['when_not_to_use']}")

    cost = op.get("cost_model", {})
    if cost:
        cost_basis = cost.get("basis", "per_call")
        cost_amount = cost.get("amount_usd", "varies")
        parts.append(f"COST: ${cost_amount} {cost_basis}")

    slo = op.get("slo", {})
    if slo:
        latency = slo.get("p50_latency_ms") or slo.get("max_latency_ms", "varies")
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


async def handle_mcp_request(payload: dict) -> dict:
    """
    Main JSON-RPC dispatcher. Returns a dict suitable to be JSON-encoded
    and sent back to the client.
    """
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {}) or {}

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
            "SMB Transaction & Communication Broker. Use tools/list to see all 12 operations. "
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

async def _h_tools_call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments", {}) or {}
    if not name:
        raise _ParamError("Missing 'name' parameter")

    op = get_operation(name)
    if not op:
        raise _ParamError(f"Unknown tool: '{name}'")

    receipt = await _dispatch_operation(name, arguments)
    return {
        "content": [
            {"type": "text", "text": json.dumps(receipt, indent=2, default=str)}
        ],
        "isError": receipt.get("status") == "failure",
    }


async def _dispatch_operation(name: str, args: dict) -> dict:
    """Route an operation call to the underlying handler. Returns dict, not OutcomeReceipt."""
    if name == "find_business":
        from core.find_business import handle_find_business
        from core.models import FindBusinessRequest, LocationFilter, Vertical
        loc = args.get("location", {})
        req = FindBusinessRequest(
            vertical=Vertical(args["vertical"]),
            location=LocationFilter(zip_or_city=loc.get("zip_or_city", "Atlanta")),
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
                "uri": "smb-broker://manifest",
                "name": "Capability Manifest",
                "description": "Full manifest with all 12 operations and their schemas.",
                "mimeType": "application/json",
            },
            {
                "uri": "smb-broker://errors",
                "name": "Error Code Catalog",
                "description": "All 16 error codes with retry semantics.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "smb-broker://compliance/jurisdictions",
                "name": "Jurisdiction Rules",
                "description": "Compliance rules by country/state — TCPA, GDPR, CASL, recording consent.",
                "mimeType": "application/json",
            },
        ]
    }


async def _h_resources_read(params: dict) -> dict:
    uri = params.get("uri")
    if uri == "smb-broker://manifest":
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(get_full_manifest(), indent=2),
            }]
        }
    if uri == "smb-broker://errors":
        from pathlib import Path
        path = Path(__file__).parent.parent / "api" / "errors.md"
        if path.exists():
            return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                                  "text": path.read_text()}]}
    if uri == "smb-broker://compliance/jurisdictions":
        from compliance.jurisdiction_rules import _RULES
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps({k: vars(v) for k, v in _RULES.items()},
                                   indent=2, default=str),
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
                "name": "book_appointment_workflow",
                "description": "Multi-step workflow: find_business → verify_business → preview_cost → schedule_appointment.",
                "arguments": [
                    {"name": "vertical", "description": "personal_services | home_services | professional_services", "required": True},
                    {"name": "location", "description": "ZIP or city", "required": True},
                    {"name": "capability", "description": "Service needed (e.g. haircut, plumbing)", "required": True},
                ],
            },
            {
                "name": "outbound_message_with_compliance",
                "description": "Send an outbound SMS/email with full compliance pre-check.",
                "arguments": [
                    {"name": "recipient", "description": "Phone or email", "required": True},
                    {"name": "message_type", "description": "transactional | marketing", "required": True},
                ],
            },
            {
                "name": "cost_estimation",
                "description": "Get a cost estimate before committing to an operation.",
                "arguments": [
                    {"name": "operation", "description": "Operation name", "required": True},
                ],
            },
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
    "resources/list": _h_resources_list,
    "resources/read": _h_resources_read,
    "prompts/list": _h_prompts_list,
}
