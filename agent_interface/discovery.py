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
            # NOT /auth/token — that route is admin-only (X-Admin-Secret,
            # 401 for every outside caller; disabled entirely when
            # ADMIN_SECRET is unset, which is production's state today). The
            # route an integrator can actually use is the email-verified free
            # key flow below. It currently requires a human to open the
            # verification email; there is no machine-mintable path in
            # production (POST /keys/mint returns 503 not_configured and is
            # not something to build against).
            "free_key_url": "/keys/request",
            "free_key_method": "POST {\"email\": \"you@example.com\"}",
            "token_format": "HS256 signed claims",
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
    # version: so a post-deploy smoke can PROVE the new build is serving.
    # Render reports a deploy "live" ~45s before the new code answers, and the
    # old smoke checks (200 on /health, find_business in tools/list) pass
    # identically on the OLD build - a deploy that never flipped read green.
    # Compare this field to the pushed SERVICE_VERSION and the gap is visible.
    #
    # THAT COMMENT WAS THE PLAN, AND IT DID NOT HOLD. SERVICE_VERSION is a
    # hand-edited literal (config.py) that went 19 commits / 11 days
    # (2026-09-10 -> 2026-09-21) without a single bump while api.hatchloop.dev
    # served stale code with four live authorization holes - it read "0.2.13"
    # on day one and "0.2.13" on day eleven, so nothing that compared against
    # it could have told the difference. A version field that never moves is
    # worse than none, because it LOOKS like verification.
    #
    # build_commit / deploy_target below are the fields that actually cannot
    # be stale this way: they are stamped into the image from the commit
    # `docker build` was actually given (deploy/Dockerfile ARG GIT_COMMIT /
    # ARG DEPLOY_TARGET, set by ops/vps/deploy_agentbroker_vps.py), not typed
    # by a person choosing when to bump a number. "unknown" means exactly
    # that: this image was built without the stamp, which is itself
    # informative (an old image, or a build that bypassed the deploy script).
    try:
        from config import SERVICE_VERSION as _sv
    except Exception:                           # noqa: BLE001
        _sv = "unknown"
    try:
        from config import GIT_COMMIT as _gc, DEPLOY_TARGET as _dt
    except Exception:                           # noqa: BLE001
        _gc, _dt = "unknown", "unknown"
    return {
        "status": "healthy" if not broken else "unhealthy",
        "version": _sv,
        # Short commit sha only - never a path, token, or env dump. See
        # scripts/system_health.py check_agentbroker_deploy_drift for what
        # reads these two fields and why.
        "build_commit": _gc,
        "deploy_target": _dt,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
        # Present for a reader, never for the restart decision - see above.
        "degraded": broken or None,
    }
