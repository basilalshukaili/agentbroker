"""
Manifest server — serves the capability manifest to calling agents.

Endpoints:
  GET /manifest          → full manifest.json
  GET /manifest/ops      → operations list only (lighter payload)
  GET /manifest/op/{name}→ single operation detail
  GET /manifest/version  → version + last_updated

The manifest server is the first contact point for an agent discovering
this service. It is READ-ONLY and requires no auth (public discovery).
The ABRouter is consulted to serve a variant manifest if an experiment
is active and the agent_id header is present.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from optimizer.ab_router import ABRouter

MANIFEST_PATH = Path(__file__).parent.parent / "manifest" / "manifest.json"
_manifest_cache: Optional[dict] = None
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS = 60.0


def _load_manifest() -> dict:
    global _manifest_cache, _cache_loaded_at
    now = time.time()
    if _manifest_cache is None or (now - _cache_loaded_at) > _CACHE_TTL_SECONDS:
        _manifest_cache = json.loads(MANIFEST_PATH.read_text())
        _cache_loaded_at = now
    return _manifest_cache


def get_full_manifest(agent_id: Optional[str] = None) -> dict:
    """
    Return the full manifest, optionally with A/B variant overlay.
    If a variant has a manifest_override, it is merged over the base manifest.
    """
    base = _load_manifest()

    if agent_id:
        router = ABRouter.get_active()
        assignment = router.get_variant(agent_id)
        if assignment.variant.manifest_override:
            # Shallow merge: variant can override specific sections
            merged = dict(base)
            merged.update(assignment.variant.manifest_override)
            merged["_variant_id"] = assignment.variant_id
            merged["_experiment_id"] = assignment.experiment_id
            return merged
        # Still tag the response with variant info even if no override
        result = dict(base)
        result["_variant_id"] = assignment.variant_id
        result["_experiment_id"] = assignment.experiment_id
        return result

    return base


def get_operations_list(agent_id: Optional[str] = None) -> list[dict]:
    """Return lightweight operation summaries: name, description, execution_profile, cost_model."""
    manifest = get_full_manifest(agent_id)
    return [
        {
            "name": op["name"],
            "description": op["description"],
            "when_to_use": op["when_to_use"],
            "execution_profile": op["execution_profile"],
            "cost_model": op["cost_model"],
            "slo": op["slo"],
        }
        for op in manifest.get("operations", [])
    ]


def get_operation(name: str, agent_id: Optional[str] = None) -> Optional[dict]:
    """Return a single operation by name."""
    manifest = get_full_manifest(agent_id)
    for op in manifest.get("operations", []):
        if op["name"] == name:
            return op
    return None


def get_manifest_version(agent_id: Optional[str] = None) -> dict:
    manifest = get_full_manifest(agent_id)
    service = manifest.get("service", {})
    return {
        "version": service.get("version", "unknown"),
        "last_updated": service.get("last_updated", "unknown"),
        "service_name": service.get("name", "unknown"),
        "operation_count": len(manifest.get("operations", [])),
    }
