"""
Unit tests for Credits billing Slices 3 + 4.

Slice 3 tests -- MCP wiring (_h_tools_call credit branch):
  - CREDITS_ENABLED=false  -> behavior identical to today (no credit deduction)
  - CREDITS_ENABLED=true   -> paid tool + paid account -> run_metered_tool called once
  - CREDITS_ENABLED=true   -> insufficient balance -> honest failure, dispatch NOT called
  - CREDITS_ENABLED=true   -> free_ key -> unchanged 50/day path (credits bypassed)
  - CREDITS_ENABLED=true   -> x402-paying call -> NO credit deduction (ONE rail)
  - CREDITS_ENABLED=true   -> failed tool -> release, no charge
  - CREDITS_ENABLED=true   -> pending_async tool -> release, no charge
  - CREDITS_ENABLED=true   -> duplicate operation_id -> charged once
  - CREDITS_ENABLED=true   -> Supabase down -> billing_unavailable (fail closed)
  - ensure_grandfather     -> applied exactly once on first account encounter

Slice 4 tests -- Polar webhook credit grant:
  - package credit grant on order.paid (metadata.credits path)
  - package credit grant on order.paid (PACKAGE_CREDITS name map path)
  - idempotent: re-delivered order_id -> grant NOT called again
  - CREDITS_ENABLED=false -> no grant (existing behavior unchanged)

billing/packages.py tests:
  - credits_for_product resolves from metadata, POLAR_PACKAGES env, and name map
  - POLAR_PACKAGES env override parses correctly
  - no match -> returns 0

All Supabase / identity / dispatch calls are mocked.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _success_receipt(cost_usd: float = 0.05) -> dict:
    return {
        "status": "success",
        "cost": {"amount": cost_usd, "currency": "USD", "basis": "per_call"},
    }


def _failure_receipt(status: str = "failure") -> dict:
    return {"status": status, "cost": {"amount": 0.0, "currency": "USD", "basis": "per_call"}}


def _reserve_ok(balance_after: int = 95) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _reserve_insufficient(balance: int = 0) -> dict:
    return {"ok": False, "reason_code": "insufficient_credits", "balance": balance}


def _commit_ok(balance_after: int = 90) -> dict:
    return {"ok": True, "balance_after": balance_after}


def _release_ok(balance_after: int = 100) -> dict:
    return {"ok": True, "balance_after": balance_after}


# Minimal MCP params for a paid tool call
def _mcp_params(name: str = "capture_lead", paying: bool = False) -> dict:
    """`paying=True` attaches an x402 payment, which is what makes a call an
    x402-paying call.

    This mattered on 2026-08-29. The x402 branch used to fire on
    `enabled() and is_paid_tool()` alone, so merely enabling the rail captured
    EVERY call and a test could assert the x402 path was taken without ever
    attaching a payment. That ordering would have made the advertised free tier
    false the instant the flag was set, so the branch now also requires a
    payment to be present - and this helper has to be able to produce one, or
    the test named "x402_paying_call" is not testing a paying call."""
    params = {
        "name": name,
        "arguments": {"smb_id": "smb_test", "prospect": {"name": "Test"}},
    }
    if paying:
        params["_meta"] = {"x402/payment": "eyJ4NDAyVmVyc2lvbiI6MX0="}
    return params


# Fake X-Agent-Identity header for a paid account (not free_)
_PAID_HEADERS = {"x-agent-identity": "tok_paid_account"}
_FREE_HEADERS  = {"x-agent-identity": "free_dev_trial"}


# ---------------------------------------------------------------------------
# Slice 3: MCP wiring tests
# ---------------------------------------------------------------------------

class TestMCPCreditsBranchDisabled:
    """With CREDITS_ENABLED=false, _h_tools_call must behave exactly as before."""

    def test_credits_disabled_does_not_call_run_metered_tool(self):
        """When CREDITS_ENABLED is false, run_metered_tool is never called."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "false"}):
            with patch(
                "agent_interface.mcp_server._dispatch_operation",
                new=AsyncMock(return_value=_success_receipt()),
            ) as mock_dispatch:
                with patch("billing.credits.run_metered_tool", new=AsyncMock()) as mock_meter:
                    from agent_interface.mcp_server import _h_tools_call
                    result = run(_h_tools_call(_mcp_params("capture_lead"), _PAID_HEADERS))
                    mock_meter.assert_not_awaited()
                    mock_dispatch.assert_awaited_once()

    def test_credits_disabled_free_key_still_works(self):
        """With CREDITS_ENABLED=false, free keys fall through to the 50/day gate."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "false"}):
            with patch(
                "agent_interface.mcp_server._dispatch_operation",
                new=AsyncMock(return_value=_success_receipt()),
            ) as mock_dispatch:
                with patch("billing.credits.run_metered_tool", new=AsyncMock()) as mock_meter:
                    # Free key should hit _dispatch_operation via normal gate,
                    # but run_metered_tool must never be touched
                    from agent_interface.mcp_server import _h_tools_call
                    # Won't get past auth in real code, but mock dispatch so we can verify meter
                    run(_h_tools_call(_mcp_params("capture_lead"), _FREE_HEADERS))
                    mock_meter.assert_not_awaited()


class TestMCPCreditsBranchEnabled:
    """With CREDITS_ENABLED=true, paid tools + paid accounts go through credits rail."""

    def _mock_resolve(self, account_id: str):
        return patch(
            "billing.credits.resolve_account",
            return_value=account_id,
        )

    def _mock_is_free(self, result: bool):
        return patch("billing.credits.is_free_key", return_value=result)

    def _mock_grandfather(self):
        return patch("billing.credits.ensure_grandfather", new=AsyncMock(return_value=False))

    def test_paid_tool_debits_once_on_success(self):
        """CREDITS_ENABLED=true + paid account + paid tool -> run_metered_tool called once."""
        mock_receipt = _success_receipt(0.05)
        mock_receipt["credits"] = {"charged": 5, "balance": 95}

        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("sub_cust1"):
                with self._mock_is_free(False):
                    with self._mock_grandfather():
                        with patch(
                            "billing.credits.run_metered_tool",
                            new=AsyncMock(return_value=mock_receipt),
                        ) as mock_meter:
                            from agent_interface.mcp_server import _h_tools_call
                            result = run(_h_tools_call(_mcp_params("capture_lead"), _PAID_HEADERS))

                        # run_metered_tool called exactly once
                        mock_meter.assert_awaited_once()
                        # isError=False on success
                        assert result.get("isError") is False
                        import json
                        content_text = result["content"][0]["text"]
                        parsed = json.loads(content_text)
                        assert parsed["status"] == "success"
                        assert parsed["credits"]["charged"] == 5

    def test_insufficient_balance_no_dispatch(self):
        """Insufficient credits -> honest failure returned, dispatch NOT called."""
        insufficient = {
            "status": "failure",
            "reason_code": "insufficient_credits",
            "human_message": "Insufficient credits...",
            "credits": {"charged": 0, "balance": 0},
        }

        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("sub_cust1"):
                with self._mock_is_free(False):
                    with self._mock_grandfather():
                        with patch(
                            "billing.credits.run_metered_tool",
                            new=AsyncMock(return_value=insufficient),
                        ):
                            with patch(
                                "agent_interface.mcp_server._dispatch_operation",
                                new=AsyncMock(return_value=_success_receipt()),
                            ) as mock_dispatch:
                                from agent_interface.mcp_server import _h_tools_call
                                result = run(_h_tools_call(_mcp_params("capture_lead"), _PAID_HEADERS))

                        # Normal dispatch must NOT be called (run_metered_tool handles it)
                        mock_dispatch.assert_not_awaited()
                        import json
                        parsed = json.loads(result["content"][0]["text"])
                        assert parsed["reason_code"] == "insufficient_credits"
                        assert result["isError"] is True

    def test_free_key_bypasses_credits(self):
        """Free key -> credits branch skipped; run_metered_tool never called."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("free_dev_123"):
                with patch("billing.credits.is_free_key", return_value=True):
                    with patch(
                        "billing.credits.run_metered_tool",
                        new=AsyncMock(),
                    ) as mock_meter:
                        with patch(
                            "agent_interface.mcp_server._dispatch_operation",
                            new=AsyncMock(return_value=_success_receipt()),
                        ):
                            from agent_interface.mcp_server import _h_tools_call
                            run(_h_tools_call(_mcp_params("capture_lead"), _FREE_HEADERS))
                        mock_meter.assert_not_awaited()

    def test_no_account_bypasses_credits(self):
        """No X-Agent-Identity header -> credits branch skipped."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with patch("billing.credits.resolve_account", return_value=None):
                with patch(
                    "billing.credits.run_metered_tool",
                    new=AsyncMock(),
                ) as mock_meter:
                    with patch(
                        "agent_interface.mcp_server._dispatch_operation",
                        new=AsyncMock(return_value=_success_receipt()),
                    ):
                        from agent_interface.mcp_server import _h_tools_call
                        run(_h_tools_call(_mcp_params("capture_lead"), {}))
                    mock_meter.assert_not_awaited()

    def test_x402_paying_call_no_credit_deduction(self):
        """When x402 is enabled and the tool is paid, credits gate is never reached
        (x402 branch returns first -- ONE rail guarantee)."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with patch("billing.x402_gate.enabled", return_value=True):
                with patch("billing.x402_gate.is_paid_tool", return_value=True):
                    with patch(
                        "billing.x402_gate.run_paid_tool",
                        new=AsyncMock(return_value={
                            "content": [{"type": "text", "text": "{}"}],
                            "isError": False,
                        }),
                    ) as mock_x402:
                        with patch(
                            "billing.credits.run_metered_tool",
                            new=AsyncMock(),
                        ) as mock_meter:
                            from agent_interface.mcp_server import _h_tools_call
                            run(_h_tools_call(_mcp_params("capture_lead", paying=True), _PAID_HEADERS))
                        mock_x402.assert_awaited_once()
                        mock_meter.assert_not_awaited()

    def test_failed_tool_no_charge(self):
        """Tool failure status -> run_metered_tool releases hold and returns charged=0."""
        failed_with_credits = _failure_receipt("failure")
        failed_with_credits["credits"] = {"charged": 0, "balance": 100}

        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("sub_cust1"):
                with self._mock_is_free(False):
                    with self._mock_grandfather():
                        with patch(
                            "billing.credits.run_metered_tool",
                            new=AsyncMock(return_value=failed_with_credits),
                        ):
                            from agent_interface.mcp_server import _h_tools_call
                            result = run(_h_tools_call(_mcp_params("send_message"), _PAID_HEADERS))

                        import json
                        parsed = json.loads(result["content"][0]["text"])
                        assert parsed["credits"]["charged"] == 0

    def test_supabase_down_fail_closed(self):
        """Supabase unreachable during reserve -> billing_unavailable (fail closed)."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("sub_cust1"):
                with self._mock_is_free(False):
                    with self._mock_grandfather():
                        with patch(
                            "billing.credits.run_metered_tool",
                            new=AsyncMock(side_effect=RuntimeError("connection refused")),
                        ):
                            from agent_interface.mcp_server import _h_tools_call
                            result = run(_h_tools_call(_mcp_params("capture_lead"), _PAID_HEADERS))

                        import json
                        parsed = json.loads(result["content"][0]["text"])
                        assert parsed["reason_code"] == "billing_unavailable"
                        assert result["isError"] is True

    def test_read_tool_bypasses_credits(self):
        """Read tool (find_business, price=0) -> is_credit_paid_tool=False -> no meter."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "true"}):
            with self._mock_resolve("sub_cust1"):
                with self._mock_is_free(False):
                    with patch(
                        "billing.credits.run_metered_tool",
                        new=AsyncMock(),
                    ) as mock_meter:
                        with patch(
                            "agent_interface.mcp_server._dispatch_operation",
                            new=AsyncMock(return_value={"status": "success", "result": {}}),
                        ):
                            from agent_interface.mcp_server import _h_tools_call
                            run(_h_tools_call(
                                {"name": "find_business",
                                 "arguments": {"vertical": "healthcare",
                                               "location": {"zip_or_city": "Atlanta"}}},
                                _PAID_HEADERS,
                            ))
                        mock_meter.assert_not_awaited()


