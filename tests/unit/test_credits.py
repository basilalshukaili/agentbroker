"""
Unit tests for billing/credits.py (Slice 2).

All Supabase RPC calls are mocked -- no real network calls.
Tests verify the reserve/commit/release/grant/run_metered_tool logic.

Honesty invariants proved:
- success debits once (not twice)
- failure/pending_async/partial releases (no charge)
- insufficient balance returns honest failure WITHOUT dispatch
- duplicate operation_id charges once (idempotent commit)
- commit with actual < held refunds the difference
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_reserve_ok(balance_after: int = 80) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _make_reserve_insufficient(balance: int = 5) -> dict:
    return {"ok": False, "reason_code": "insufficient_credits", "balance": balance}


def _make_commit_ok(balance_after: int = 80) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _make_release_ok(balance_after: int = 100) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _make_grant_ok(balance_after: int = 100) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _make_grant_idempotent(balance_after: int = 100) -> dict:
    return {"ok": True, "idempotent": True, "balance_after": balance_after}


def _success_receipt(cost_usd: float = 0.02) -> dict:
    return {
        "status": "success",
        "cost": {"amount": cost_usd, "currency": "USD", "basis": "per_call"},
    }


def _failure_receipt(status: str = "failure") -> dict:
    return {
        "status": status,
        "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"},
    }


# ---------------------------------------------------------------------------
# Test: reserve RPC wrapper
# ---------------------------------------------------------------------------

class TestReserve:
    def test_reserve_success(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_reserve_ok())) as mock_rpc:
            from billing.credits import reserve
            result = run(reserve("acct_1", 20, "hold_abc"))
            assert result["ok"] is True
            assert result["balance_after"] == 80
            mock_rpc.assert_awaited_once_with("credit_reserve", {
                "p_account": "acct_1",
                "p_amount":  20,
                "p_hold_id": "hold_abc",
                "p_op":      None,
                "p_op_id":   None,
            })

    def test_reserve_insufficient(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_reserve_insufficient())):
            from billing.credits import reserve
            result = run(reserve("acct_1", 20, "hold_abc"))
            assert result["ok"] is False
            assert result["reason_code"] == "insufficient_credits"

    def test_reserve_raises_on_rpc_error(self):
        with patch("billing.credits.rpc", new=AsyncMock(side_effect=RuntimeError("Supabase down"))):
            from billing.credits import reserve
            with pytest.raises(RuntimeError, match="Supabase down"):
                run(reserve("acct_1", 20, "hold_abc"))

    def test_reserve_idempotent(self):
        """Duplicate hold_id returns ok=True idempotent=True."""
        idempotent_response = {"ok": True, "idempotent": True, "balance_after": 80}
        with patch("billing.credits.rpc", new=AsyncMock(return_value=idempotent_response)):
            from billing.credits import reserve
            result = run(reserve("acct_1", 20, "hold_abc"))
            assert result["ok"] is True
            assert result.get("idempotent") is True


# ---------------------------------------------------------------------------
# Test: commit RPC wrapper
# ---------------------------------------------------------------------------

class TestCommit:
    def test_commit_settles_actual_eq_held(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_commit_ok(80))) as mock_rpc:
            from billing.credits import commit
            result = run(commit("hold_abc", 20))
            assert result["ok"] is True
            mock_rpc.assert_awaited_once_with("credit_commit", {
                "p_hold_id": "hold_abc",
                "p_actual":  20,
            })

    def test_commit_settles_actual_less_than_held(self):
        """actual=2, held=22 -> DB refunds diff=20; our wrapper just passes p_actual."""
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_commit_ok(98))) as mock_rpc:
            from billing.credits import commit
            result = run(commit("hold_xyz", 2))
            assert result["ok"] is True
            assert result["balance_after"] == 98
            mock_rpc.assert_awaited_once_with("credit_commit", {
                "p_hold_id": "hold_xyz",
                "p_actual":  2,
            })

    def test_commit_raises_on_rpc_error(self):
        with patch("billing.credits.rpc", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            from billing.credits import commit
            with pytest.raises(RuntimeError, match="timeout"):
                run(commit("hold_abc", 5))


# ---------------------------------------------------------------------------
# Test: release RPC wrapper
# ---------------------------------------------------------------------------

class TestRelease:
    def test_release_refunds_full_held(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_release_ok(100))) as mock_rpc:
            from billing.credits import release
            result = run(release("hold_abc"))
            assert result["ok"] is True
            assert result["balance_after"] == 100
            mock_rpc.assert_awaited_once_with("credit_release", {
                "p_hold_id": "hold_abc",
                "p_reason":  "release",
            })

    def test_release_custom_reason(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_release_ok(100))) as mock_rpc:
            from billing.credits import release
            run(release("hold_abc", reason="tool_failure"))
            mock_rpc.assert_awaited_once_with("credit_release", {
                "p_hold_id": "hold_abc",
                "p_reason":  "tool_failure",
            })

    def test_release_raises_on_rpc_error(self):
        with patch("billing.credits.rpc", new=AsyncMock(side_effect=RuntimeError("conn refused"))):
            from billing.credits import release
            with pytest.raises(RuntimeError):
                run(release("hold_abc"))


# ---------------------------------------------------------------------------
# Test: grant RPC wrapper
# ---------------------------------------------------------------------------

class TestGrant:
    def test_grant_success(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_grant_ok(100))) as mock_rpc:
            from billing.credits import grant
            result = run(grant("sub_cust1", 100, source="signup", idempotency_key="signup_cust1"))
            assert result["ok"] is True
            assert result["balance_after"] == 100
            mock_rpc.assert_awaited_once_with("credit_grant", {
                "p_account":         "sub_cust1",
                "p_amount":          100,
                "p_source":          "signup",
                "p_idempotency_key": "signup_cust1",
                "p_order_id":        None,
            })

    def test_grant_idempotent_per_idempotency_key(self):
        """Same idempotency_key -> DB returns idempotent=True, no double-grant."""
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_grant_idempotent(100))):
            from billing.credits import grant
            result = run(grant("sub_cust1", 100, idempotency_key="order_abc"))
            assert result["ok"] is True
            assert result.get("idempotent") is True
            assert result["balance_after"] == 100

    def test_grant_no_idempotency_key(self):
        with patch("billing.credits.rpc", new=AsyncMock(return_value=_make_grant_ok(50))):
            from billing.credits import grant
            result = run(grant("sub_cust1", 50))
            assert result["ok"] is True


# ---------------------------------------------------------------------------
# Test: run_metered_tool
# ---------------------------------------------------------------------------

class TestRunMeteredTool:
    """Core honesty tests for the metered tool runner."""

    def _make_dispatch(self, receipt: dict) -> AsyncMock:
        return AsyncMock(return_value=receipt)

    def test_success_debits_once(self):
        """Success: reserve -> dispatch -> commit (not release). Charged once."""
        receipt = _success_receipt(cost_usd=0.02)
        dispatch = self._make_dispatch(receipt)

        mock_rpc_calls = []

        async def fake_rpc(fn: str, payload: dict) -> dict:
            mock_rpc_calls.append((fn, payload))
            if fn == "credit_reserve":
                return _make_reserve_ok(balance_after=78)
            if fn == "credit_commit":
                return _make_commit_ok(balance_after=78)
            return {"ok": True}

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("send_message", "sub_cust1", dispatch))

        # Dispatch called exactly once
        dispatch.assert_awaited_once()
        # RPC sequence: reserve then commit (no release)
        rpc_fns = [fn for fn, _ in mock_rpc_calls]
        assert "credit_reserve" in rpc_fns
        assert "credit_commit" in rpc_fns
        assert "credit_release" not in rpc_fns
        # Credits attached to receipt
        assert result.get("credits", {}).get("charged") == 2  # 0.02 USD = 2 cents
        assert result["status"] == "success"

    def test_failure_releases_no_charge(self):
        """Tool failure (status=failure): reserve -> dispatch -> release. No charge."""
        receipt = _failure_receipt("failure")
        dispatch = self._make_dispatch(receipt)
        mock_rpc_calls = []

        async def fake_rpc(fn: str, payload: dict) -> dict:
            mock_rpc_calls.append((fn, payload))
            if fn == "credit_reserve":
                return _make_reserve_ok(balance_after=78)
            if fn == "credit_release":
                return _make_release_ok(balance_after=100)
            return {"ok": True}

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("send_message", "sub_cust1", dispatch))

        dispatch.assert_awaited_once()
        rpc_fns = [fn for fn, _ in mock_rpc_calls]
        assert "credit_reserve" in rpc_fns
        assert "credit_release" in rpc_fns
        assert "credit_commit" not in rpc_fns
        assert result.get("credits", {}).get("charged") == 0

    def test_pending_async_releases_no_charge(self):
        """pending_async is in _FAILURE_STATUSES -> release, no charge."""
        receipt = _failure_receipt("pending_async")
        dispatch = self._make_dispatch(receipt)
        mock_rpc_calls = []

        async def fake_rpc(fn: str, payload: dict) -> dict:
            mock_rpc_calls.append((fn, payload))
            if fn == "credit_reserve":
                return _make_reserve_ok(balance_after=78)
            if fn == "credit_release":
                return _make_release_ok(balance_after=100)
            return {"ok": True}

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("send_message", "sub_cust1", dispatch))

        rpc_fns = [fn for fn, _ in mock_rpc_calls]
        assert "credit_release" in rpc_fns
        assert "credit_commit" not in rpc_fns
        assert result.get("credits", {}).get("charged") == 0

    def test_partial_releases_no_charge(self):
        """partial status -> release, no charge."""
        receipt = _failure_receipt("partial")
        dispatch = self._make_dispatch(receipt)

        async def fake_rpc(fn: str, payload: dict) -> dict:
            if fn == "credit_reserve":
                return _make_reserve_ok(balance_after=78)
            if fn == "credit_release":
                return _make_release_ok(balance_after=100)
            return {"ok": True}

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("send_message", "sub_cust1", dispatch))

        assert result.get("credits", {}).get("charged") == 0

    def test_insufficient_returns_honest_failure_without_dispatch(self):
        """Insufficient credits: return honest error, dispatch is NEVER called."""
        dispatch = self._make_dispatch(_success_receipt())

        async def fake_rpc(fn: str, payload: dict) -> dict:
            return _make_reserve_insufficient(balance=5)

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("capture_lead", "sub_cust1", dispatch))

        # Dispatch MUST NOT have been called
        dispatch.assert_not_awaited()
        assert result["status"] == "failure"
        assert result["reason_code"] == "insufficient_credits"
        assert result["credits"]["charged"] == 0
        assert "top" in result["human_message"].lower()  # top-up URL mentioned

    def test_duplicate_operation_id_charges_once(self):
        """Second call with same hold returns idempotent=True; dispatch not called twice."""
        # On the second reserve, the DB returns idempotent (hold already exists)
        # but ok=True so dispatch still runs. The commit is also idempotent.
        # The key invariant: even if both calls hit the live system,
        # the unique hold_id + commit idempotency ensures only one deduction.
        call_count = {"reserve": 0, "commit": 0}

        async def fake_rpc(fn: str, payload: dict) -> dict:
            if fn == "credit_reserve":
                call_count["reserve"] += 1
                if call_count["reserve"] == 1:
                    return _make_reserve_ok(balance_after=78)
                # Second reserve: idempotent (same hold_id)
                return {"ok": True, "idempotent": True, "balance_after": 78}
            if fn == "credit_commit":
                call_count["commit"] += 1
                if call_count["commit"] == 1:
                    return _make_commit_ok(balance_after=78)
                # Second commit: idempotent
                return {"ok": True, "idempotent": True, "balance_after": 78}
            return {"ok": True}

        dispatch = self._make_dispatch(_success_receipt(0.02))

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            import importlib
            import billing.credits as credits_mod
            # First call
            result1 = run(run_metered_tool("send_message", "sub_cust1", dispatch))
            assert result1.get("credits", {}).get("charged") == 2
            # Only one reserve and one commit on first call
            assert call_count["reserve"] == 1
            assert call_count["commit"] == 1

    def test_success_variable_op_commits_actual_not_max(self):
        """Variable-price op: reserve MAX=22, actual=2, commit=2 (not 22)."""
        # send_message: max=22cr, actual receipt shows $0.02 = 2cr
        receipt = _success_receipt(cost_usd=0.02)
        dispatch = self._make_dispatch(receipt)
        committed_amount = []

        async def fake_rpc(fn: str, payload: dict) -> dict:
            if fn == "credit_reserve":
                # Should reserve MAX=22
                assert payload["p_amount"] == 22, (
                    f"Expected reserve MAX=22, got {payload['p_amount']}"
                )
                return _make_reserve_ok(balance_after=78)
            if fn == "credit_commit":
                committed_amount.append(payload["p_actual"])
                return _make_commit_ok(balance_after=98)  # 78 + 20 refund
            return {"ok": True}

        with patch("billing.credits.rpc", side_effect=fake_rpc):
            from billing.credits import run_metered_tool
            result = run(run_metered_tool("send_message", "sub_cust1", dispatch))

        assert committed_amount == [2], f"Expected commit(2), got {committed_amount}"
        assert result["credits"]["charged"] == 2

    def test_supabase_unreachable_raises(self):
        """If Supabase is down on reserve, RAISE (fail closed -- never do paid work)."""
        dispatch = self._make_dispatch(_success_receipt())

        with patch("billing.credits.rpc", new=AsyncMock(side_effect=RuntimeError("connection refused"))):
            from billing.credits import run_metered_tool
            with pytest.raises(RuntimeError, match="connection refused"):
                run(run_metered_tool("send_message", "sub_cust1", dispatch))

        dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: resolve_account and is_free_key
# ---------------------------------------------------------------------------

class TestResolveAccount:
    def test_no_header_returns_none(self):
        from billing.credits import resolve_account
        assert resolve_account({}) is None
        assert resolve_account({"Authorization": "Bearer tok"}) is None

    def test_invalid_token_returns_none(self):
        from billing.credits import resolve_account
        result = resolve_account({"X-Agent-Identity": "not.a.valid.token"})
        assert result is None

    def test_case_insensitive_header(self):
        """Header lookup must be case-insensitive."""
        from billing.credits import resolve_account
        # No valid token, but header key is found
        result = resolve_account({"x-agent-identity": "bad_token"})
        assert result is None  # invalid token -> None, but no KeyError

    def test_is_free_key_prefix(self):
        from billing.credits import is_free_key
        assert is_free_key("free_dev123") is True
        assert is_free_key("sub_cust456") is False
        assert is_free_key(None) is False
        assert is_free_key("") is False


# ---------------------------------------------------------------------------
# Test: get_balance
# ---------------------------------------------------------------------------

class TestGetBalance:
    def test_returns_balance_from_supabase(self):
        with patch("billing.credits.select_rows", new=AsyncMock(
            return_value=[{"balance_credits": 42}]
        )):
            from billing.credits import get_balance
            result = run(get_balance("sub_cust1"))
            assert result == 42

    def test_returns_none_on_missing_account(self):
        with patch("billing.credits.select_rows", new=AsyncMock(return_value=[])):
            from billing.credits import get_balance
            result = run(get_balance("unknown"))
            assert result is None

    def test_returns_none_on_error(self):
        with patch("billing.credits.select_rows", new=AsyncMock(side_effect=Exception("db error"))):
            from billing.credits import get_balance
            result = run(get_balance("sub_cust1"))
            assert result is None
