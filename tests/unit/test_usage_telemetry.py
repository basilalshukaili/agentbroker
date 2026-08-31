"""
Unit tests — usage telemetry (billing/usage_logger.py) and free-key flow
(agent_interface/key_requests.py).
"""
import pytest
import hashlib
import hmac
import base64
import time
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# usage_logger: classify_session_kind
# ---------------------------------------------------------------------------

class TestClassifySessionKind:
    def setup_method(self):
        from billing.usage_logger import classify_session_kind
        self.classify = classify_session_kind

    def test_glama_bot_is_crawler(self):
        result = self.classify("tools/call", "find_business", "Glama-Bot/1.0", None)
        assert result == "crawler"

    def test_smithery_bot_is_crawler(self):
        result = self.classify("initialize", None, "smithery-crawler/2.0", None)
        assert result == "crawler"

    def test_pulsemcp_bot_is_crawler(self):
        result = self.classify("tools/list", None, "PulseMCP-Index/1", None)
        assert result == "crawler"

    def test_initialize_only_is_crawler(self):
        result = self.classify("initialize", None, "Mozilla/5.0", None)
        assert result == "crawler"

    def test_tools_list_only_is_crawler(self):
        result = self.classify("tools/list", None, "Mozilla/5.0", None)
        assert result == "crawler"

    def test_tools_call_without_key_is_anon_agent(self):
        result = self.classify("tools/call", "send_message", "Claude/1.0", None)
        assert result == "anon_agent"

    def test_tools_call_with_anonymous_key_is_anon_agent(self):
        result = self.classify("tools/call", "find_business", "Claude/1.0", "anonymous")
        assert result == "anon_agent"

    def test_tools_call_with_key_is_verified_human(self):
        result = self.classify("tools/call", "send_message", "Claude/1.0", "sub_abc123")
        assert result == "verified_human_key"

    def test_curl_is_crawler(self):
        result = self.classify("tools/call", "find_business", "curl/7.88.0", None)
        assert result == "crawler"

    def test_python_requests_is_crawler(self):
        result = self.classify("tools/list", None, "python-requests/2.28.0", None)
        assert result == "crawler"

    # --- human vs agent principal distinction (2026-09-01) ---

    def test_system_principal_is_verified_agent_key(self):
        """A tools/call with a valid key AND principal_type='system' is 'verified_agent_key'."""
        result = self.classify("tools/call", "send_message", "Claude/1.0",
                               "sub_abc123", principal_type="system")
        assert result == "verified_agent_key"

    def test_business_principal_is_verified_agent_key(self):
        """'business' is the PrincipalKind value for system principals after validate_token mapping."""
        result = self.classify("tools/call", "find_business", "Claude/1.0",
                               "sub_xyz", principal_type="business")
        assert result == "verified_agent_key"

    def test_human_principal_is_verified_human_key(self):
        """A tools/call with a valid key AND principal_type='human' stays 'verified_human_key'."""
        result = self.classify("tools/call", "preview_cost", "Mozilla/5.0",
                               "sub_human999", principal_type="human")
        assert result == "verified_human_key"

    def test_none_principal_type_defaults_to_verified_human_key(self):
        """Older tokens without a principal type default to 'verified_human_key' (backward compat)."""
        result = self.classify("tools/call", "send_message", "Claude/1.0",
                               "sub_oldtoken", principal_type=None)
        assert result == "verified_human_key"

    def test_system_principal_on_non_work_method_is_crawler(self):
        """Even with a system key, initialize is a non-work method -> crawler."""
        result = self.classify("initialize", None, "Claude/1.0",
                               "sub_agent001", principal_type="system")
        assert result == "crawler"


# ---------------------------------------------------------------------------
# usage_logger: hash helpers
# ---------------------------------------------------------------------------

class TestHashHelpers:
    def test_ip_hash_is_8_chars(self):
        from billing.usage_logger import _hash8
        h = _hash8("192.168.1.1")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_ip_same_hash(self):
        from billing.usage_logger import _hash8
        assert _hash8("10.0.0.1") == _hash8("10.0.0.1")

    def test_different_ips_different_hashes(self):
        from billing.usage_logger import _hash8
        assert _hash8("10.0.0.1") != _hash8("10.0.0.2")


