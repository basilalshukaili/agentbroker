"""
Tests for the MCP server and the multi-protocol discovery surfaces.
These verify that agents can actually find and call us through every protocol.
"""
import asyncio
import json
import pytest

from agent_interface.mcp_server import handle_mcp_request, _build_tool_list
from agent_interface.well_known import (
    get_ai_plugin_manifest, get_openai_tools, get_anthropic_tools,
    get_agents_json, get_mcp_descriptor, get_llms_txt, get_llms_full_txt,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# MCP server tests
# ---------------------------------------------------------------------------

class TestMcpServer:

    def test_initialize_returns_protocol_version(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }))
        assert r["jsonrpc"] == "2.0"
        assert r["id"] == 1
        assert "protocolVersion" in r["result"]
        assert r["result"]["serverInfo"]["name"] == "agent-broker"
        assert "tools" in r["result"]["capabilities"]

    def test_tools_list_returns_all_tools(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }))
        tools = r["result"]["tools"]
        assert len(tools) >= 20
        names = {t["name"] for t in tools}
        assert names == {
            "find_business", "verify_business", "send_message", "capture_lead",
            "schedule_appointment", "send_transactional_confirmation", "handle_inbound",
            "escalate_to_human", "get_status", "get_outcome", "preview_cost", "self_test",
            "import_booking_url", "call_business", "check_booking_link", "check_compliance",
            "verify_company_record", "screen_sanctions", "map_trade_restriction",
            "get_conversation",
        }

    def test_every_tool_has_input_schema_and_annotations(self):
        tools = _build_tool_list()
        for tool in tools:
            assert "inputSchema" in tool, f"{tool['name']} missing inputSchema"
            assert "annotations" in tool, f"{tool['name']} missing annotations"
            ann = tool["annotations"]
            assert "readOnlyHint" in ann
            assert "destructiveHint" in ann

    def test_readonly_tools_marked_correctly(self):
        tools = {t["name"]: t for t in _build_tool_list()}
        assert tools["find_business"]["annotations"]["readOnlyHint"] is True
        assert tools["preview_cost"]["annotations"]["readOnlyHint"] is True
        assert tools["schedule_appointment"]["annotations"]["readOnlyHint"] is False
        assert tools["schedule_appointment"]["annotations"]["destructiveHint"] is True

    def test_tools_call_find_business(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "find_business",
                "arguments": {
                    "vertical": "personal_services",
                    "location": {"zip_or_city": "30309"},
                    "capability": "haircut",
                },
            },
        }))
        assert "result" in r, f"Expected result, got: {r}"
        content = r["result"]["content"][0]["text"]
        data = json.loads(content)
        assert data["status"] == "success"
        assert data["result"]["businesses"]

    def test_tools_call_preview_cost_is_free(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "preview_cost",
                "arguments": {"operation": "self_test", "params": {}},
            },
        }))
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["estimated_cost_usd"] == 0.0

    def test_tools_call_unknown_tool(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }))
        assert "error" in r
        assert r["error"]["code"] == -32602  # invalid params

    def test_method_not_found(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/banana", "params": {},
        }))
        assert r["error"]["code"] == -32601

    def test_resources_list(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {},
        }))
        resources = r["result"]["resources"]
        assert len(resources) >= 3
        uris = {res["uri"] for res in resources}
        # Both legacy `smb-broker://` and canonical `agent-broker://` schemes
        # are read by resources/read; tools/list returns the canonical scheme.
        assert any("manifest" in u for u in uris)

    def test_prompts_list(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 8, "method": "prompts/list", "params": {},
        }))
        prompts = r["result"]["prompts"]
        assert len(prompts) >= 3
        names = {p["name"] for p in prompts}
        # We renamed prompts to be agent-pickable for the URL-first flow
        assert "book_from_any_url" in names or "book_appointment_workflow" in names

    def test_ping(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 9, "method": "ping", "params": {},
        }))
        assert r["result"] == {}


