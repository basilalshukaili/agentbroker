"""
SMB Transaction & Communication Broker — FastAPI entry point.

Routes map directly to the 12 manifest operations.
Auth: X-Agent-Identity header (required for state-changing ops in production).
All responses use OutcomeReceipt schema.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import config
from core.models import (
    OutcomeReceipt, OperationStatus, ErrorCode,
    FindBusinessRequest, VerifyBusinessRequest,
    SendMessageRequest, CaptureLeadRequest,
    ScheduleAppointmentRequest, SendTransactionalConfirmationRequest,
    HandleInboundRequest, EscalateToHumanRequest,
    PreviewCostRequest, WebhookRegistrationRequest,
)
from agent_interface.manifest_server import get_full_manifest, get_operations_list, get_manifest_version
from agent_interface.discovery import get_discovery_card, health_check
from agent_interface.identity import validate_token, check_operation_allowed
from agent_interface.self_test import run_self_test
from agent_interface.mcp_server import handle_mcp_request
from agent_interface.well_known import (
    get_ai_plugin_manifest, get_openai_tools, get_anthropic_tools,
    get_agents_json, get_mcp_descriptor, get_llms_txt, get_llms_full_txt,
)
from fastapi.responses import PlainTextResponse


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate config on startup
    if config.ENVIRONMENT == "production":
        warnings = config.validate_production_config()
        for w in warnings:
            import logging
            logging.getLogger("smb_broker").warning(w)
    yield


app = FastAPI(
    title="SMB Transaction & Communication Broker",
    description=(
        "Agent-callable service for discovering, verifying, communicating with, "
        "scheduling, and transacting with small/mid businesses."
    ),
    version=config.SERVICE_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _get_identity(token: Optional[str], operation: str):
    """Validate agent identity token. Returns identity or raises 401/403."""
    if not config.REQUIRE_AUTH:
        return None  # Auth disabled in development
    if not token:
        raise HTTPException(status_code=401, detail="X-Agent-Identity header required.")
    result = validate_token(token)
    if not result.valid:
        raise HTTPException(status_code=401, detail=result.error)
    if not check_operation_allowed(result.identity, operation):
        raise HTTPException(
            status_code=403,
            detail=f"Agent scope does not include operation '{operation}'.",
        )
    return result.identity


# ---------------------------------------------------------------------------
# Discovery & health
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent-service", tags=["Discovery"])
async def discovery_card(x_agent_identity: Optional[str] = Header(None)):
    agent_id = None
    if x_agent_identity:
        res = validate_token(x_agent_identity)
        if res.valid:
            agent_id = res.identity.agent_id
    return get_discovery_card(agent_id)


@app.get("/health", tags=["Discovery"])
async def health():
    return health_check()


# ---------------------------------------------------------------------------
# MCP server — JSON-RPC 2.0 endpoint
# ---------------------------------------------------------------------------

@app.post("/mcp", tags=["MCP"])
async def mcp_endpoint(request: Request):
    """Model Context Protocol JSON-RPC 2.0 endpoint."""
    payload = await request.json()
    response = await handle_mcp_request(payload)
    return response


# ---------------------------------------------------------------------------
# Multi-protocol discovery — .well-known endpoints
# ---------------------------------------------------------------------------

@app.get("/.well-known/ai-plugin.json", tags=["Discovery"])
async def well_known_ai_plugin():
    """ChatGPT / OpenAI plugin manifest."""
    return get_ai_plugin_manifest()


@app.get("/.well-known/openai-tools.json", tags=["Discovery"])
async def well_known_openai_tools():
    """OpenAI function-calling tools array."""
    return get_openai_tools()


@app.get("/.well-known/anthropic-tools.json", tags=["Discovery"])
async def well_known_anthropic_tools():
    """Claude tool_use array format."""
    return get_anthropic_tools()


@app.get("/.well-known/agents.json", tags=["Discovery"])
async def well_known_agents():
    """A2A (Agent-to-Agent) protocol descriptor."""
    return get_agents_json()


@app.get("/.well-known/mcp.json", tags=["Discovery"])
async def well_known_mcp():
    """MCP server descriptor pointing at /mcp."""
    return get_mcp_descriptor()


@app.get("/llms.txt", response_class=PlainTextResponse, tags=["Discovery"])
async def llms_txt():
    """LLM-readable site map (https://llmstxt.org/)."""
    return get_llms_txt()


@app.get("/llms-full.txt", response_class=PlainTextResponse, tags=["Discovery"])
async def llms_full_txt():
    """Full content dump for LLM training crawlers."""
    return get_llms_full_txt()


# ---------------------------------------------------------------------------
# Public booking-page importer — the strategic moat
# ---------------------------------------------------------------------------

@app.post("/supply/import_booking_url", tags=["Supply"])
async def supply_import_booking_url(
    payload: dict,
    x_agent_identity: Optional[str] = Header(None),
):
    """Idempotent ingest: given any booking URL, classify the platform and add SMB."""
    from supply.booking_page_importer import import_from_booking_url, ImportRequest
    from core.models import Vertical
    vertical = None
    if payload.get("vertical"):
        try:
            vertical = Vertical(payload["vertical"])
        except Exception:
            vertical = None
    req = ImportRequest(
        booking_url=payload["booking_url"],
        business_name=payload.get("business_name"),
        vertical=vertical,
        country_code=payload.get("country_code"),
        contact_phone=payload.get("contact_phone"),
        contact_email=payload.get("contact_email"),
        capabilities=payload.get("capabilities", []),
    )
    result = await import_from_booking_url(req)
    return {
        "status": result.status.value,
        "smb_id": result.smb_id,
        "platform": result.platform.value if result.platform else None,
        "message": result.message,
        "next_steps": result.next_steps,
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

@app.get("/manifest", tags=["Manifest"])
async def manifest(x_agent_identity: Optional[str] = Header(None)):
    agent_id = None
    if x_agent_identity:
        res = validate_token(x_agent_identity)
        if res.valid:
            agent_id = res.identity.agent_id
    return get_full_manifest(agent_id)


@app.get("/manifest/ops", tags=["Manifest"])
async def manifest_ops(x_agent_identity: Optional[str] = Header(None)):
    agent_id = None
    if x_agent_identity:
        res = validate_token(x_agent_identity)
        if res.valid:
            agent_id = res.identity.agent_id
    return {"operations": get_operations_list(agent_id)}


@app.get("/manifest/version", tags=["Manifest"])
async def manifest_version():
    return get_manifest_version()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@app.post("/ops/find_business", response_model=OutcomeReceipt, tags=["Operations"])
async def find_business(
    req: FindBusinessRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "find_business")
    from core.find_business import handle_find_business
    return await handle_find_business(req)


@app.post("/ops/verify_business", response_model=OutcomeReceipt, tags=["Operations"])
async def verify_business(
    req: VerifyBusinessRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "verify_business")
    from core.verify_business import handle_verify_business
    return await handle_verify_business(req)


@app.post("/ops/send_message", response_model=OutcomeReceipt, tags=["Operations"])
async def send_message(
    req: SendMessageRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "send_message")
    from core.send_message import handle_send_message
    return await handle_send_message(req)


@app.post("/ops/capture_lead", response_model=OutcomeReceipt, tags=["Operations"])
async def capture_lead(
    req: CaptureLeadRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "capture_lead")
    from core.capture_lead import handle_capture_lead
    return await handle_capture_lead(req)


@app.post("/ops/schedule_appointment", response_model=OutcomeReceipt, tags=["Operations"])
async def schedule_appointment(
    req: ScheduleAppointmentRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "schedule_appointment")
    from core.schedule_appointment import handle_schedule_appointment
    return await handle_schedule_appointment(req)


@app.post("/ops/send_transactional_confirmation", response_model=OutcomeReceipt, tags=["Operations"])
async def send_transactional_confirmation(
    req: SendTransactionalConfirmationRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "send_transactional_confirmation")
    from core.send_transactional_confirmation import handle_send_transactional_confirmation
    return await handle_send_transactional_confirmation(req)


@app.post("/ops/handle_inbound", response_model=OutcomeReceipt, tags=["Operations"])
async def handle_inbound(
    req: HandleInboundRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "handle_inbound")
    from core.handle_inbound import handle_inbound as _handle_inbound
    return await _handle_inbound(req)


@app.post("/ops/escalate_to_human", response_model=OutcomeReceipt, tags=["Operations"])
async def escalate_to_human(
    req: EscalateToHumanRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "escalate_to_human")
    from core.escalate_to_human import handle_escalate_to_human
    return await handle_escalate_to_human(req)


@app.get("/ops/get_status/{operation_id}", response_model=dict, tags=["Operations"])
async def get_status(
    operation_id: str,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "get_status")
    from core.status_outcome import handle_get_status
    return await handle_get_status(operation_id)


@app.get("/ops/get_outcome/{operation_id}", response_model=OutcomeReceipt, tags=["Operations"])
async def get_outcome(
    operation_id: str,
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "get_outcome")
    from core.status_outcome import handle_get_outcome
    return await handle_get_outcome(operation_id)


@app.post("/ops/preview_cost", tags=["Operations"])
async def preview_cost(
    req: PreviewCostRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    # preview_cost is explicitly free and read-only — no auth required
    from core.preview_cost import handle_preview_cost
    return await handle_preview_cost(req)


@app.post("/ops/self_test", tags=["Operations"])
async def self_test(
    x_agent_identity: Optional[str] = Header(None),
):
    _get_identity(x_agent_identity, "self_test")
    report = await run_self_test()
    return {
        "all_passed": report.all_passed,
        "passed": report.passed_checks,
        "failed": report.failed_checks,
        "total": report.total_checks,
        "latency_ms": report.latency_ms,
        "checks": [
            {"name": c.name, "passed": c.passed, "latency_ms": c.latency_ms,
             "error": c.error}
            for c in report.checks
        ],
    }


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@app.post("/webhooks/register", tags=["Webhooks"])
async def register_webhook(
    req: WebhookRegistrationRequest,
    x_agent_identity: Optional[str] = Header(None),
):
    identity = _get_identity(x_agent_identity, "send_message")  # any state-changing op
    agent_id = identity.agent_id if identity else "dev_agent"
    from agent_interface.webhooks import register
    result = register(req, agent_id)
    return {
        "webhook_id": result.webhook_id,
        "secret": result.secret,
        "message": result.message,
    }


@app.get("/webhooks", tags=["Webhooks"])
async def list_webhooks(
    x_agent_identity: Optional[str] = Header(None),
):
    identity = _get_identity(x_agent_identity, "send_message")
    agent_id = identity.agent_id if identity else "dev_agent"
    from agent_interface.webhooks import list_webhooks as _list
    return {"webhooks": _list(agent_id)}


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from core.models import ComplianceViolationError
    if isinstance(exc, ComplianceViolationError):
        err = exc.to_api_error()
        return JSONResponse(status_code=422, content=err.model_dump())
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=config.DEBUG,
        log_level=config.LOG_LEVEL.lower(),
    )
