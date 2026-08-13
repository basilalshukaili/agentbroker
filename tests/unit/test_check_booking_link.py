"""
Unit tests — check_booking_link read-only pre-flight classifier.

Pure/offline: none of these tests touch the network (the handler never fetches
the URL), so they are deterministic.
"""
import asyncio
import json

from core.check_booking_link import handle_check_booking_link
from core.models import OperationStatus
from agent_interface.mcp_server import handle_mcp_request, _build_tool_list


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestCheckBookingLinkHandler:
    def test_supported_calcom_link(self):
        r = run(handle_check_booking_link("https://cal.com/jane"))
        assert r.status == OperationStatus.SUCCESS
        assert r.reason_code == "supported_platform"
        assert r.result["supported"] is True
        assert r.result["importable"] is True
        assert r.result["platform"] == "cal.com"
        assert r.result["expected_channels"] == ["direct_api:calcom"]
        assert r.result["predicted_smb_id"].startswith("smb_imp_")
        # Never claims liveness it did not verify.
        assert r.result["checked_live"] is False
        assert r.cost.basis == "free"

    def test_predicted_smb_id_matches_importer(self):
        """The predicted id must equal what import_booking_url would actually assign."""
        from supply.booking_page_importer import _smb_id_for_url
        url = "https://www.opentable.com/r/acme-bistro"
        r = run(handle_check_booking_link(url))
        assert r.result["supported"] is True
        assert r.result["platform"] == "opentable"
        assert r.result["predicted_smb_id"] == _smb_id_for_url(url)

    def test_unsupported_host_is_successful_but_not_importable(self):
        r = run(handle_check_booking_link("https://example.com/book-now"))
        # A "not supported" answer is a SUCCESSFUL check, not a failure.
        assert r.status == OperationStatus.SUCCESS
        assert r.reason_code == "unsupported_platform"
        assert r.result["supported"] is False
        assert r.result["importable"] is False
        assert r.next_actions  # points at find_business / call_business fallback

    def test_decoy_path_does_not_mislabel_platform(self):
        """A non-allowlisted host with 'cal.com/' in the PATH must not be trusted."""
        r = run(handle_check_booking_link("https://evil.example/cal.com/jane"))
        assert r.result["supported"] is False
        assert r.result["platform"] == "unknown"

    def test_malformed_url_is_bad_input(self):
        r = run(handle_check_booking_link("not-a-url"))
        assert r.status == OperationStatus.FAILURE
        assert r.reason_code == "bad_input"

    def test_importable_verdict_agrees_with_ssrf_allowlist(self):
        """importable=True here must mean the importer's host allowlist accepts it."""
        from supply.booking_page_importer import _host_in_allowlist
        from urllib.parse import urlparse
        for url in ("https://calendly.com/acme/intro", "https://booksy.com/en-us/1_jane"):
            r = run(handle_check_booking_link(url))
            host = urlparse(url).hostname
            assert _host_in_allowlist(host) is True
            assert r.result["importable"] is True


class TestCheckBookingLinkOverMcp:
    def test_listed_as_read_only_idempotent_tool(self):
        tools = {t["name"]: t for t in _build_tool_list()}
        assert "check_booking_link" in tools
        ann = tools["check_booking_link"]["annotations"]
        assert ann["readOnlyHint"] is True
        assert ann["idempotentHint"] is True
        assert ann["destructiveHint"] is False

    def test_callable_via_mcp_tools_call(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {
                "name": "check_booking_link",
                "arguments": {"url": "https://cal.com/jane"},
            },
        }))
        assert "result" in r, f"expected result, got {r}"
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["status"] == "success"
        assert data["result"]["platform"] == "cal.com"

    def test_missing_url_arg_surfaces_named_param_error(self):
        r = run(handle_mcp_request({
            "jsonrpc": "2.0", "id": 43, "method": "tools/call",
            "params": {"name": "check_booking_link", "arguments": {}},
        }))
        assert "error" in r
        assert r["error"]["code"] == -32602
        assert "url" in r["error"]["message"]

    def test_preview_cost_reports_free(self):
        from core.preview_cost import handle_preview_cost
        from core.models import PreviewCostRequest
        resp = run(handle_preview_cost(PreviewCostRequest(
            operation="check_booking_link", params={"url": "https://cal.com/jane"})))
        assert resp.estimated_cost_usd == 0.0