# ---------------------------------------------------------------------------
# .well-known endpoints
# ---------------------------------------------------------------------------

class TestWellKnownEndpoints:

    def test_ai_plugin_has_required_fields(self):
        m = get_ai_plugin_manifest()
        assert m["schema_version"] == "v1"
        assert m["name_for_model"] == "agent_broker"
        assert "description_for_model" in m
        assert m["api"]["type"] == "openapi"

    def test_openai_tools_has_12_tools(self):
        d = get_openai_tools()
        assert len(d["tools"]) >= 13
        for t in d["tools"]:
            assert t["type"] == "function"
            assert "function" in t
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_anthropic_tools_has_12_tools(self):
        d = get_anthropic_tools()
        assert len(d["tools"]) >= 13
        for t in d["tools"]:
            assert "name" in t
            assert "description" in t
            assert "input_schema" in t

    def test_agents_json_has_skills_and_capabilities(self):
        a = get_agents_json()
        assert "skills" in a
        assert len(a["skills"]) >= 13
        assert a["capabilities"]["streaming"] is True
        assert a["capabilities"]["push_notifications"] is True
        assert "mcp" in a["supported_protocols"]
        assert "openai-tools" in a["supported_protocols"]

    def test_mcp_descriptor_points_to_mcp_endpoint(self):
        d = get_mcp_descriptor()
        assert d["transport"]["endpoint"].endswith("/mcp")
        assert d["transport"]["method"] == "POST"

    def test_llms_txt_includes_all_operations(self):
        txt = get_llms_txt()
        for op in ("find_business", "verify_business", "send_message", "capture_lead",
                   "schedule_appointment", "send_transactional_confirmation",
                   "handle_inbound", "escalate_to_human", "get_status",
                   "get_outcome", "preview_cost", "self_test"):
            assert f"### {op}" in txt, f"{op} missing from llms.txt"

    def test_llms_full_txt_includes_input_schemas(self):
        txt = get_llms_full_txt()
        assert "Input Schema" in txt
        assert "Output Schema" in txt
        assert "Examples" in txt


# ---------------------------------------------------------------------------
# Pricing tier sanity
# ---------------------------------------------------------------------------

class TestPricingTiers:

    def test_subscription_tiers_present(self):
        from billing.pricing_tiers import list_tiers, SubscriptionTier
        tiers = list_tiers()
        assert len(tiers) == 4
        names = {t.tier for t in tiers}
        assert names == {SubscriptionTier.FREE, SubscriptionTier.DEVELOPER,
                         SubscriptionTier.BUSINESS, SubscriptionTier.ENTERPRISE}

    def test_listing_tiers_have_exclusive_with_scarcity(self):
        from billing.pricing_tiers import list_listing_tiers, ListingTier
        tiers = {t.tier: t for t in list_listing_tiers()}
        assert tiers[ListingTier.EXCLUSIVE].exclusivity is True
        assert tiers[ListingTier.FEATURED].exclusivity is False
        assert tiers[ListingTier.EXCLUSIVE].monthly_fee_usd > tiers[ListingTier.FEATURED].monthly_fee_usd

    def test_outcome_premium_for_schedule_appointment(self):
        from billing.pricing_tiers import get_outcome_premium
        op = get_outcome_premium("schedule_appointment")
        assert op is not None
        assert op.base_cost_usd + op.success_premium_usd == 1.00

    def test_revenue_forecast_year1_positive(self):
        from billing.pricing_tiers import forecast_revenue
        r = forecast_revenue(
            free_agents=500, dev_agents=80, business_agents=15, enterprise_agents=2,
            avg_enterprise_monthly_usd=5000,
            free_smbs=2000, verified_smbs=120, featured_smbs=30, exclusive_smbs=5,
            avg_outcome_premium_per_op_usd=0.45, monthly_outcome_ops=8000,
        )
        assert r["total_monthly_usd"] > 30000
        assert r["total_annual_usd"] > 400000
