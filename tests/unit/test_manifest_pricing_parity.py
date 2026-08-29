"""
The manifest must never advertise a price we do not charge.

`billing/pricing.py` calls itself the single source of truth, and for the two
billing rails it is — but `manifest/manifest.json` kept its own hand-written USD
prices and they drifted into a lie: find_business advertised at $0.01,
verify_business $0.02, call_business $0.50 and the read tools $0.001, while
pricing.py charges ZERO for every one of them (found 2026-08-26). An agent
reading the manifest to decide whether it could afford a call was told we cost
more than we do.

Editing the file once does not fix that class of bug — only removing the second
source does. These tests are the guard on the generator.
"""
from __future__ import annotations

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, "manifest", "manifest.json")


@pytest.fixture(scope="module")
def manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def ops(manifest):
    return {o["name"]: o for o in manifest["operations"]}


def test_manifest_is_in_sync_with_the_price_table():
    """The whole point: one source, checked mechanically."""
    from scripts.sync_manifest_pricing import sync
    assert sync(check_only=True) == 0, (
        "manifest cost_model has drifted from billing/pricing.py - "
        "run: python scripts/sync_manifest_pricing.py")


def test_free_tools_are_advertised_as_free(ops):
    """The specific lie that shipped: charging $0 while advertising a price."""
    from billing.pricing import _PRICING_CENTS
    premium = {"verify_company_record", "screen_sanctions", "map_trade_restriction"}
    for name, cents in _PRICING_CENTS.items():
        if cents != 0 or name in premium:
            continue
        cm = ops[name]["cost_model"]
        assert cm.get("unit_price_usd") in (0, 0.0), (
            f"{name} is free in pricing.py but the manifest advertises "
            f"{cm.get('unit_price_usd')}")


def test_paid_tools_match_the_price_table_exactly(ops):
    from billing.pricing import _PRICING_CENTS
    premium = {"verify_company_record", "screen_sanctions", "map_trade_restriction"}
    for name, cents in _PRICING_CENTS.items():
        if cents == 0 or name in premium:
            continue
        cm = ops[name]["cost_model"]
        assert cm["unit_price_usd"] == round(cents / 100, 4), (
            f"{name}: manifest {cm['unit_price_usd']} != pricing.py {cents}c")


def test_variable_price_tools_advertise_the_ceiling(ops):
    """Quoting only the minimum understates what an agent may be charged."""
    from billing.pricing import _MAX_PRICING_CENTS
    for name, max_cents in _MAX_PRICING_CENTS.items():
        cm = ops[name]["cost_model"]
        assert cm.get("max_price_usd") == round(max_cents / 100, 4), (
            f"{name} reserves up to {max_cents}c but the manifest does not say so")


def test_premium_data_tools_are_not_called_flatly_free(ops):
    """They are free only while metering is off. "free" becomes a lie the moment
    DATA_METERING_ENABLED flips, so the manifest must state the real rule."""
    for name in ("verify_company_record", "screen_sanctions", "map_trade_restriction"):
        cm = ops[name]["cost_model"]
        assert cm["basis"] == "freemium_daily_quota"
        assert cm["unit_price_usd"] > 0
        assert "quota" in cm.get("free_quota_note", "").lower()


def test_every_operation_has_a_cost_model(ops):
    for name, op in ops.items():
        assert op.get("cost_model"), f"{name} has no cost_model"


def test_manifest_covers_exactly_the_priced_operations(ops):
    """A tool in one list and not the other means someone is guessing."""
    from billing.pricing import ALL_OPERATIONS
    assert set(ops) == set(ALL_OPERATIONS), (
        f"manifest-only: {sorted(set(ops) - set(ALL_OPERATIONS))}, "
        f"pricing-only: {sorted(set(ALL_OPERATIONS) - set(ops))}")


def test_service_identity_matches_config(manifest):
    """Three surfaces once carried three different versions (0.1.0 / 0.2.3 /
    0.2.5). The manifest follows config, which follows the deployed build."""
    import config
    svc = manifest["service"]
    assert svc["id"] == "agent-broker"
    assert svc["version"] == config.SERVICE_VERSION


