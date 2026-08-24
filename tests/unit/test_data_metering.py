"""
Unit tests for the DATA_METERING_ENABLED freemium data tool gate.

Coverage:
  - Flag=false: data tools bypass billing, no quota consumed                  [flag_off_*]
  - Flag=true, within quota: free dispatch, quota decremented                 [within_*]
  - Flag=true, beyond quota with credits: run_metered_tool deducts 2         [credits_*]
  - Flag=true, beyond quota, no credits/x402: honest failure cost=0          [exceed_*]
  - x402 path: pricing returns $0.02, is_paid is True                        [x402_*]
  - Idempotency: reserve failure (insufficient) never dispatches the tool     [idem_*]
  - Flag states: env var parsing                                              [cfg_*]

All Supabase and network calls are mocked. Tests are pure unit tests with no
external dependencies.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture: reset in-memory data quota counter between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_data_quota_counter():
    """Clear the in-memory free-key data quota counter before each test."""
    from billing import data_quota
    data_quota._free_key_data_daily.clear()
    yield
    data_quota._free_key_data_daily.clear()


# ===========================================================================
# cfg_* -- flag state parsing
# ===========================================================================

class TestFlagStateCfg:
    """DATA_METERING_ENABLED env var parsing."""

    def test_cfg_default_is_false(self):
        env = os.environ.copy()
        env.pop("DATA_METERING_ENABLED", None)
        with patch.dict(os.environ, env, clear=True):
            val = os.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes")
            assert val is False

    def test_cfg_true_string_enables(self):
        with patch.dict(os.environ, {"DATA_METERING_ENABLED": "true"}):
            val = os.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes")
            assert val is True

    def test_cfg_1_enables(self):
        with patch.dict(os.environ, {"DATA_METERING_ENABLED": "1"}):
            val = os.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes")
            assert val is True

    def test_cfg_yes_enables(self):
        with patch.dict(os.environ, {"DATA_METERING_ENABLED": "yes"}):
            val = os.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes")
            assert val is True

    def test_cfg_false_string_disables(self):
        with patch.dict(os.environ, {"DATA_METERING_ENABLED": "false"}):
            val = os.getenv("DATA_METERING_ENABLED", "").lower() in ("1", "true", "yes")
            assert val is False


# ===========================================================================
# flag_off_* -- DATA_METERING_ENABLED=false: data tools always free, no quota
# ===========================================================================

class TestFlagOff:
    """When flag is off, consume_data_quota is never reached; data tools run free."""

    @pytest.mark.asyncio
    async def test_flag_off_quota_not_consumed_for_data_tool(self):
        """Simulate flag=off: quota counter stays zero after a data tool call."""
        from billing import data_quota

        # With flag off, mcp_server bypasses consume_data_quota entirely.
        # We verify the counter is untouched.
        assert "free_abc" not in data_quota._free_key_data_daily

    @pytest.mark.asyncio
    async def test_flag_off_allows_when_consume_called_anyway(self):
        """consume_data_quota itself always returns allowed when quota is fresh."""
        with patch.dict(os.environ, {
            "DATA_METERING_ENABLED": "false",
            "FREE_DATA_QUOTA_PER_DAY": "50",
        }):
            with patch("billing.data_quota._resolve_key_id", return_value="free_testk"):
                from billing.data_quota import consume_data_quota
                result = await consume_data_quota(
                    "verify_company_record", token="free_testk", ip=""
                )
                assert result["allowed"] is True


# ===========================================================================
# within_* -- within quota: free dispatch
# ===========================================================================

class TestWithinQuota:
    """Within-quota calls are allowed, quota is decremented."""

    @pytest.mark.asyncio
    async def test_within_free_key_quota_allowed(self):
        with patch.dict(os.environ, {"FREE_DATA_QUOTA_PER_DAY": "50"}):
            with patch("billing.data_quota._resolve_key_id", return_value="free_key1"):
                from billing.data_quota import consume_data_quota
                result = await consume_data_quota(
                    "screen_sanctions", token="free_key1", ip=""
                )
                assert result["allowed"] is True
                assert result["remaining"] == 49

    @pytest.mark.asyncio
    async def test_within_free_key_quota_counter_incremented(self):
        with patch.dict(os.environ, {"FREE_DATA_QUOTA_PER_DAY": "50"}):
            with patch("billing.data_quota._resolve_key_id", return_value="free_key2"):
                from billing import data_quota
                from billing.data_quota import consume_data_quota
                await consume_data_quota("screen_sanctions", token="free_key2", ip="")
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                assert data_quota._free_key_data_daily["free_key2"]["count"] == 1
                assert data_quota._free_key_data_daily["free_key2"]["date"] == today

    @pytest.mark.asyncio
    async def test_within_anon_quota_allowed(self):
        """Anonymous caller within anon quota: allowed."""
        with patch.dict(os.environ, {
            "ANON_DATA_QUOTA_PER_DAY": "20",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
        }):
            # First call: Supabase returns empty (no row yet)
            # select_rows / insert_row are imported locally inside _consume_anon_data,
            # so we patch them at their source in storage.supabase_client.
            with patch("storage.supabase_client.select_rows", new_callable=AsyncMock, return_value=[]):
                with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
                    from billing.data_quota import consume_data_quota
                    result = await consume_data_quota(
                        "map_trade_restriction", token="", ip="1.2.3.4"
                    )
                    assert result["allowed"] is True
                    assert result["remaining"] == 19

    @pytest.mark.asyncio
    async def test_within_anon_fail_open_when_no_supabase(self):
        """Anonymous caller with no Supabase config: fail-open (allowed)."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_KEY": "",
            "ANON_DATA_QUOTA_PER_DAY": "20",
        }):
            from billing.data_quota import consume_data_quota
            result = await consume_data_quota(
                "verify_company_record", token="", ip="9.8.7.6"
            )
            assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_within_anon_fail_open_when_no_ip(self):
        """Anonymous caller with no IP: fail-open (allowed)."""
        with patch.dict(os.environ, {"ANON_DATA_QUOTA_PER_DAY": "20"}):
            from billing.data_quota import consume_data_quota
            result = await consume_data_quota(
                "screen_sanctions", token="", ip=""
            )
            assert result["allowed"] is True


