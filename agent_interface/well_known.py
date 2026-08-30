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
from config import SERVICE_VERSION
from agent_interface.manifest_server import get_full_manifest
from agent_interface import profiles


# ---------------------------------------------------------------------------
# Base URLs (override via env in production)
# ---------------------------------------------------------------------------

import os  # noqa: F401 - other helpers in this module still read env vars
# THE ONE normalised value (see config.PUBLIC_BASE_URL). This used to read the
# env var directly and skip the scheme/trailing-slash handling that
# web/_partials.py applied, so a scheme-less PUBLIC_BASE_URL made every
# descriptor here emit URLs an agent cannot fetch while the website was fine.
from config import PUBLIC_BASE_URL as BASE_URL


def _tool_names() -> list[str]:
    return [op["name"] for op in get_full_manifest().get("operations", [])]


def _mcp_description() -> str:
    """Built from the live operation list, never a typed-out count."""
    names = _tool_names()
    return (f"MCP server for HatchLoop AgentBroker. Exposes {len(names)} "
            f"operations: {', '.join(names)}.")


def _payments_block() -> dict:
    """What a caller actually pays. Derived from billing/pricing.py.

    The old hardcoded note said billing was "not yet active" and that "All
    tools are currently free to call". Neither is true: credits are live and
    write tools spend them. Telling an agent everything is free is the same
    class of defect as a manifest advertising prices we do not charge.
    """
    try:
        from billing.pricing import _PRICING_CENTS
        free = sorted(n for n, c in _PRICING_CENTS.items() if c == 0)
        paid = sorted(n for n, c in _PRICING_CENTS.items() if c > 0)
    except Exception:  # noqa: BLE001
        return {"status": "see_pricing",
                "note": "See https://hatchloop.dev/pricing for current pricing."}
    # RAILS ARE DERIVED FROM THE GATE, not asserted.
    #
    # This said `["credits"]` with a comment that crypto "is built and off".
    # It was neither off nor unbuilt: X402_ENABLED=true on the origin with a
    # live receiver address, and attaching any payment payload to a paid tool
    # returns a complete x402 PaymentRequired offer - USDC on Base, a real
    # price, a real address. The rail an autonomous agent can complete WITHOUT
    # A HUMAN was the one rail every discovery document denied existed.
    #
    # That is the producer-with-no-caller pattern applied to the payment
    # funnel: the capability shipped and nothing told anyone it was there.
    #
    # The comment's reasoning was right when written - no VASP licence exists
    # in Oman and the CBO warns against crypto payments, so advertising it was
    # blocked pending legal clearance. The founder closed that on 2026-08-29:
    # "No need for lawyer writing, anyway we will not deposit the crypto money
    # to Oman, so it is ok you can continue without it." His decision, his
    # exposure, recorded here so the change does not later look accidental.
    #
    # Derived rather than re-hardcoded to "credits, x402", because the next
    # person to switch the gate off must not have to remember this file.
    try:
        from billing import x402_gate
        x402_live = x402_gate.enabled()
    except Exception:  # noqa: BLE001
        x402_live = False

    rails = ["credits"] + (["x402"] if x402_live else [])
    return {
        "status": "active",
        "free_tools": free,
        "paid_tools": paid,
        "unit": "credits (1 credit = 1 US cent)",
        "rails": rails,
        "note": (
            f"{len(free)} tools are free to call; {len(paid)} spend credits. "
            "Premium data tools are free within a daily quota, then billed per "
            "call. Call preview_cost (free) for the exact price of any "
            "operation before committing. Pricing: https://hatchloop.dev/pricing"
            + (" No account needed to pay per call: attach an x402 payment in "
               "params._meta['x402/payment'] and the server returns a signed "
               "price offer (USDC on Base) for any paid tool."
               if x402_live else "")
        ),
    }


