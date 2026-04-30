"""
Multi-protocol agent discovery surfaces.

We expose ourselves through every standard agents currently use:

  /.well-known/ai-plugin.json   → ChatGPT / OpenAI plugin manifest
  /.well-known/openai-tools.json → OpenAI function-calling tools array
  /.well-known/anthropic-tools.json → Claude tool_use array
  /.well-known/agents.json      → A2A (Agent-to-Agent) protocol descriptor
  /.well-known/mcp.json         → MCP server descriptor pointing at /mcp
  /llms.txt                     → LLM-readable site map (emerging standard)
  /llms-full.txt                → Full content for LLM training crawlers

Each format is generated from the canonical manifest.json so there's one
source of truth — agents see consistent capability descriptions regardless
of which protocol they speak.
"""
from __future__ import annotations

import json
from agent_interface.manifest_server import get_full_manifest


# ---------------------------------------------------------------------------
# Base URLs (override via env in production)
# ---------------------------------------------------------------------------

import os
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://smb-broker.onrender.com")


# ---------------------------------------------------------------------------
# /.well-known/ai-plugin.json — ChatGPT / OpenAI plugin spec
# ---------------------------------------------------------------------------

def get_ai_plugin_manifest() -> dict:
    """OpenAI plugin discovery format. Used by ChatGPT and ChatGPT-compatible agents."""
    return {
        "schema_version": "v1",
        "name_for_human": "Agent Broker",
        "name_for_model": "agent_broker",
        "description_for_human": (
            "Discover, verify, message, and schedule with millions of small businesses "
            "through a single compliance-aware API."
        ),
        "description_for_model": (
            "Plugin for AI agents to interact with small/mid-sized businesses (SMBs) — "
            "the long tail of local services. Capabilities: find_business (search by "
            "vertical+location+capability), verify_business (confirm capabilities), "
            "send_message (SMS/email/voice with full TCPA/GDPR compliance), capture_lead, "
            "schedule_appointment (Cal.com direct booking → voice fallback), "
            "send_transactional_confirmation, handle_inbound (classify customer messages), "
            "escalate_to_human, get_status, get_outcome, preview_cost (free), self_test (free). "
            "ALWAYS call preview_cost before any state-changing operation. Always pass "
            "an X-Agent-Identity header for state-changing ops. WinRate is the north-star metric."
        ),
        "auth": {
            "type": "user_http",
            "authorization_type": "bearer",
            "verification_tokens": {},
        },
        "api": {
            "type": "openapi",
            "url": f"{BASE_URL}/openapi.yaml",
            "is_user_authenticated": True,
        },
        "logo_url": f"{BASE_URL}/static/logo.png",
        "contact_email": "basilalshukaili@gmail.com",
        "legal_info_url": f"{BASE_URL}/legal",
    }


# ---------------------------------------------------------------------------
# /.well-known/openai-tools.json — OpenAI function-calling format
# ---------------------------------------------------------------------------

def get_openai_tools() -> dict:
    """OpenAI function-calling tools array. Drop-in for `tools=` parameter."""
    manifest = get_full_manifest()
    tools = []
    for op in manifest.get("operations", []):
        tools.append({
            "type": "function",
            "function": {
                "name": op["name"],
                "description": _llm_optimized_description(op),
                "parameters": op.get("input_schema", {"type": "object"}),
            },
        })
    return {
        "version": "1.0",
        "service": "agent-broker",
        "tools": tools,
        "endpoint": f"{BASE_URL}/ops/{{tool_name}}",
        "auth_header": "X-Agent-Identity",
    }


# ---------------------------------------------------------------------------
# /.well-known/anthropic-tools.json — Claude tool_use format
# ---------------------------------------------------------------------------

def get_anthropic_tools() -> dict:
    """Anthropic tool_use array format. Drop-in for `tools=` parameter."""
    manifest = get_full_manifest()
    tools = []
    for op in manifest.get("operations", []):
        tools.append({
            "name": op["name"],
            "description": _llm_optimized_description(op),
            "input_schema": op.get("input_schema", {"type": "object"}),
        })
    return {
        "version": "1.0",
        "service": "agent-broker",
        "tools": tools,
        "endpoint": f"{BASE_URL}/ops/{{tool_name}}",
        "auth_header": "X-Agent-Identity",
        "mcp_endpoint": f"{BASE_URL}/mcp",
    }


# ---------------------------------------------------------------------------
# /.well-known/agents.json — A2A (Agent-to-Agent) protocol descriptor
# ---------------------------------------------------------------------------

