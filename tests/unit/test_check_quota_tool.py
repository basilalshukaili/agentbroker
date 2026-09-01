"""
Unit tests — check_quota MCP tool (agent_interface/mcp_server._handle_check_quota).

Verifies:
  1. Anonymous caller gets tier='anonymous', nulls for limit/remaining.
  2. Free-key caller gets tier='free' with correct daily_limit, used_today,
     remaining_today derived from the in-memory counter.
  3. Subscription/paid caller gets tier='unlimited'.
  4. Invalid/expired token falls back to anonymous (never raises).
  5. check_quota is wired in the MCP dispatcher (tools/list includes it;
     tools/call routes it correctly).
  6. check_quota is in readOnlyHint and idempotentHint sets.
  7. check_quota does NOT consume the free daily quota when called.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helper: build a valid free-key token for tests
# ---------------------------------------------------------------------------

def _make_free_key_token(key_id: str = "free_testquota") -> str:
    """Issue a free-key JWT for test use."""
    from agent_interface.identity import issue_token, TokenRequest
    resp = issue_token(TokenRequest(
        agent_id=key_id,
        principal_id="test_user_001",
        principal_type="human",
        allowed_operations=["*"],
        budget_cap_usd=0.0,
    ))
    return resp.token


def _make_paid_key_token(key_id: str = "sub_paid001") -> str:
    """Issue a subscription JWT (system principal) for test use."""
    from agent_interface.identity import issue_token, TokenRequest
    resp = issue_token(TokenRequest(
        agent_id=key_id,
        principal_id="cust_enterprise",
        principal_type="system",
        allowed_operations=["*"],
        budget_cap_usd=5000.0,
    ))
    return resp.token


# ---------------------------------------------------------------------------
# _handle_check_quota unit tests
# ---------------------------------------------------------------------------

class TestHandleCheckQuota:
    def setup_method(self):
        # Clear the in-memory free-key daily counter between tests.
        from agent_interface import key_request_logic
        key_request_logic._free_key_daily.clear()

    def test_anonymous_no_token(self):
        from agent_interface.mcp_server import _handle_check_quota
        result = _handle_check_quota("")
        assert result["tier"] == "anonymous"
        assert result["daily_limit"] is None
        assert result["remaining_today"] is None
        assert result["key_id"] is None
        assert "resets" in result

    def test_anonymous_literal_anonymous_token(self):
        from agent_interface.mcp_server import _handle_check_quota
        result = _handle_check_quota("anonymous")
        assert result["tier"] == "anonymous"

    def test_free_key_full_quota(self):
        from agent_interface.mcp_server import _handle_check_quota
        from agent_interface.key_request_logic import FREE_TIER_DAILY_LIMIT
        token = _make_free_key_token("free_testquota_full")
        result = _handle_check_quota(token)
        assert result["tier"] == "free"
        assert result["daily_limit"] == FREE_TIER_DAILY_LIMIT
        assert result["used_today"] == 0
        assert result["remaining_today"] == FREE_TIER_DAILY_LIMIT
        assert result["key_id"] == "free_testquota_full"

    def test_free_key_after_some_ops(self):
        from agent_interface.mcp_server import _handle_check_quota
        from agent_interface.key_request_logic import (
            consume_free_daily, FREE_TIER_DAILY_LIMIT,
        )
        kid = "free_after_ops"
        # Consume 3 ops directly via the quota tracker.
        for _ in range(3):
            consume_free_daily(kid)
        token = _make_free_key_token(kid)
        result = _handle_check_quota(token)
        assert result["tier"] == "free"
        assert result["used_today"] == 3
        assert result["remaining_today"] == FREE_TIER_DAILY_LIMIT - 3

    def test_free_key_exhausted(self):
        from agent_interface.mcp_server import _handle_check_quota
        from agent_interface.key_request_logic import (
            consume_free_daily, FREE_TIER_DAILY_LIMIT,
        )
        kid = "free_exhausted"
        for _ in range(FREE_TIER_DAILY_LIMIT):
            consume_free_daily(kid)
        token = _make_free_key_token(kid)
        result = _handle_check_quota(token)
        assert result["tier"] == "free"
        assert result["remaining_today"] == 0
        assert result["used_today"] == FREE_TIER_DAILY_LIMIT

    def test_paid_subscription_token(self):
        from agent_interface.mcp_server import _handle_check_quota
        token = _make_paid_key_token("sub_enterprise001")
        result = _handle_check_quota(token)
        assert result["tier"] == "unlimited"
        assert result["daily_limit"] is None
        assert result["remaining_today"] is None
        assert result["key_id"] == "sub_enterprise001"

    def test_invalid_token_falls_back_to_anonymous(self):
        from agent_interface.mcp_server import _handle_check_quota
        result = _handle_check_quota("totally.invalid_token_garbage")
        assert result["tier"] == "anonymous"

    def test_check_quota_does_not_consume_free_daily(self):
        """
        Calling check_quota must NEVER decrement the free daily counter.
        A quota-check call is read-only; the quota reflects ops consumed by
        write tools, not by this diagnostic tool.
        """
        from agent_interface.mcp_server import _handle_check_quota
        from agent_interface.key_request_logic import (
            FREE_TIER_DAILY_LIMIT, get_free_daily_remaining,
        )
        kid = "free_not_consumed"
        token = _make_free_key_token(kid)
        # Call check_quota 5 times.
        for _ in range(5):
            _handle_check_quota(token)
        # Counter must still be at the full limit — check_quota consumed nothing.
        assert get_free_daily_remaining(kid) == FREE_TIER_DAILY_LIMIT

    def test_result_always_has_resets_field(self):
        from agent_interface.mcp_server import _handle_check_quota
        for token in ("", "anonymous", _make_free_key_token(), _make_paid_key_token()):
            result = _handle_check_quota(token)
            assert "resets" in result
            assert result["resets"].endswith("00:00:00Z")


# ---------------------------------------------------------------------------
# MCP dispatcher integration: check_quota is routable via tools/call
# ---------------------------------------------------------------------------

class TestCheckQuotaDispatcher:
    def setup_method(self):
        from agent_interface import key_request_logic
        key_request_logic._free_key_daily.clear()

    def test_tools_list_includes_check_quota(self):
        result = asyncio.run(
            __import__("agent_interface.mcp_server", fromlist=["handle_mcp_request"])
            .handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        )
        tool_names = [t["name"] for t in result["result"]["tools"]]
        assert "check_quota" in tool_names

    def test_check_quota_is_read_only_hint(self):
        result = asyncio.run(
            __import__("agent_interface.mcp_server", fromlist=["handle_mcp_request"])
            .handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        )
        tool = next(t for t in result["result"]["tools"] if t["name"] == "check_quota")
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["idempotentHint"] is True
        assert tool["annotations"].get("destructiveHint", False) is False

    def test_check_quota_call_anonymous_via_dispatcher(self):
        """tools/call check_quota with no token returns anonymous tier without error."""
        from agent_interface.mcp_server import handle_mcp_request
        import json
        result = asyncio.run(handle_mcp_request(
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "check_quota", "arguments": {}},
            },
            headers={"user-agent": "TestRunner/1.0"},
        ))
        assert result.get("result") is not None
        assert result["result"].get("isError") is not True
        body = json.loads(result["result"]["content"][0]["text"])
        assert body["tier"] == "anonymous"

    def test_check_quota_call_free_key_via_dispatcher(self):
        """tools/call check_quota with a free key token returns tier='free'."""
        from agent_interface.mcp_server import handle_mcp_request
        import json
        token = _make_free_key_token("free_dispatch_test")
        result = asyncio.run(handle_mcp_request(
            {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "check_quota", "arguments": {}},
            },
            headers={"x-agent-identity": token, "user-agent": "TestRunner/1.0"},
        ))
        assert result.get("result") is not None
        body = json.loads(result["result"]["content"][0]["text"])
        assert body["tier"] == "free"
        assert body["key_id"] == "free_dispatch_test"