def describe_cost(cost: dict) -> str:
    """One honest cost sentence for any cost_model class.

    These descriptors read `amount_usd`, a key the generated cost models do not
    have — so after the manifest was regenerated they said NOTHING about price.
    Silence is better than the old lie ($0.005 for a tool that is free) but
    worse than the truth: an agent choosing tools should be told "free"
    explicitly, not left to infer it.
    """
    if not cost:
        return ""
    basis = cost.get("basis")
    amount = cost.get("unit_price_usd", cost.get("amount_usd"))
    if basis == "free" or amount in (0, 0.0):
        return "Cost: free (no key required)."
    if basis == "freemium_daily_quota":
        return f"Cost: free within the daily quota, then ${amount} per call."
    if amount is None:
        return "Cost: see preview_cost."
    max_usd = cost.get("max_price_usd")
    if max_usd and max_usd != amount:
        return (f"Cost: from ${amount} per call, up to ${max_usd} "
                f"(call preview_cost for the exact price).")
    return f"Cost: ${amount} per call."


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
        # A ROLE address, not a person. The founder's personal Gmail was published
        # here and in llms-full.txt - live, public, and machine-read by plugin
        # infrastructure (found 2026-08-26).
        "contact_email": "hello@hatchloop.dev",
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
        "version": SERVICE_VERSION,
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
# /.well-known/agent-card.json (+ /.well-known/agent.json) — canonical A2A card
# ---------------------------------------------------------------------------
# The A2A protocol's discovery file was renamed agent.json → agent-card.json.
# Agent-registry crawlers (e.g. AgenstryBot) probe these canonical paths; we
# previously only served the non-standard plural agents.json, so they 404'd and
# couldn't index us. This serves a spec-shaped AgentCard (camelCase fields) so
# those registries catalog us. Honest about transport: discovery/transaction is
# via our MCP endpoint (we are an MCP server, not a full
# A2A JSON-RPC server) — the card's description and _meta make that explicit so
# a discovering agent knows exactly how to actually call us.

def get_agent_card() -> dict:
    """Canonical A2A AgentCard, generated from the manifest."""
    manifest = get_full_manifest()
    skills = []
    for op in manifest.get("operations", []):
        skills.append({
            "id": op["name"],
            "name": op["name"].replace("_", " ").title(),
            "description": op["description"],
            "tags": [op.get("execution_profile", "sync"), *_extract_skill_tags(op)],
            "examples": [ex.get("description", "") for ex in op.get("examples", [])][:2],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        })
    mcp_url = f"{BASE_URL}/mcp"
    return {
        "protocolVersion": "0.2.5",
        "name": "Agent Broker",
        "description": (
            "AI agents find, verify, message, and book appointments with small "
            "businesses worldwide. Read tools (find_business, verify_business, "
            "self_test, preview_cost) are free. Write tools require an "
            "X-Agent-Identity token. Built-in TCPA/GDPR/CASL compliance gate. "
            "Connect via the MCP endpoint below (streamable-http)."
        ),
        "url": mcp_url,
        "preferredTransport": "streamable-http",
        "version": "1.0.2",
        "provider": {
            "organization": "Agent Broker",
            "url": BASE_URL,
        },
        "documentationUrl": f"{BASE_URL}/llms.txt",
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
        "_meta": {
            "transport": "mcp",
            "mcpEndpoint": mcp_url,
            # DERIVED, not asserted. This block said status "coming_soon",
            # "Billing is in development", "all tools are callable at no cost"
            # and "Crypto payment is not offered" - FOUR claims, every one of
            # them false. Credits went live 2026-08-24 and the x402 rail has
            # been accepting payment offers in production. An agent card is
            # read by machines deciding whether they can transact with us, so
            # these were the most expensive wrong sentences on the site.
            "payments": _payments_block(),
        },
    }


# ---------------------------------------------------------------------------
# /.well-known/mcp.json — MCP server descriptor
# ---------------------------------------------------------------------------

