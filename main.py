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
from pydantic import BaseModel, Field

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
# Telemetry middleware — single source of truth for request/op counters.
# Increments are O(1) atomic-ish; no DB round-trip. Memory-only on free tier
# is fine: counters reset on deploy and that's documented on the home page.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _telemetry_counter_middleware(request: Request, call_next):
    path = request.url.path
    is_op = path.startswith("/ops/") or path == "/mcp"
    if is_op:
        from telemetry.metrics import record_agent_request
        record_agent_request()
    response = await call_next(request)
    if is_op and 200 <= response.status_code < 300:
        from telemetry.metrics import record_operation_completed
        record_operation_completed()
    return response


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


@app.get("/healthz/external", tags=["Discovery"])
async def health_external():
    """
    Deep health check — pings every upstream we depend on (Twilio, Cal.com,
    Vapi, Resend, Paddle) plus internal discovery surfaces (manifest, MCP,
    llms.txt). Returns per-service status with latency and any low-balance
    warnings. Cheap (~ 5 s parallel) and safe to expose publicly: no key
    or sensitive data leaks into the response.
    """
    from agent_interface.health_external import run_external_health
    report = await run_external_health()
    status_code = 200 if report["status"] == "ok" else (200 if report["status"] == "degraded" else 503)
    return JSONResponse(
        content=report,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


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


# ---------------------------------------------------------------------------
# Public, free, standalone compliance API
# ---------------------------------------------------------------------------
# Different from /ops/* — this endpoint is intentionally NOT gated by
# X-Agent-Identity and not metered. The compliance gate is valuable
# **standalone** for any developer sending SMS/email/voice through any
# carrier (not just our broker). By exposing it as a free public API we
# (a) become useful to devs who never plan to use our broker,
# (b) build SEO around "TCPA / GDPR / CASL compliance check API",
# (c) get traffic that demonstrates the rest of our surface exists.
#
# This is a deliberate distribution wedge — a compliance pre-check is
# something every messaging-app developer needs and nobody currently
# sells as a free API.

class _ComplianceCheckRequest(BaseModel):
    recipient_id: str = Field(..., description="Phone (E.164) or email of recipient.")
    channel: str = Field(..., description="One of: sms, email, voice.")
    message_type: str = Field(
        "transactional",
        description="One of: marketing, transactional, reminder, opt-in-confirm, customer-service.",
    )
    content: str = Field(..., description="The message body the agent is about to send.")
    country_code: Optional[str] = Field(
        None, description="ISO 3166-1 alpha-2 country code (e.g. 'US', 'DE'). Auto-inferred from phone if omitted."
    )
    state_code: Optional[str] = Field(
        None, description="US state code (e.g. 'CA') for state-specific rules."
    )


@app.post("/compliance/check", tags=["Public APIs"])
async def compliance_check_public(req: _ComplianceCheckRequest):
    """Free, public, no-auth compliance pre-check API.

    Returns whether sending the supplied (recipient, channel, message_type, content)
    combination would comply with TCPA / GDPR / CASL / 10DLC and 22 country-specific
    rule sets. Drop-in for any messaging stack — no need to use our broker.

    Use cases:
      - Pre-flight check before calling Twilio/Vonage/Plivo directly
      - Audit a marketing list before bulk send
      - Dashboards / lint rules in messaging app pipelines

    Free forever. Rate-limited to ~60 req/min per IP.
    """
    from compliance.pre_check import pre_check
    from core.models import ComplianceViolationError
    try:
        pre_check(
            recipient_id=req.recipient_id,
            channel=req.channel,
            message_type=req.message_type,
            content=req.content,
            country_code=req.country_code,
            state_code=req.state_code,
        )
        return {
            "legal": True,
            "rule_set": req.country_code or "international",
            "channel": req.channel,
            "message_type": req.message_type,
            "notes": "Pre-check passed. Send is permitted under the supplied jurisdiction.",
        }
    except ComplianceViolationError as cve:
        return JSONResponse(
            status_code=200,
            content={
                "legal": False,
                "rule": cve.rule,
                "rule_set": cve.jurisdiction,
                "channel": cve.channel,
                "message": cve.message,
                "remediation": _remediation_for(cve.rule),
            },
        )


def _remediation_for(rule: str) -> str:
    return {
        "restricted_content": "Reword the message to remove the restricted category, or seek explicit licensing for the regulated content.",
        "recipient_opted_out": "Honor the opt-out — the recipient has unsubscribed. Do not send. Add to suppression list.",
        "TCPA_marketing_consent": "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers.",
        "GDPR_marketing_consent": "Obtain GDPR Article 6/7 consent before marketing email to EU/UK residents.",
        "CASL_marketing_consent": "Obtain explicit CASL consent before commercial electronic messages to Canadian recipients.",
        "10DLC_unregistered": "Register your sending number under a 10DLC campaign with The Campaign Registry before sending US A2P SMS.",
        "10DLC_campaign_not_registered": "Register a 10DLC campaign with The Campaign Registry (TCR) before sending US A2P SMS. Required by US carriers since 2023.",
    }.get(rule, "Review the cited rule in our jurisdiction reference at /docs.")


@app.get("/supply/platforms", tags=["Public APIs"])
async def supply_platforms():
    """Public catalogue of booking platforms `import_booking_url` understands.

    No auth, no rate limit. The intended caller is an agent that just got a
    `no_results` from `find_business` and needs to know what URL formats to
    accept from its end-user. Each entry includes a regex pattern + an example.
    """
    return {
        "platforms": [
            {"name": "Cal.com",         "pattern": r"https://cal.com/<handle>",                                  "example": "https://cal.com/peer"},
            {"name": "Calendly",        "pattern": r"https://calendly.com/<handle>/<slug>",                       "example": "https://calendly.com/acme/intro"},
            {"name": "Doctolib",        "pattern": r"https://www.doctolib.{fr,de,it}/<specialty>/<city>/<doctor>","example": "https://www.doctolib.fr/dentiste/paris/jean-dupont"},
            {"name": "Booksy",          "pattern": r"https://booksy.com/en-us/<id>_<slug>",                       "example": "https://booksy.com/en-us/123_jane-salon"},
            {"name": "Fresha",          "pattern": r"https://fresha.com/<slug>",                                  "example": "https://fresha.com/a/jane-salon-london"},
            {"name": "OpenTable",       "pattern": r"https://www.opentable.com/r/<slug>",                         "example": "https://www.opentable.com/r/acme-bistro-tokyo"},
            {"name": "Setmore",         "pattern": r"https://setmore.com/<slug>",                                 "example": "https://setmore.com/jane-salon"},
            {"name": "Square",          "pattern": r"https://squareup.com/appointments/book/<id> or https://<name>.square.site", "example": "https://jane.square.site"},
            {"name": "Acuity",          "pattern": r"https://app.acuityscheduling.com/schedule.php?owner=<id>",   "example": "https://app.acuityscheduling.com/schedule.php?owner=12345"},
            {"name": "Schedulista",     "pattern": r"https://www.schedulista.com/<slug>",                         "example": "https://www.schedulista.com/jane-salon"},
            {"name": "Squarespace",     "pattern": r"https://<custom>.squarespace-scheduling.com",                "example": "https://jane.squarespace-scheduling.com"},
            {"name": "BookMyCity",      "pattern": r"https://bookmycity.com/<slug>",                              "example": "https://bookmycity.com/jane-salon"},
        ],
        "import_endpoint": "/supply/import_booking_url",
        "next_step": (
            "Pick a URL the user mentioned, POST it to /supply/import_booking_url, "
            "then the directory is populated and find_business returns the business. "
            "The schedule_appointment call works against the imported entry."
        ),
    }


@app.get("/demo", tags=["Public APIs"])
async def demo():
    """Live, no-auth, no-input demonstration of the full flow.

    Imports a public Cal.com showcase URL, then immediately calls find_business
    against the just-imported entry. Lets a curious developer confirm the
    contract works in 1 second without writing any code or providing any input.
    """
    from supply.booking_page_importer import import_from_booking_url, ImportRequest
    from core.find_business import handle_find_business
    from core.models import FindBusinessRequest, Vertical, LocationFilter
    import time

    t0 = time.monotonic()

    demo_url = "https://cal.com/peer"  # Cal.com's longstanding public showcase handle
    imp = await import_from_booking_url(ImportRequest(
        booking_url=demo_url,
        business_name="Cal.com Demo (Peer)",
        vertical=Vertical.PROFESSIONAL_SERVICES,
        country_code="US",
    ))

    found = await handle_find_business(FindBusinessRequest(
        vertical=Vertical.PROFESSIONAL_SERVICES,
        location=LocationFilter(zip_or_city="online"),
        capability=None,
        max_results=5,
    ))

    return {
        "ok": True,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "step_1_import": {
            "input_url": demo_url,
            "platform_detected": imp.platform.value if imp.platform else None,
            "smb_id": imp.smb_id,
            "status": imp.status.value,
        },
        "step_2_find_business": {
            "matches": len(found.result.get("businesses", [])) if isinstance(found.result, dict) else 0,
            "total_in_directory": found.result.get("total_in_supply_network") if isinstance(found.result, dict) else None,
        },
        "next": (
            "Now POST to /ops/schedule_appointment with the smb_id from step 1 to "
            "complete the booking flow. Or call /mcp tools/list to see all 12 tools."
        ),
        "try_yourself": {
            "mcp_endpoint": "POST https://smb-broker.onrender.com/mcp",
            "import_endpoint": "POST https://smb-broker.onrender.com/supply/import_booking_url",
            "find_endpoint": "POST https://smb-broker.onrender.com/ops/find_business",
        },
    }


@app.get("/compliance/jurisdictions", tags=["Public APIs"])
async def compliance_jurisdictions():
    """Public list of jurisdictions with native compliance rules.
    No auth, free. Useful for any compliance-aware sender."""
    from compliance.jurisdiction_rules import list_supported_jurisdictions
    return {
        "supported": list_supported_jurisdictions(),
        "fallback": "international",
        "note": "Unknown jurisdictions fall back to a conservative INTERNATIONAL rule set.",
    }


@app.get("/openapi.yaml", response_class=PlainTextResponse, tags=["Discovery"], include_in_schema=False)
async def openapi_yaml():
    """Hand-curated OpenAPI 3.1 spec served as YAML.

    apis.guru's bot, RapidAPI, and several MCP-discovery crawlers prefer
    `application/x-yaml` over the auto-generated `/openapi.json`. We host
    both side-by-side; the YAML is the "marketing" copy with examples.
    """
    from pathlib import Path
    spec_path = Path(__file__).parent / "manifest" / "openapi.yaml"
    return PlainTextResponse(content=spec_path.read_text(encoding="utf-8"), media_type="application/x-yaml")


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
# Web UI — server-rendered HTML, no client framework
# ---------------------------------------------------------------------------
# Caching note: every page is deterministic per deploy. The CDN / browser
# can hold them for 5 minutes. /api/metrics is a tiny JSON polled every 8 s
# from the home page; we mark it no-store so the live counters never look stale.

from fastapi.responses import HTMLResponse, RedirectResponse
from web.pages import (
    render_home, render_pricing, render_terms, render_privacy,
    render_refund, render_checkout,
)

_PAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=300, s-maxage=300"}


@app.get("/", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_home():
    return HTMLResponse(content=render_home(), headers=_PAGE_CACHE_HEADERS)


@app.get("/pricing", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_pricing():
    return HTMLResponse(content=render_pricing(), headers=_PAGE_CACHE_HEADERS)


@app.get("/terms", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_terms():
    return HTMLResponse(content=render_terms(), headers=_PAGE_CACHE_HEADERS)


@app.get("/privacy", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_privacy():
    return HTMLResponse(content=render_privacy(), headers=_PAGE_CACHE_HEADERS)


@app.get("/refund", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_refund():
    return HTMLResponse(content=render_refund(), headers=_PAGE_CACHE_HEADERS)


@app.get("/checkout", response_class=HTMLResponse, tags=["Web UI"], include_in_schema=False)
async def web_checkout(plan: str | None = None):
    return HTMLResponse(content=render_checkout(plan), headers=_PAGE_CACHE_HEADERS)


# Legacy URL aliases (the old React-SPA hash links). Permanent redirects so
# any external link or AI-generated citation still resolves.
@app.get("/dashboard", include_in_schema=False)
async def web_dashboard_alias():
    return RedirectResponse(url="/", status_code=301)


@app.get("/terms-of-service", include_in_schema=False)
async def web_terms_alias():
    return RedirectResponse(url="/terms", status_code=301)


@app.get("/privacy-policy", include_in_schema=False)
async def web_privacy_alias():
    return RedirectResponse(url="/privacy", status_code=301)


@app.get("/refund-policy", include_in_schema=False)
async def web_refund_alias():
    return RedirectResponse(url="/refund", status_code=301)


@app.get("/api/metrics", tags=["Metrics"])
async def get_metrics():
    """Public counters consumed by the home-page live tiles. No PII."""
    from telemetry.metrics import get_telemetry_summary
    try:
        summary = await get_telemetry_summary()
    except Exception:
        summary = {}
    payload = {
        "total_agents_requested": summary.get("total_agents_requested", 0),
        "total_businesses_found": summary.get("total_businesses_found", 0),
        "total_messages_sent": summary.get("total_messages_sent", 0),
        "total_operations_completed": summary.get("total_operations_completed", 0),
        "last_updated": summary.get("last_updated"),
    }
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


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