def test_tool_descriptions_carry_exactly_one_cost_line():
    """The COST line is what an agent reads to decide affordability. Two lines
    (the freemium branch falling through into the flat-price branch) would tell
    it two different prices for the same call."""
    from agent_interface.mcp_server import _build_tool_list
    for tool in _build_tool_list():
        lines = [l for l in tool["description"].splitlines() if l.startswith("COST:")]
        assert len(lines) == 1, f"{tool['name']} has {len(lines)} COST lines: {lines}"


def test_cost_lines_are_honest_per_class():
    from agent_interface.mcp_server import _build_tool_list
    got = {t["name"]: next(l for l in t["description"].splitlines()
                           if l.startswith("COST:"))
           for t in _build_tool_list()}
    # Was `== "COST: free"`. The line now also states whether a key is needed,
    # because free and keyless are different claims and conflating them made a
    # buyer count 13 free tools against a page saying 12 keyless ones. Assert
    # the CLASS (free, and says so) rather than the exact wording, so the
    # sentence can be improved without a test edit - but still pin that it does
    # not silently start quoting a price.
    assert got["find_business"].startswith("COST: free")
    assert "no key" in got["find_business"], (
        "a free, keyless tool must say so - it is the first thing an agent "
        "evaluating us will try")
    assert "$" not in got["find_business"]
    # variable ops must not quote a flat price
    assert "from $" in got["send_message"] and "preview_cost" in got["send_message"]
    # premium data must not claim to be flatly free
    assert "quota" in got["screen_sanctions"]
    assert got["screen_sanctions"] != "COST: free"


def test_mcp_server_version_is_derived_not_hardcoded():
    """The origin reported serverInfo 0.1.0 while the build was 0.2.x, and the
    edge snapshot refreshed FROM the origin would have inherited it."""
    import config
    from agent_interface import mcp_server
    assert mcp_server.SERVER_VERSION == config.SERVICE_VERSION
    src = open(mcp_server.__file__, encoding="utf-8").read()
    assert 'SERVER_VERSION = "' not in src, "version must be derived from config"


def test_well_known_descriptors_report_the_real_version():
    import config
    from agent_interface.well_known import get_mcp_descriptor
    assert get_mcp_descriptor()["version"] == config.SERVICE_VERSION


def test_descriptor_cost_sentences_cover_every_class():
    """openai-tools/anthropic-tools read `amount_usd`, which the generated cost
    models do not have — so after the regeneration they said NOTHING about
    price. Better than the old lie ($0.005 for a free tool), worse than the
    truth."""
    from agent_interface.well_known import describe_cost
    assert describe_cost({"basis": "free", "unit_price_usd": 0.0}).startswith("Cost: free")
    assert "quota" in describe_cost(
        {"basis": "freemium_daily_quota", "unit_price_usd": 0.02})
    assert describe_cost({"basis": "per_call", "unit_price_usd": 0.05}) == \
        "Cost: $0.05 per call."
    variable = describe_cost({"basis": "per_call_variable", "unit_price_usd": 0.02,
                              "max_price_usd": 0.22})
    assert "up to $0.22" in variable, "must advertise the ceiling, not just the floor"
    assert describe_cost({}) == ""


def test_no_descriptor_prices_a_free_tool():
    """The exact defect that shipped: import_booking_url advertised at $0.005
    across the OpenAI and Anthropic descriptors while pricing.py charges 0."""
    from agent_interface.well_known import get_openai_tools
    from billing.pricing import _PRICING_CENTS

    tools = get_openai_tools()
    tools = tools.get("tools", tools) if isinstance(tools, dict) else tools
    for entry in tools:
        fn = entry.get("function", entry)
        name = fn.get("name")
        if _PRICING_CENTS.get(name) != 0:
            continue
        desc = fn.get("description", "")
        assert "Cost: free" in desc or "quota" in desc, (
            f"{name} is free but its descriptor says: "
            f"{[s for s in desc.split('. ') if 'Cost' in s]}")


def test_server_json_version_matches_config():
    """server.json drives the public MCP-registry listing — a stale version
    there publishes a release that does not exist."""
    import config
    with open(os.path.join(ROOT, "server.json"), encoding="utf-8") as fh:
        assert json.load(fh)["version"] == config.SERVICE_VERSION
