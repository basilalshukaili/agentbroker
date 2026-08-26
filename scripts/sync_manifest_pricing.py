#!/usr/bin/env python3
"""Regenerate manifest cost_model blocks from billing/pricing.py.

WHY THIS EXISTS. `billing/pricing.py` calls itself the single source of truth,
and for both billing rails it is. But `manifest/manifest.json` carried its own
hand-written USD prices, and they drifted into a LIE: the manifest advertised
find_business at $0.01, verify_business at $0.02, call_business at $0.50 and the
read tools at $0.001, while pricing.py charges ZERO for every one of them. An
agent reading our manifest to decide whether it can afford a call was being told
we are more expensive than we are — and it contradicted "8 utility tools always
free" on every other public surface.

Drift like that is not fixed by editing the file once. It is fixed by removing
the second source. This script derives every cost_model from pricing.py, and
tests/unit/test_manifest_pricing_parity.py fails if they ever diverge again.

Usage:
    python scripts/sync_manifest_pricing.py            # rewrite in place
    python scripts/sync_manifest_pricing.py --check    # exit 1 if out of sync
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MANIFEST = os.path.join(ROOT, "manifest", "manifest.json")

SERVICE_ID = "agent-broker"
SERVICE_NAME = "AgentBroker"

# Tools whose price is zero only because metering is currently OFF. Saying
# "free" flatly would become a lie the moment DATA_METERING_ENABLED flips, so
# they are described by their real rule instead of their current value.
PREMIUM_DATA = {"verify_company_record", "screen_sanctions", "map_trade_restriction"}


def build_cost_model(op_name: str) -> dict:
    from billing.pricing import _PRICING_CENTS, _MAX_PRICING_CENTS

    cents = _PRICING_CENTS.get(op_name)
    if cents is None:
        # Unknown to the price table: say so rather than inventing a number.
        return {"basis": "unpriced", "notes": "Not present in the price table."}

    if op_name in PREMIUM_DATA:
        return {
            "basis": "freemium_daily_quota",
            "unit_price_usd": round(cents / 100, 4),
            "free_quota_note": (
                "Free up to the daily quota (500/day with a free email-verified "
                "key, 100/day anonymous). Beyond the quota, billed per call via "
                "credits or x402."
            ),
        }

    if cents == 0:
        return {"basis": "free", "unit_price_usd": 0.0,
                "notes": "No key required, unmetered."}

    model = {"basis": "per_call", "unit_price_usd": round(cents / 100, 4),
             "credits": cents}
    max_cents = _MAX_PRICING_CENTS.get(op_name)
    if max_cents and max_cents != cents:
        # Variable-price op: advertise the CEILING too. Quoting only the
        # minimum would understate what an agent may actually be charged.
        model["basis"] = "per_call_variable"
        model["max_price_usd"] = round(max_cents / 100, 4)
        model["max_credits"] = max_cents
        model["notes"] = (
            f"Reserves up to {max_cents} credits and settles the actual cost "
            f"from the receipt; minimum {cents}."
        )
    return model


def sync(check_only: bool = False) -> int:
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)

    from config import SERVICE_VERSION

    drift: list[str] = []

    import os as _os
    base = _os.getenv("PUBLIC_BASE_URL", "https://api.hatchloop.dev").rstrip("/")

    svc = manifest.get("service", {})
    # The service header still carried api.smb-broker.example - a domain that
    # does not exist - in base_url, discovery_url and contact, four months
    # stale (found 2026-08-26). It is public: /manifest serves it.
    for field, want in (("id", SERVICE_ID), ("name", SERVICE_NAME),
                        ("version", SERVICE_VERSION),
                        ("base_url", base),
                        ("discovery_url", f"{base}/.well-known/mcp.json"),
                        ("contact", "hello@hatchloop.dev")):
        if field not in svc and field in ("base_url", "discovery_url", "contact"):
            continue  # do not invent fields the manifest never had
        if svc.get(field) != want:
            drift.append(f"service.{field}: {svc.get(field)!r} -> {want!r}")
            svc[field] = want
    manifest["service"] = svc

    for op in manifest.get("operations", []):
        want = build_cost_model(op["name"])
        if op.get("cost_model") != want:
            drift.append(
                f"{op['name']}.cost_model: "
                f"{json.dumps(op.get('cost_model'))[:60]} -> {json.dumps(want)[:60]}")
            op["cost_model"] = want

    if check_only:
        if drift:
            print(f"manifest_pricing: OUT OF SYNC -- {len(drift)} difference(s):")
            for d in drift:
                print("  " + d)
            return 1
        print("manifest_pricing: IN SYNC with billing/pricing.py")
        return 0

    if not drift:
        print("manifest_pricing: already in sync -- nothing written")
        return 0

    with open(MANIFEST, "w", encoding="utf-8", newline="") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"manifest_pricing: rewrote {len(drift)} block(s)")
    for d in drift:
        print("  " + d)
    return 0


if __name__ == "__main__":
    sys.exit(sync(check_only="--check" in sys.argv))