def get_agents_json() -> dict:
    """
    A2A protocol descriptor. The emerging standard for agents to discover
    other agents/services. https://github.com/google/A2A
    """
    manifest = get_full_manifest()
    skills = []
    for op in manifest.get("operations", []):
        skills.append({
            "id": op["name"],
            "name": op["name"].replace("_", " ").title(),
            "description": op["description"],
            "tags": [op.get("execution_profile", "sync"),
                     *_extract_skill_tags(op)],
            "examples": [ex.get("description", "") for ex in op.get("examples", [])][:2],
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
        })

    return {
        "name": "SMB Transaction & Communication Broker",
        "description": (
            "Agent-callable service for the long tail of small businesses. "
            "Discover, verify, communicate, schedule, transact — all with built-in "
            "TCPA/GDPR/CASL compliance and idempotent semantics."
        ),
        "version": "0.1.0",
        "protocol_version": "a2a-v0.2",
        "url": BASE_URL,
        "documentation_url": f"{BASE_URL}/docs",
        "default_input_modes": ["application/json"],
        "default_output_modes": ["application/json"],
        "capabilities": {
            "streaming": True,                # for async ops
            "push_notifications": True,        # webhooks
            "state_transition_history": True,  # via get_status
        },
        "authentication": {
            "schemes": ["bearer", "agent-identity-jwt"],
            "header": "X-Agent-Identity",
            "token_endpoint": f"{BASE_URL}/auth/token",
        },
        "skills": skills,
        "supported_protocols": ["mcp", "openai-tools", "anthropic-tools", "rest", "a2a"],
        "discovery_urls": {
            "mcp": f"{BASE_URL}/mcp",
            "openapi": f"{BASE_URL}/openapi.yaml",
            "manifest": f"{BASE_URL}/manifest",
            "ai_plugin": f"{BASE_URL}/.well-known/ai-plugin.json",
        },
    }


# ---------------------------------------------------------------------------
# /.well-known/mcp.json — MCP server descriptor
# ---------------------------------------------------------------------------

def get_mcp_descriptor() -> dict:
    """Where to connect to our MCP server."""
    return {
        "name": "agent-broker",
        "version": "0.1.0",
        "transport": {
            "type": "http",
            "endpoint": f"{BASE_URL}/mcp",
            "method": "POST",
            "content_type": "application/json",
        },
        "alternate_transport": {
            "type": "sse",
            "endpoint": f"{BASE_URL}/mcp/sse",
        },
        "description": (
            "MCP server for SMB Transaction & Communication Broker. "
            "Exposes 12 operations: find_business, verify_business, send_message, "
            "capture_lead, schedule_appointment, send_transactional_confirmation, "
            "handle_inbound, escalate_to_human, get_status, get_outcome, "
            "preview_cost, self_test."
        ),
        "auth": {
            "header": "X-Agent-Identity",
            "scheme": "bearer",
        },
    }


# ---------------------------------------------------------------------------
# /llms.txt — LLM-readable site map
# ---------------------------------------------------------------------------