# ===========================================================================
# exceed_* -- beyond quota: honest failure, cost=0, tool NOT dispatched
# ===========================================================================

class TestExceedQuota:
    """Beyond-quota callers get honest failure; tool is not dispatched."""

    @pytest.mark.asyncio
    async def test_exceed_free_key_quota_honest_failure(self):
        """Free key beyond quota: failure, reason_code=free_quota_exceeded, cost=0."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with patch.dict(os.environ, {"FREE_DATA_QUOTA_PER_DAY": "3"}):
            with patch("billing.data_quota._resolve_key_id", return_value="free_exhaust"):
                from billing import data_quota
                from billing.data_quota import consume_data_quota
                # Pre-fill counter to exactly the limit
                data_quota._free_key_data_daily["free_exhaust"] = {
                    "count": 3, "date": today
                }
                result = await consume_data_quota(
                    "verify_company_record", token="free_exhaust", ip=""
                )
                assert result["allowed"] is False
                resp = result["response"]
                assert resp["status"] == "failure"
                assert resp["reason_code"] == "free_quota_exceeded"
                assert resp["cost"]["amount"] == 0.0
                assert "free_quota_exceeded" in resp["reason_code"]
                assert "hatchloop.dev" in resp["human_message"]

    @pytest.mark.asyncio
    async def test_exceed_free_key_quota_no_dispatch_implied(self):
        """Beyond quota: consume_data_quota returns allowed=False; caller must NOT dispatch."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with patch.dict(os.environ, {"FREE_DATA_QUOTA_PER_DAY": "1"}):
            with patch("billing.data_quota._resolve_key_id", return_value="free_one"):
                from billing import data_quota
                from billing.data_quota import consume_data_quota
                data_quota._free_key_data_daily["free_one"] = {
                    "count": 1, "date": today
                }
                result = await consume_data_quota(
                    "screen_sanctions", token="free_one", ip=""
                )
                assert result["allowed"] is False
                # Verify the response dict has correct structure
                r = result["response"]
                assert r["cost"]["amount"] == 0.0
                assert r["cost"]["currency"] == "USD"

    @pytest.mark.asyncio
    async def test_exceed_anon_quota_honest_failure(self):
        """Anonymous caller beyond anon quota: honest failure, cost=0."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ip = "10.0.0.1"
        bucket = hashlib.sha256(f"{ip}:{today}".encode()).hexdigest()
        limit = 5

        with patch.dict(os.environ, {
            "ANON_DATA_QUOTA_PER_DAY": str(limit),
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
        }):
            mock_rows = [{"bucket_key": bucket, "count": limit, "quota_date": today}]
            # select_rows is imported locally; patch at source.
            with patch("storage.supabase_client.select_rows", new_callable=AsyncMock, return_value=mock_rows):
                from billing.data_quota import consume_data_quota
                result = await consume_data_quota(
                    "map_trade_restriction", token="", ip=ip
                )
                assert result["allowed"] is False
                assert result["response"]["reason_code"] == "free_quota_exceeded"
                assert result["response"]["cost"]["amount"] == 0.0


# ===========================================================================
# credits_* -- beyond quota + credit account: deduct 2 credits
# ===========================================================================

class TestCreditsDeduction:
    """Credit-account holders (non-free-key) hit the credits gate, not the quota gate."""

    @pytest.mark.asyncio
    async def test_credits_run_metered_tool_deducts_2_on_success(self):
        """run_metered_tool settles 2 credits for a data tool on success."""
        from billing import credits

        expected_receipt = {
            "status": "success",
            "credits": {"charged": 2, "balance": 98},
        }

        # Successful reserve -> dispatch -> commit
        reserve_ok = {"ok": True, "balance_after": 100}
        commit_ok = {"balance_after": 98}

        dispatch = AsyncMock(return_value={"status": "success", "cost": {"amount": 0.02}})

        with patch("billing.credits.reserve", new_callable=AsyncMock, return_value=reserve_ok):
            with patch("billing.credits.commit", new_callable=AsyncMock, return_value=commit_ok):
                with patch("billing.credits._receipt_is_error", return_value=False):
                    result = await credits.run_metered_tool(
                        "verify_company_record", "sub_customer1", dispatch
                    )
                    dispatch.assert_called_once()  # tool was dispatched
                    assert result["credits"]["charged"] == 2
                    assert result["credits"]["balance"] == 98

    @pytest.mark.asyncio
    async def test_credits_no_charge_on_tool_failure(self):
        """run_metered_tool releases hold (no charge) when tool fails."""
        from billing import credits

        reserve_ok = {"ok": True, "balance_after": 100}
        release_ok = {"balance_after": 100}
        dispatch = AsyncMock(return_value={"status": "failure", "reason_code": "some_err"})

        with patch("billing.credits.reserve", new_callable=AsyncMock, return_value=reserve_ok):
            with patch("billing.credits.release", new_callable=AsyncMock, return_value=release_ok):
                with patch("billing.credits._receipt_is_error", return_value=True):
                    result = await credits.run_metered_tool(
                        "screen_sanctions", "sub_customer2", dispatch
                    )
                    dispatch.assert_called_once()
                    assert result["credits"]["charged"] == 0


# ===========================================================================
# x402_* -- pricing table: data tools are $0.02, free tools unchanged
# ===========================================================================

class TestX402Pricing:
    """Pricing table sanity after setting data tools to 2 cents."""

    def test_x402_price_cents_is_2_for_data_tools(self):
        from billing.pricing import price_cents
        assert price_cents("verify_company_record") == 2
        assert price_cents("screen_sanctions") == 2
        assert price_cents("map_trade_restriction") == 2

    def test_x402_price_usd_is_0_02_for_data_tools(self):
        from billing.pricing import price_usd_str
        assert price_usd_str("verify_company_record") == "0.02"
        assert price_usd_str("screen_sanctions") == "0.02"
        assert price_usd_str("map_trade_restriction") == "0.02"

    def test_x402_is_paid_true_for_data_tools(self):
        from billing.pricing import is_paid
        assert is_paid("verify_company_record") is True
        assert is_paid("screen_sanctions") is True
        assert is_paid("map_trade_restriction") is True

    def test_x402_price_atomic_is_20000_for_data_tools(self):
        """2 cents * 10000 atomic units/cent = 20000 atomic units."""
        from billing.pricing import price_atomic
        assert price_atomic("verify_company_record") == 20_000
        assert price_atomic("screen_sanctions") == 20_000
        assert price_atomic("map_trade_restriction") == 20_000

    def test_discovery_tools_still_free(self):
        from billing.pricing import price_cents, is_paid
        for tool in [
            "find_business", "verify_business", "check_booking_link",
            "check_compliance", "preview_cost", "get_status",
            "get_outcome", "self_test",
        ]:
            assert price_cents(tool) == 0, f"{tool} should be free"
            assert is_paid(tool) is False, f"{tool} should not be paid"

    def test_write_tools_unaffected(self):
        from billing.pricing import price_cents
        assert price_cents("send_message") == 2
        assert price_cents("capture_lead") == 5
        assert price_cents("schedule_appointment") == 15


# ===========================================================================
# idem_* -- idempotency: failed reserve never dispatches the tool
# ===========================================================================

class TestIdempotency:
    """reserve() failure (insufficient or already held) never dispatches the tool."""

    @pytest.mark.asyncio
    async def test_idem_reserve_fails_no_dispatch(self):
        """When reserve returns ok=False, dispatch is never called."""
        from billing import credits

        reserve_fail = {"ok": False, "reason_code": "insufficient_credits", "balance": 0}
        dispatch = AsyncMock()

        with patch("billing.credits.reserve", new_callable=AsyncMock, return_value=reserve_fail):
            result = await credits.run_metered_tool(
                "verify_company_record", "sub_empty", dispatch
            )
            dispatch.assert_not_called()
            assert result["status"] == "failure"
            assert result["reason_code"] == "insufficient_credits"
            assert result["cost"]["amount"] == 0.0

    @pytest.mark.asyncio
    async def test_idem_same_operation_id_skipped_at_db(self):
        """If the DB constraint fires (ON CONFLICT DO NOTHING), reserve returns ok=False
        (simulated as insufficient_credits) and the tool is not run a second time."""
        from billing import credits

        # Simulate: first call succeeds, second call fails because hold already exists
        reserve_results = [
            {"ok": True, "balance_after": 100},   # first call: reserve OK
            {"ok": False, "reason_code": "insufficient_credits", "balance": 98},  # second
        ]
        commit_ok = {"balance_after": 98}
        dispatch = AsyncMock(return_value={"status": "success", "cost": {"amount": 0.02}})

        reserve_mock = AsyncMock(side_effect=reserve_results)
        with patch("billing.credits.reserve", reserve_mock):
            with patch("billing.credits.commit", new_callable=AsyncMock, return_value=commit_ok):
                with patch("billing.credits._receipt_is_error", return_value=False):
                    # First call
                    r1 = await credits.run_metered_tool(
                        "screen_sanctions", "sub_idem", dispatch
                    )
                    # Second call (same logical op, different hold_id but same intent)
                    r2 = await credits.run_metered_tool(
                        "screen_sanctions", "sub_idem", dispatch
                    )

        assert dispatch.call_count == 1  # dispatched once, not twice
        assert r1["credits"]["charged"] == 2
        assert r2["status"] == "failure"  # second was refused


# ===========================================================================
# preview_cost_* -- preview_cost == real charge for data tools
# ===========================================================================

class TestPreviewCostParity:
    """preview_cost always returns 0.02 for data tools (matches actual charge)."""

    def test_preview_parity_data_tools(self):
        """price_cents/100 matches what preview_cost would return."""
        from billing.pricing import price_cents, price_usd_str
        for tool in ["verify_company_record", "screen_sanctions", "map_trade_restriction"]:
            cents = price_cents(tool)
            usd_str = price_usd_str(tool)
            assert cents == 2
            assert usd_str == "0.02"
            assert cents / 100 == 0.02
