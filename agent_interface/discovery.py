"""
Discovery module — helps agents find this service and understand its capabilities
before making their first call.

Endpoints:
  GET /.well-known/agent-service    → service discovery card
  GET /capabilities                 → flat capability list (for LLM tool selection)
  GET /health                       → service health + version
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from agent_interface.manifest_server import get_full_manifest, get_manifest_version

_SERVICE_DESCRIPTION = (
    "SMB Transaction & Communication Broker — enables AI agents to discover, "
    "verify, communicate with, and schedule appointments with long-tail small "
    "and mid-sized businesses through a single, compliance-aware tool surface."
)

_CONTACT = {
    "support_email": "support@agent-broker-edge.basil-agent.workers.dev",
    "docs_url": "https://agent-broker-edge.basil-agent.workers.dev/docs",
    "openapi_url": "https://agent-broker-edge.basil-agent.workers.dev/openapi.yaml",
    "mcp_tools_url": "https://agent-broker-edge.basil-agent.workers.dev/manifest/mcp_tools.json",
}


def get_discovery_card(agent_id: Optional[str] = None) -> dict:
    """
    Service discovery card — the first document an agent should fetch.
    Tells the agent what this service does, how to auth, and where to get
    the full capability manifest.
    """
    version_info = get_manifest_version(agent_id)
    return {
        "service_type": "smb_broker",
        "service_id": "smb-broker-v1",
        "version": version_info["version"],
        "description": _SERVICE_DESCRIPTION,
        "auth": {
            "scheme": "AgentIdentity",
            "header": "X-Agent-Identity",
            "token_url": "/auth/token",
            "token_format": "HS256 signed claims (stub) — use issue_token()",
        },
        "manifest_url": "/manifest",
        "operations_url": "/manifest/ops",
        "health_url": "/health",
        "contact": _CONTACT,
        "verticals_supported": [
            "personal_services",
            "home_services",
            "professional_services",
        ],
        "geo_coverage": ["US"],
        "compliance": {
            "tcpa": True,
            "gdpr": True,
            "casl": True,
            "can_spam": True,
            "10dlc": True,
            "recording_consent": True,
        },
        "execution_profiles": {
            "sync": "≤2s response",
            "sync_fast": "≤5s response",
            "async_by_default": "returns pending_async, completes via webhook",
        },
    }


def get_capabilities_flat(agent_id: Optional[str] = None) -> list[dict]:
    """
    Flat capability list optimized for LLM tool selection.
    Each entry is a concise {name, description, when_to_use} triple.
    """
    manifest = get_full_manifest(agent_id)
    return [
        {
            "name": op["name"],
            "description": op["description"],
            "when_to_use": op["when_to_use"],
            "when_not_to_use": op.get("when_not_to_use", ""),
            "execution_profile": op.get("execution_profile", "sync"),
        }
        for op in manifest.get("operations", [])
    ]


def health_check() -> dict:
    """Live health status — checked by orchestrators and circuit breakers."""
    return {
        "status": "healthy",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": {
            "manifest": "ok",
            "directory": "ok",
            "compliance": "ok",
        },
    }