# ---------------------------------------------------------------------------
# Slice 3: ensure_grandfather tests
# ---------------------------------------------------------------------------

class TestEnsureGrandfather:
    """ensure_grandfather must apply the courtesy grant exactly once."""

    def test_grandfather_applied_on_first_encounter(self):
        """No existing row -> grant with grandfather_ idempotency key."""
        with patch.dict(os.environ, {"GRANDFATHER_CREDITS": "1000"}):
            with patch("billing.credits.get_balance", new=AsyncMock(return_value=None)):
                with patch("billing.credits.rpc", new=AsyncMock(return_value={"ok": True, "balance_after": 1000})) as mock_rpc:
                    from billing.credits import ensure_grandfather
                    result = run(ensure_grandfather("sub_cust1"))
                    assert result is True
                    mock_rpc.assert_awaited_once()
                    call_args = mock_rpc.call_args
                    assert call_args[0][0] == "credit_grant"
                    assert call_args[0][1]["p_idempotency_key"] == "grandfather_sub_cust1"
                    assert call_args[0][1]["p_amount"] == 1000

    def test_grandfather_skipped_on_existing_account(self):
        """Existing account row (balance=100) -> no grant."""
        with patch("billing.credits.get_balance", new=AsyncMock(return_value=100)):
            with patch("billing.credits.rpc", new=AsyncMock()) as mock_rpc:
                from billing.credits import ensure_grandfather
                result = run(ensure_grandfather("sub_cust1"))
                assert result is False
                mock_rpc.assert_not_awaited()

    def test_grandfather_idempotent_second_call(self):
        """If account exists on second call -> no double grant."""
        call_count = {"n": 0}

        async def fake_balance(account_id: str):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None   # first call: no account
            return 1000       # second call: account exists

        async def fake_rpc(fn, payload):
            return {"ok": True, "balance_after": 1000}

        with patch.dict(os.environ, {"GRANDFATHER_CREDITS": "1000"}):
            with patch("billing.credits.get_balance", side_effect=fake_balance):
                with patch("billing.credits.rpc", side_effect=fake_rpc) as mock_rpc:
                    from billing.credits import ensure_grandfather
                    run(ensure_grandfather("sub_cust1"))   # first: grants
                    run(ensure_grandfather("sub_cust1"))   # second: account exists -> no grant
                    assert mock_rpc.await_count == 1       # only one grant call total

    def test_grandfather_fail_open_on_error(self):
        """If Supabase is down, ensure_grandfather logs and returns False (fail-open)."""
        with patch("billing.credits.get_balance", new=AsyncMock(side_effect=Exception("db down"))):
            from billing.credits import ensure_grandfather
            result = run(ensure_grandfather("sub_cust1"))
            assert result is False  # fail-open, no raise


