"""Agent-Identity token revocation — durable, customer-level revocation used
by the Polar refund path (see billing/polar_webhook.py). This is the other
half of AUDIT-2026-08-16's refund finding: minting the revocation record is
only half the fix, validate_token() has to actually reject a revoked token
at the auth gate (main._get_identity / agent_interface.mcp_server.
_mcp_gate_identity already turn any invalid ValidationResult into the
standard 401 + Polar checkout-link message -- unchanged by this test, just
relied upon)."""
import asyncio
import time
from unittest.mock import AsyncMock, patch

from agent_interface.identity import (
    issue_subscription_token,
    revoke_customer,
    is_customer_revoked,
    validate_token,
)


def run(coro):
    return asyncio.run(coro)


class TestRevokedTokenRejectedAtValidation:
    def test_token_valid_before_revocation(self):
        token_resp = issue_subscription_token(
            customer_id="cust_reject_before_1", plan="developer",
            customer_email="x@example.com",
        )
        result = validate_token(token_resp.token)
        assert result.valid is True

    def test_revoked_token_fails_validation(self):
        token_resp = issue_subscription_token(
            customer_id="cust_reject_1", plan="developer",
            customer_email="x@example.com",
        )
        assert validate_token(token_resp.token).valid is True  # sanity: good before revoke

        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(revoke_customer(customer_id="cust_reject_1", order_id="order_x", reason="order.refunded"))

        result = validate_token(token_resp.token)
        assert result.valid is False
        assert "revoked" in (result.error or "").lower()
        assert result.identity is None

    def test_revocation_is_per_customer_not_global(self):
        """Revoking one customer must not affect another customer's live token."""
        victim = issue_subscription_token(
            customer_id="cust_innocent_1", plan="developer", customer_email="a@example.com",
        )
        refunded = issue_subscription_token(
            customer_id="cust_refunded_1", plan="developer", customer_email="b@example.com",
        )
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            run(revoke_customer(customer_id="cust_refunded_1", order_id="order_y", reason="order.refunded"))

        assert validate_token(refunded.token).valid is False
        assert validate_token(victim.token).valid is True

    def test_revoke_customer_persists_durably_best_effort(self):
        """revoke_customer() must attempt a durable write (so the revocation
        survives a restart) but must never raise even if that write fails --
        matches the fire-and-forget contract every other durable write in
        this codebase has (billing/durable_meter.py, storage/supabase_client.py)."""
        with patch(
            "storage.supabase_client.insert_row",
            new_callable=AsyncMock, side_effect=RuntimeError("supabase down"),
        ) as mock_insert:
            # Must not raise despite the durable write failing.
            run(revoke_customer(customer_id="cust_store_down_1", order_id="order_z", reason="order.refunded"))

        # In-memory revocation still took effect even though persistence failed.
        assert is_customer_revoked("cust_store_down_1") is True
        assert mock_insert.await_count == 1

    def test_revoke_customer_noop_on_empty_customer_id(self):
        # Must not raise or pollute the revoked set on a malformed/empty id.
        run(revoke_customer(customer_id="", order_id="order_q", reason="order.refunded"))
        assert is_customer_revoked("") is False

    def test_expired_token_still_rejected_independent_of_revocation(self):
        """Regression guard: the new revocation check must not short-circuit
        or otherwise disturb the pre-existing expiry check."""
        token_resp = issue_subscription_token(
            customer_id="cust_expiry_check_1", plan="developer",
            customer_email="x@example.com",
        )
        # Directly craft an already-expired token via the same signing path
        # the module uses, so we don't depend on real wall-clock waiting.
        from agent_interface.identity import _sign
        import json, base64
        b64_payload, _sig = token_resp.token.split(".")
        padded = b64_payload + "=" * (-len(b64_payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode())
        claims["exp"] = time.time() - 10
        expired_token = _sign(claims)

        result = validate_token(expired_token)
        assert result.valid is False
        assert "expired" in (result.error or "").lower()
