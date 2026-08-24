"""
billing/packages.py -- Polar package definitions for AgentBroker credits.

Editable config for the 3 packages the founder creates on Polar's dashboard.
The founder sets product_id -> credits via the POLAR_PACKAGES env var (JSON)
so product IDs (which are assigned by Polar) can be wired without a code deploy.

Founder one-time setup (Polar dashboard):
  - Starter  $9   / 1000 credits   product with metadata.credits=1000
  - Growth   $29  / 3500 credits   product with metadata.credits=3500
  - Scale    $99  / 13000 credits  product with metadata.credits=13000
Then set POLAR_PACKAGES={"<starter_id>": 1000, "<growth_id>": 3500, "<scale_id>": 13000}

Usage:
  from billing.packages import credits_for_product
  cr = credits_for_product(product_name="Starter", product_id="prod_abc", product_metadata={})
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("smb_broker.packages")

# ---------------------------------------------------------------------------
# Name-to-credits map (fallback when metadata.credits is absent).
# Keys are lowercase; matching is case-insensitive prefix/substring.
# ---------------------------------------------------------------------------
PACKAGE_CREDITS: dict[str, int] = {
    "starter":  1000,
    "growth":   3500,
    "scale":    13000,
}

# ---------------------------------------------------------------------------
# POLAR_PACKAGES env override: JSON mapping product_id -> credits.
# Set by the founder after creating the Polar products so product IDs
# (assigned by Polar, unknown until creation) can be wired without a deploy.
# Example: POLAR_PACKAGES={"prod_abc123": 1000, "prod_def456": 3500}
# ---------------------------------------------------------------------------

def _load_polar_packages() -> dict[str, int]:
    """Load POLAR_PACKAGES from env. Returns {} if not set or malformed."""
    raw = os.getenv("POLAR_PACKAGES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): int(v) for k, v in parsed.items()}
    except Exception as exc:
        log.warning("POLAR_PACKAGES env malformed: %s -- ignoring", exc)
    return {}


def credits_for_product(
    *,
    product_name: str = "",
    product_id: str = "",
    product_metadata: dict[str, Any] | None = None,
) -> int:
    """Resolve credit amount for a purchased Polar product.

    Priority order:
    1. product.metadata.credits (set by founder on the Polar product)
    2. POLAR_PACKAGES env: product_id -> credits
    3. PACKAGE_CREDITS name map: match on lowercase product name

    Returns 0 if nothing matches (caller should log a warning and skip grant).
    """
    meta = product_metadata or {}

    # 1. metadata.credits
    meta_credits = meta.get("credits")
    if meta_credits is not None:
        try:
            cr = int(meta_credits)
            if cr > 0:
                return cr
        except (TypeError, ValueError):
            log.warning("product metadata.credits not an int: %r", meta_credits)

    # 2. POLAR_PACKAGES env map
    polar_pkgs = _load_polar_packages()
    if product_id and product_id in polar_pkgs:
        return polar_pkgs[product_id]

    # 3. Name heuristic (lowercase prefix/substring match)
    name_lower = (product_name or "").lower()
    for key, cr in PACKAGE_CREDITS.items():
        if key in name_lower:
            return cr

    log.warning(
        "credits_for_product: no match for product_name=%r product_id=%r meta=%r",
        product_name, product_id, meta,
    )
    return 0