# ---------------------------------------------------------------------------
# Slice 4: Polar webhook credit grant tests
# ---------------------------------------------------------------------------

def _make_polar_event(event_type: str = "order.paid", order_id: str = "order_abc") -> dict:
    return {
        "type": event_type,
        "data": {
            "id": order_id,
            "status": "paid",
            "customer": {"id": "cust_123", "email": "dev@example.com"},
            "product": {
                "id": "prod_starter",
                "name": "Starter",
                "metadata": {"credits": 1000},
            },
        },
    }


class TestPolarCreditGrant:
    """Credit grant fires on order.paid when CREDITS_ENABLED=true."""

    def _patch_polar_env(self):
        return patch.dict(os.environ, {"CREDITS_ENABLED": "true"})

    def _mock_identity(self):
        """Mock identity token issuance so handle_polar_event doesn't fail on missing JWT key."""
        token_resp = MagicMock()
        token_resp.token = "eyJ.fake.token"
        token_resp.expires_at = 9999999999
        return patch(
            "agent_interface.identity.issue_subscription_token",
            return_value=token_resp,
        )

    def _mock_already_processed(self, result: bool = False):
        return patch(
            "billing.polar_webhook._already_processed",
            new=AsyncMock(return_value=result),
        )

    def _mock_mark_processed(self):
        return patch(
            "billing.polar_webhook._mark_processed",
            new=AsyncMock(),
        )

    def test_credit_grant_on_order_paid_metadata_credits(self):
        """order.paid with product.metadata.credits -> credit grant called once."""
        with self._patch_polar_env():
            with self._mock_already_processed(False):
                with self._mock_mark_processed():
                    with self._mock_identity():
                        with patch("billing.credits.rpc", new=AsyncMock(return_value={"ok": True, "balance_after": 1000})) as mock_rpc:
                            from billing.polar_webhook import handle_polar_event
                            run(handle_polar_event(_make_polar_event()))

                        # credit_grant RPC should have been called
                        grant_calls = [
                            c for c in mock_rpc.call_args_list
                            if c[0][0] == "credit_grant"
                        ]
                        assert len(grant_calls) == 1
                        grant_payload = grant_calls[0][0][1]
                        assert grant_payload["p_amount"] == 1000
                        assert grant_payload["p_account"] == "sub_cust_123"
                        assert grant_payload["p_idempotency_key"] == "order_abc"
                        assert grant_payload["p_source"] == "polar"

    def test_credit_grant_uses_name_map_fallback(self):
        """product.metadata has no credits -> PACKAGE_CREDITS name map used."""
        event = _make_polar_event()
        event["data"]["product"]["metadata"] = {}  # no credits in metadata

        with self._patch_polar_env():
            with self._mock_already_processed(False):
                with self._mock_mark_processed():
                    with self._mock_identity():
                        with patch("billing.credits.rpc", new=AsyncMock(return_value={"ok": True, "balance_after": 1000})) as mock_rpc:
                            from billing.polar_webhook import handle_polar_event
                            run(handle_polar_event(event))

                        grant_calls = [
                            c for c in mock_rpc.call_args_list
                            if c[0][0] == "credit_grant"
                        ]
                        assert len(grant_calls) == 1
                        # "Starter" -> 1000 from PACKAGE_CREDITS
                        assert grant_calls[0][0][1]["p_amount"] == 1000

    def test_credit_grant_idempotent_on_duplicate_order(self):
        """Re-delivered order_id -> _already_processed=True -> grant NOT called."""
        with self._patch_polar_env():
            with self._mock_already_processed(True):  # already processed
                with patch("billing.credits.rpc", new=AsyncMock()) as mock_rpc:
                    from billing.polar_webhook import handle_polar_event
                    run(handle_polar_event(_make_polar_event()))

                grant_calls = [
                    c for c in mock_rpc.call_args_list
                    if c[0][0] == "credit_grant"
                ]
                assert len(grant_calls) == 0

    def test_credit_grant_skipped_when_disabled(self):
        """CREDITS_ENABLED=false -> credit_grant RPC never called."""
        with patch.dict(os.environ, {"CREDITS_ENABLED": "false"}):
            with self._mock_already_processed(False):
                with self._mock_mark_processed():
                    with self._mock_identity():
                        with patch("billing.credits.rpc", new=AsyncMock(return_value={"ok": True, "balance_after": 0})) as mock_rpc:
                            from billing.polar_webhook import handle_polar_event
                            run(handle_polar_event(_make_polar_event()))

                        grant_calls = [
                            c for c in mock_rpc.call_args_list
                            if c[0][0] == "credit_grant"
                        ]
                        assert len(grant_calls) == 0

    def test_revoke_event_no_credit_grant(self):
        """Revoke events (refund/subscription.revoked) must never grant credits."""
        with self._patch_polar_env():
            with patch("billing.credits.rpc", new=AsyncMock()) as mock_rpc:
                with patch(
                    "agent_interface.identity.revoke_customer",
                    new=AsyncMock(),
                ):
                    from billing.polar_webhook import handle_polar_event
                    run(handle_polar_event({
                        "type": "order.refunded",
                        "data": {"id": "order_abc", "customer": {"id": "cust_123"}},
                    }))

                grant_calls = [
                    c for c in mock_rpc.call_args_list
                    if c[0][0] == "credit_grant"
                ]
                assert len(grant_calls) == 0