# ---------------------------------------------------------------------------
# usage_logger: fire_log_usage (no-op when no event loop)
# ---------------------------------------------------------------------------

class TestFireLogUsage:
    def test_fire_log_usage_does_not_raise_without_loop(self):
        from billing.usage_logger import fire_log_usage
        # Should silently pass when no async loop is running
        fire_log_usage("tools/call", "find_business", {"vertical": "personal_services"},
                       "1.2.3.4", "Mozilla/5.0", "anonymous")

    def test_fire_log_usage_does_not_raise_with_none_args(self):
        from billing.usage_logger import fire_log_usage
        fire_log_usage("initialize", None, None, None, None, None)


# ---------------------------------------------------------------------------
# key_requests: token signing
# ---------------------------------------------------------------------------

class TestVerifyToken:
    def test_valid_token_returns_email(self):
        from agent_interface.key_request_logic import make_verify_token as _make_verify_token, verify_token as _verify_token
        token, _ = _make_verify_token("test@example.com")
        result = _verify_token(token)
        assert result == "test@example.com"

    def test_tampered_token_returns_none(self):
        from agent_interface.key_request_logic import make_verify_token as _make_verify_token, verify_token as _verify_token
        token, _ = _make_verify_token("test@example.com")
        # Flip a character in the sig portion
        parts = token.split(".")
        tampered = parts[0] + "." + parts[1][:-1] + ("a" if parts[1][-1] != "a" else "b")
        assert _verify_token(tampered) is None

    def test_expired_token_returns_none(self):
        from agent_interface.key_request_logic import verify_token as _verify_token, _VERIFY_SECRET
        # Build a token that expired 1 second ago
        expired_at = time.time() - 1
        payload = f"expired@example.com|{expired_at}"
        sig = hmac.new(_VERIFY_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        token = f"{b64}.{sig}"
        assert _verify_token(token) is None

    def test_malformed_token_returns_none(self):
        from agent_interface.key_request_logic import verify_token as _verify_token
        assert _verify_token("notavalidtoken") is None
        assert _verify_token("") is None
        assert _verify_token("a.b.c") is None


# ---------------------------------------------------------------------------
# key_requests: free-tier daily rate limiter
# ---------------------------------------------------------------------------

class TestFreeTierDailyLimit:
    def setup_method(self):
        # Clear the in-memory counters between tests
        from agent_interface import key_request_logic
        key_request_logic._free_key_daily.clear()

    def test_first_call_is_allowed(self):
        from agent_interface.key_request_logic import consume_free_daily
        assert consume_free_daily("free_testkey001") is True

    def test_up_to_limit_is_allowed(self):
        from agent_interface.key_request_logic import consume_free_daily, FREE_TIER_DAILY_LIMIT
        key = "free_limitkey"
        for i in range(FREE_TIER_DAILY_LIMIT):
            result = consume_free_daily(key)
            assert result is True, f"Call {i+1} should be allowed"

    def test_over_limit_is_denied(self):
        from agent_interface.key_request_logic import consume_free_daily, FREE_TIER_DAILY_LIMIT
        key = "free_denykey"
        for _ in range(FREE_TIER_DAILY_LIMIT):
            consume_free_daily(key)
        # Next call should be denied
        assert consume_free_daily(key) is False

    def test_remaining_decrements(self):
        from agent_interface.key_request_logic import consume_free_daily, get_free_daily_remaining, FREE_TIER_DAILY_LIMIT
        key = "free_remkey"
        assert get_free_daily_remaining(key) == FREE_TIER_DAILY_LIMIT
        consume_free_daily(key)
        assert get_free_daily_remaining(key) == FREE_TIER_DAILY_LIMIT - 1

    def test_is_free_key_detects_prefix(self):
        from agent_interface.key_request_logic import is_free_key
        assert is_free_key("free_abc123") is True
        assert is_free_key("sub_abc123") is False
        assert is_free_key(None) is False
        assert is_free_key("anonymous") is False

    def test_different_keys_independent_limits(self):
        from agent_interface.key_request_logic import consume_free_daily, FREE_TIER_DAILY_LIMIT
        key_a = "free_key_a"
        key_b = "free_key_b"
        for _ in range(FREE_TIER_DAILY_LIMIT):
            consume_free_daily(key_a)
        # key_a exhausted, key_b still fresh
        assert consume_free_daily(key_a) is False
        assert consume_free_daily(key_b) is True


# ---------------------------------------------------------------------------
# key_requests: /keys/request and /keys/verify via FastAPI TestClient
# ---------------------------------------------------------------------------

class TestKeyRequestEndpoints:
    """
    FastAPI endpoint tests — skipped when fastapi is not installed in the
    test environment (Render/prod has it; local test env may not).
    """
    def setup_method(self):
        from agent_interface import key_request_logic
        key_request_logic._free_key_daily.clear()

    def _get_client(self):
        pytest.importorskip("fastapi", reason="fastapi not installed in test environment")
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from agent_interface.key_requests import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_request_with_invalid_email_returns_400(self):
        client = self._get_client()
        resp = client.post("/keys/request", json={"email": "notanemail"})
        assert resp.status_code == 400
        assert "invalid_email" in resp.json()["error"]

    def test_request_with_valid_email_returns_200(self):
        with patch("agent_interface.key_request_logic.store_pending", new=AsyncMock()), \
             patch("agent_interface.key_request_logic.send_verification_email", new=AsyncMock()):
            client = self._get_client()
            resp = client.post("/keys/request", json={"email": "test@example.com"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "verification_sent"

    def test_verify_with_valid_token_returns_200_html(self):
        from agent_interface.key_request_logic import make_verify_token
        token, _ = make_verify_token("hello+test@hatchloop.dev")
        with patch("agent_interface.key_request_logic.consume_pending", new=AsyncMock(return_value="hello+test@hatchloop.dev")), \
             patch("agent_interface.key_request_logic.send_key_email", new=AsyncMock()):
            client = self._get_client()
            resp = client.get(f"/keys/verify?token={token}")
            assert resp.status_code == 200
            assert "X-Agent-Identity" in resp.text
            assert "free" in resp.text.lower()

    def test_verify_with_invalid_token_returns_400(self):
        client = self._get_client()
        resp = client.get("/keys/verify?token=totallyinvalidtoken")
        assert resp.status_code == 400
        assert "invalid" in resp.text.lower() or "expired" in resp.text.lower()


# ---------------------------------------------------------------------------
# MCP dispatcher: telemetry is wired (smoke test — no Supabase call)
# ---------------------------------------------------------------------------

class TestMCPTelemetryWiring:
    def test_fire_called_on_initialize(self):
        """After handle_mcp_request, fire_log_usage should have been scheduled."""
        import asyncio
        from unittest.mock import patch as mpatch
        from agent_interface.mcp_server import handle_mcp_request

        fired = []

        def fake_fire(method, tool_name, arguments, ip, ua, key_id, **kwargs):
            fired.append({"method": method, "tool": tool_name})

        with mpatch("billing.usage_logger.fire_log_usage", fake_fire):
            result = asyncio.run(
                handle_mcp_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "test", "version": "1"}}},
                    headers={"user-agent": "TestRunner/1.0"},
                )
            )
        assert result.get("result") is not None
        assert len(fired) == 1
        assert fired[0]["method"] == "initialize"

    def test_fire_called_on_tools_list(self):
        import asyncio
        from unittest.mock import patch as mpatch
        from agent_interface.mcp_server import handle_mcp_request

        fired = []

        def fake_fire(method, tool_name, arguments, ip, ua, key_id, **kwargs):
            fired.append(method)

        with mpatch("billing.usage_logger.fire_log_usage", fake_fire):
            result = asyncio.run(
                handle_mcp_request(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
            )
        assert "tools" in result.get("result", {})
        assert "tools/list" in fired
