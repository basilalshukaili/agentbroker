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
    # Real, monitored inbox on our own domain. This published a generic
    # *.workers.dev address that was also not a real mailbox - anyone who
    # replied to our own discovery descriptor reached nobody (2026-08-26).
    "support_email": "hello@hatchloop.dev",
    "docs_url": "https://hatchloop.dev/docs/",
    "openapi_url": "https://hatchloop.dev/openapi.yaml",
    "mcp_tools_url": "https://hatchloop.dev/.well-known/mcp.json",
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
    """Live health status - checked by orchestrators and circuit breakers.

    THIS USED TO BE FOUR STRING LITERALS. It reported manifest, directory and
    compliance as "ok" without looking at any of them, and it had no code path
    that could return anything else. Render gates container restarts on it
    (`healthCheckPath: /health`) and CI gates the post-deploy step on it, so
    both were satisfied by a process that could serve a constant.

    WHY IT CHECKS WHAT IT CHECKS. A liveness endpoint that fails when a
    DEPENDENCY blips is worse than a constant: Render restarts the container,
    the restart does not fix the dependency, and the service enters a restart
    loop. I put this exact service into one earlier today by a different
    route. So the split is deliberate:

      * `status` reflects THIS PROCESS's own invariants - the manifest parses,
        the directory loads, the compliance rules are present. Those are
        in-process, cheap, and a genuine reason to replace the container.
      * `dependencies` reports outward state honestly and NEVER changes
        `status`, so a Supabase or Treasury outage is visible without
        triggering a restart that cannot help.
    """
    checks: dict[str, str] = {}

    try:
        m = get_full_manifest()
        ops = (m or {}).get("operations") or []
        checks["manifest"] = "ok" if len(ops) >= 1 else "empty"
    except Exception as exc:                    # noqa: BLE001
        checks["manifest"] = f"error: {type(exc).__name__}"

    try:
        from supply.smb_directory import get_directory
        d = get_directory()
        checks["directory"] = "ok" if d is not None else "unavailable"
    except Exception as exc:                    # noqa: BLE001
        checks["directory"] = f"error: {type(exc).__name__}"

    try:
        from compliance.jurisdiction_rules import _RULES
        checks["compliance"] = "ok" if len(_RULES) >= 1 else "empty"
    except Exception as exc:                    # noqa: BLE001
        checks["compliance"] = f"error: {type(exc).__name__}"

    broken = [k for k, v in checks.items() if v != "ok"]
    return {
        "status": "healthy" if not broken else "unhealthy",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
        # Present for a reader, never for the restart decision - see above.
        "degraded": broken or None,
    }