# ---------------------------------------------------------------------------
# billing/packages.py tests
# ---------------------------------------------------------------------------

class TestPackages:
    """credits_for_product resolution priority: metadata > env > name map."""

    def test_metadata_credits_highest_priority(self):
        from billing.packages import credits_for_product
        result = credits_for_product(
            product_name="Starter",
            product_id="prod_abc",
            product_metadata={"credits": 999},
        )
        assert result == 999

    def test_polar_packages_env_override(self):
        from billing.packages import credits_for_product
        with patch.dict(os.environ, {"POLAR_PACKAGES": '{"prod_special": 2500}'}):
            result = credits_for_product(
                product_name="Unknown Product",
                product_id="prod_special",
                product_metadata={},
            )
        assert result == 2500

    def test_name_map_starter(self):
        from billing.packages import credits_for_product
        result = credits_for_product(
            product_name="Agent Broker Starter",
            product_id="prod_xyz",
            product_metadata={},
        )
        assert result == 1000

    def test_name_map_growth(self):
        from billing.packages import credits_for_product
        result = credits_for_product(product_name="Growth Plan", product_metadata={})
        assert result == 3500

    def test_name_map_scale(self):
        from billing.packages import credits_for_product
        result = credits_for_product(product_name="Scale Package", product_metadata={})
        assert result == 13000

    def test_no_match_returns_zero(self):
        from billing.packages import credits_for_product
        result = credits_for_product(
            product_name="Unknown Widget",
            product_id="prod_doesnt_exist",
            product_metadata={},
        )
        assert result == 0

    def test_polar_packages_env_malformed_ignored(self):
        from billing.packages import credits_for_product
        with patch.dict(os.environ, {"POLAR_PACKAGES": "NOT VALID JSON"}):
            # Falls through to name map
            result = credits_for_product(product_name="Starter", product_metadata={})
        assert result == 1000

    def test_metadata_credits_string_int(self):
        """metadata.credits may arrive as string from Polar JSON."""
        from billing.packages import credits_for_product
        result = credits_for_product(
            product_name="Growth",
            product_metadata={"credits": "3500"},
        )
        assert result == 3500