def get_mcp_descriptor() -> dict:
    """Where to connect to our MCP server."""
    return {
        "name": "agent-broker",
        "version": SERVICE_VERSION,
        "transport": {
            "type": "streamable-http",
            "endpoint": f"{BASE_URL}/mcp",
            "method": "POST",
            "content_type": "application/json",
        },
        # Both fields below were HARDCODED and both were false on the live
        # canonical host (found 2026-08-26): the description claimed "16
        # operations" while tools/list returned 20, and the payments note told
        # every agent that "All tools are currently free to call" while write
        # tools spend credits and premium data tools carry a daily quota.
        # Derived now, so neither can drift again.
        "payments": _payments_block(),
        "description": _mcp_description(),
        "auth": {
            "header": "X-Agent-Identity",
            "scheme": "bearer",
        },
        # NARROWER ENDPOINTS, advertised where an agent already looks.
        #
        # These exist because tools/list on the full server costs ~11,000
        # tokens, and an agent that came to screen one company against sanctions
        # needs about 1,000 of them. Each endpoint serves the SAME engine
        # through a smaller door and refuses everything it does not list.
        #
        # DERIVED FROM profiles.PROFILES, never typed. The pair of hardcoded
        # fields above were both false on the live host for weeks - "16
        # operations" when tools/list returned 20, and "all tools are free" when
        # writes spend credits. A hand-maintained list here would rot the same
        # way the moment a profile is added or a tool moves between them.
        #
        # Deliberately advertised HERE rather than as separate MCP registry
        # entries: the registry's moderation policy names "the same server
        # submitted multiple times under different names" as spam, and these
        # share a backend. This surface is ours, an agent reading it has already
        # found us, and pointing it at a cheaper door is a courtesy rather than
        # a second bite at discovery.
        "capability_endpoints": [
            {
                "name": pid,
                "endpoint": f"{BASE_URL}/mcp/{pid}",
                "description": spec["description"],
                "tool_count": len(profiles.tools_for(pid)),
            }
            for pid, spec in sorted(profiles.PROFILES.items())
        ],
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
        "- **Addressable market**: 60M+ US SMBs in the addressable market, "
        "wedge focus on personal services, home services, and professional services. "
        "Current on-platform supply is in early rollout — call `import_booking_url` "
        "with any public booking URL to add a business instantly.",
        "- **Compliance built in**: TCPA, GDPR, CASL, CAN-SPAM, 10DLC, and "
        "two-party recording consent are enforced as a non-bypassable gate.",
        "- **Idempotent**: every state-changing operation is keyed by "
        "(agent_id, operation, idempotency_key) — safe to retry.",
        "- **Predictable cost**: `preview_cost` is free and returns the exact "
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
        lines.append(f"- **{describe_cost(op.get('cost_model', {})) or 'Cost: varies'}**")
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
        "This service only completes consumer-initiated transactional flows; "
        "marketing, promotional, and unsolicited outbound communication are out of "
        "scope and rejected by `compliance/pre_check`. The gate cannot be bypassed.",
        "",
        "## Connecting from an agent harness",
        "",
        # WRITTEN FROM THEIR SOURCE, not from a guess. Verified 2026-08-29 by
        # reading packages/mcp/mcp-client/README.md in deepseek-ai/deepseek-harness:
        # it takes `transport: streamable-http` with `url` and optional
        # `headers`, and surfaces tools as mcp__<serverName>__<tool>. We are
        # compatible today with no changes on either side.
        #
        # Their own docs say the main cost of attaching an MCP server is "the "
        # tokens those tool definitions add to every request" - which is exactly
        # what the narrow endpoints below are for, so the example leads with one
        # rather than with the 20-tool server.
        "Any client that speaks MCP over streamable HTTP can attach this server "
        "directly; there is nothing to install and no key is needed for the free "
        "tools.",
        "",
        "**DeepSeek Harness (`dsh`)** - add one entry to your config:",
        "",
        "```yaml",
        "- id: mcp-hatchloop",
        "  name: '@deepseek-ai/dsh-mcp-client'",
        "  config:",
        "    serverName: hatchloop",
        "    transport: streamable-http",
        f"    url: {BASE_URL}/mcp/sanctions-screening",
        "```",
        "",
        "Tools then appear to the model as `mcp__hatchloop__screen_sanctions` "
        "and so on.",
        "",
        "**Pick the narrowest endpoint that covers your use case.** Every tool "
        "definition costs tokens on every request, so attaching 20 tools when "
        "you need 8 is a permanent tax on each call:",
        "",
    ] + [
        f"- `{BASE_URL}/mcp/{pid}` - {spec['description']} "
        f"({len(profiles.tools_for(pid))} tools)"
        for pid, spec in sorted(profiles.PROFILES.items())
    ] + [
        f"- `{BASE_URL}/mcp/agent-broker` - everything ({len(get_full_manifest().get('operations', []))} tools)",
        "",
        "The same rows work in Claude Code, Codex, and any other MCP client - "
        "the transport is standard.",
        "",
        "## Contact",
        "",
        "hello@hatchloop.dev",
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
    cost_line = describe_cost(op.get("cost_model", {}))
    if cost_line:
        parts.append(cost_line)
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