def get_llms_txt() -> str:
    """
    Emerging standard: https://llmstxt.org/
    A markdown site map specifically for LLM crawlers and agents.
    """
    manifest = get_full_manifest()
    service = manifest.get("service", {})
    operations = manifest.get("operations", [])

    lines = [
        f"# {service.get('name', 'Agent Broker')}",
        "",
        f"> {service.get('description', '')}",
        "",
        "An agent-callable service that lets autonomous AI agents discover, "
        "verify, communicate with, schedule with, and transact with long-tail "
        "small/mid-sized businesses (SMBs) through a single compliance-aware tool surface.",
        "",
        "## Why agents should use this service",
        "",
        "- **Coverage**: 60M+ US SMBs, with a wedge focus on personal services, "
        "home services, and professional services.",
        "- **Compliance built in**: TCPA, GDPR, CASL, CAN-SPAM, 10DLC, and "
        "two-party recording consent are enforced as a non-bypassable gate.",
        "- **Idempotent**: every state-changing operation is keyed by "
        "(agent_id, operation, idempotency_key) — safe to retry.",
        "- **Predictable cost**: `preview_cost` is free and returns ±5% "
        "accurate estimates before you commit.",
        "- **Channel fallback**: direct API → voice AI → SMS → email → web "
        "form — automatic, transparent.",
        "- **Async-aware**: long-running operations return `pending_async` "
        "with a webhook callback.",
        "",
        "## Discovery",
        "",
        f"- [Manifest (JSON)]({BASE_URL}/manifest)",
        f"- [OpenAPI spec]({BASE_URL}/openapi.yaml)",
        f"- [MCP endpoint]({BASE_URL}/mcp)",
        f"- [Service health]({BASE_URL}/health)",
        f"- [Discovery card]({BASE_URL}/.well-known/agent-service)",
        "",
        "## Operations",
        "",
    ]
    for op in operations:
        lines.append(f"### {op['name']}")
        lines.append("")
        lines.append(op["description"])
        lines.append("")
        lines.append(f"- **When to use**: {op['when_to_use']}")
        if op.get("when_not_to_use"):
            lines.append(f"- **When NOT to use**: {op['when_not_to_use']}")
        cost = op.get("cost_model", {})
        lines.append(f"- **Cost**: ${cost.get('amount_usd', 'varies')} {cost.get('basis', '')}")
        lines.append(f"- **Execution**: {op.get('execution_profile', 'sync')}")
        slo = op.get("slo", {})
        if slo:
            lat = slo.get("p50_latency_ms") or slo.get("max_latency_ms", "")
            lines.append(f"- **Latency**: ~{lat}ms")
        lines.append(f"- **Endpoint**: `POST {BASE_URL}/ops/{op['name']}`")
        lines.append("")

    lines += [
        "## Authentication",
        "",
        "All state-changing operations require an `X-Agent-Identity` JWT header. "
        f"Get a token from `{BASE_URL}/auth/token`. Scopes include allowed "
        "operations, budget cap, and verticals.",
        "",
        "## Compliance",
        "",
        f"See [compliance docs]({BASE_URL}/docs/compliance) for full jurisdiction matrix. "
        "Every outbound communication is gated by `compliance/pre_check` — "
        "this cannot be bypassed.",
        "",
        "## Contact",
        "",
        "basilalshukaili@gmail.com",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# /llms-full.txt — full content dump for crawlers
# ---------------------------------------------------------------------------

def get_llms_full_txt() -> str:
    """Full manifest + every operation example, expanded for LLM training crawlers."""
    manifest = get_full_manifest()
    parts = [
        get_llms_txt(),
        "",
        "## Full Manifest",
        "",
        "```json",
        json.dumps(manifest, indent=2),
        "```",
        "",
    ]
    for op in manifest.get("operations", []):
        parts.append(f"## Operation: {op['name']}")
        parts.append("")
        parts.append("### Input Schema")
        parts.append("```json")
        parts.append(json.dumps(op.get("input_schema", {}), indent=2))
        parts.append("```")
        parts.append("")
        parts.append("### Output Schema")
        parts.append("```json")
        parts.append(json.dumps(op.get("output_schema", {}), indent=2))
        parts.append("```")
        parts.append("")
        parts.append("### Examples")
        for i, ex in enumerate(op.get("examples", [])[:3]):
            parts.append(f"#### Example {i+1}: {ex.get('description', '')}")
            parts.append("```json")
            parts.append(json.dumps(ex, indent=2))
            parts.append("```")
            parts.append("")
        parts.append("### Failure Modes")
        for fm in op.get("failure_modes", []):
            if isinstance(fm, dict):
                parts.append(f"- **{fm.get('code', '')}**: {fm.get('description', '')}")
            else:
                parts.append(f"- {fm}")
        parts.append("")

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_optimized_description(op: dict) -> str:
    """Build a concise but complete description optimized for LLM tool selection."""
    parts = [op["description"]]
    if op.get("when_to_use"):
        parts.append(f"Use when: {op['when_to_use']}")
    if op.get("when_not_to_use"):
        parts.append(f"Do NOT use when: {op['when_not_to_use']}")
    cost = op.get("cost_model", {})
    if cost.get("amount_usd"):
        parts.append(f"Cost: ~${cost['amount_usd']} per call.")
    profile = op.get("execution_profile")
    if profile and profile != "sync":
        parts.append(f"Execution: {profile} — returns pending_async.")
    return " ".join(parts)


def _extract_skill_tags(op: dict) -> list[str]:
    tags = []
    name = op["name"]
    if "find" in name or "verify" in name or "preview" in name:
        tags.append("read_only")
    if "send" in name or "schedule" in name or "capture" in name:
        tags.append("write")
    if "compliance_constraints" in op and op["compliance_constraints"]:
        tags.append("compliance_gated")
    return tags
