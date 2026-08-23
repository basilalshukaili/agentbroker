"""
Regression tests — honesty fixes (2026-08-24).

Proves that every formerly-lying path now either:
  (a) fails honestly, OR
  (b) performs the real action before returning success.

Tests run without any external credentials so they cover the production
no-creds state exactly.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from core.models import (
    ScheduleAppointmentRequest, AppointmentAction, OperationStatus,
    EscalateToHumanRequest, EscalationReason, EscalationContext,
    HandleInboundRequest, InboundChannel, InboundSender,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_real_smb():
    """Return a non-demo SMB stub that claims to support Cal.com."""
    import dataclasses
    from supply.smb_directory import get_directory
    base = get_directory().get("smb_001")
    return dataclasses.replace(
        base,
        smb_id="smb_honesty_real",
        name="Honesty Test Salon",
        is_demo=False,
        calcom_event_type_id="999",
        channels_available=["direct_api:calcom"],
    )


class _StubDirectory:
    def __init__(self, smb):
        self._smb = smb

    def get(self, smb_id):
        return self._smb if smb_id == self._smb.smb_id else None


# ---------------------------------------------------------------------------
# FIX 2b: CalCom adapter returns honest failure with no key
# ---------------------------------------------------------------------------

class TestCalComNoKey:
    """All three CalCom operations must fail honestly when CALCOM_API_KEY is unset."""

    def _adapter(self):
        from channels.direct_api.calcom import CalComAdapter
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALCOM_API_KEY", None)
            return CalComAdapter()

    def test_get_availability_raises_without_key(self):
        """FIX 2b: get_availability must raise, not return stub slots."""
        adapter = self._adapter()
        with pytest.raises(RuntimeError, match="CALCOM_API_KEY missing"):
            run(adapter.get_availability("999", "2026-01-01", "2026-01-04"))

    def test_cancel_booking_raises_without_key(self):
        """FIX 2b: cancel_booking must raise, not return status=CANCELLED."""
        adapter = self._adapter()
        with pytest.raises(RuntimeError, match="CALCOM_API_KEY missing"):
            run(adapter.cancel_booking("uid_abc"))

    def test_book_slot_raises_without_key(self):
        """Pre-existing gate: book_slot should also raise (regression guard)."""
        adapter = self._adapter()
        with pytest.raises(RuntimeError):
            run(adapter.book_slot("999", "2026-01-01T10:00:00Z", "Test", "t@e.com"))


# ---------------------------------------------------------------------------
# FIX 2b+2c: schedule_appointment returns not_configured failure with no key
# ---------------------------------------------------------------------------

class TestScheduleAppointmentNoKey:
    """Net behavior with no CALCOM_API_KEY: honest failure, never fake success."""

    def _req(self, action=AppointmentAction.BOOK, appointment_id=None):
        return ScheduleAppointmentRequest(
            smb_id="smb_honesty_real",
            action=action,
            existing_appointment_id=appointment_id,
        )

    def _run_with_real_smb(self, req):
        from core.schedule_appointment import handle_schedule_appointment
        real = _make_real_smb()
        with patch("core.schedule_appointment.get_directory", return_value=_StubDirectory(real)):
            return run(handle_schedule_appointment(req))

    def test_book_no_calcom_key_returns_failure(self):
        """FIX 2b+2c: book with no CALCOM_API_KEY -> failure, never success."""
        os.environ.pop("CALCOM_API_KEY", None)
        receipt = self._run_with_real_smb(self._req(AppointmentAction.BOOK))
        assert receipt.status == OperationStatus.FAILURE, (
            f"Expected FAILURE but got {receipt.status!r}. "
            "This means a fake booking confirmation is being returned!"
        )
        assert receipt.cost.amount == 0.0, f"Should not charge on failure, got {receipt.cost.amount}"
        assert receipt.status != OperationStatus.SUCCESS

    def test_cancel_no_calcom_key_returns_failure_not_cancelled(self):
        """FIX 2b+2c: cancel with no key -> honest failure, NOT status=CANCELLED."""
        os.environ.pop("CALCOM_API_KEY", None)
        receipt = self._run_with_real_smb(
            self._req(AppointmentAction.CANCEL, appointment_id="uid_xyz")
        )
        assert receipt.status == OperationStatus.FAILURE, (
            f"Expected FAILURE but got {receipt.status!r}. "
            "This means a fake CANCELLED receipt is being returned!"
        )
        # Must not contain a CANCELLED-looking reason code
        assert receipt.reason_code != "cancelled"
        assert receipt.cost.amount == 0.0

    def test_check_availability_no_calcom_key_returns_failure(self):
        """FIX 2b+2c: availability check with no key -> honest failure."""
        os.environ.pop("CALCOM_API_KEY", None)
        receipt = self._run_with_real_smb(self._req(AppointmentAction.CHECK_AVAILABILITY))
        assert receipt.status == OperationStatus.FAILURE, (
            f"Expected FAILURE but got {receipt.status!r}."
        )
        assert receipt.cost.amount == 0.0


# ---------------------------------------------------------------------------
# FIX 2a: async_runner voice stub is gone
# ---------------------------------------------------------------------------

class TestAsyncRunnerNoVoiceStub:
    """FIX 2a: the Celery task voice fallback must return failure, not fake success."""

    def test_enqueue_booking_voice_fallback_returns_failure_not_success(self):
        """Voice AI path in async_runner must not return success without a real Vapi call."""
        try:
            from reliability.async_runner import enqueue_booking
        except Exception:
            pytest.skip("Celery not importable in this environment")

        from storage.outcome_store import get_outcome_store
        import dataclasses
        from supply.smb_directory import get_directory

        # Build a real non-demo SMB with no calcom_event_type_id so it falls
        # straight to the voice path
        base = get_directory().get("smb_001")
        smb = dataclasses.replace(
            base,
            smb_id="smb_voice_test",
            name="Voice Fallback Test",
            is_demo=False,
            calcom_event_type_id=None,
            channels_available=["voice_ai:vapi"],
        )

        store = get_outcome_store()
        op_id = "op_voice_stub_test_001"
        store.set_pending(op_id, "schedule_appointment")

        # Manually call the Celery task body synchronously (bypass Celery dispatch)
        # by importing and running the function body directly
        import asyncio

        class _FakeDirectory:
            def get(self, smb_id):
                return smb if smb_id == "smb_voice_test" else None

        with patch("reliability.async_runner.get_directory", return_value=_FakeDirectory()), \
             patch("reliability.async_runner._fire_webhook"):
            # Run the inner logic directly; Celery .delay is not called
            from reliability import async_runner as ar
            if not ar.CELERY_AVAILABLE:
                pytest.skip("Celery not available")

            loop = asyncio.new_event_loop()
            result = ar.enqueue_booking.__wrapped__(
                MagicMock(),  # self (task)
                op_id,
                {"smb_id": "smb_voice_test", "action": "book"},
                "smb_voice_test",
                None,
                None,
            )

        # Must be failure, NEVER success
        assert result["status"] == "failure", (
            f"Voice fallback returned {result['status']!r} -- fake confirmation is still live!"
        )
        assert result["reason_code"] == "voice_not_provisioned"
        assert result.get("cost", {}).get("amount", 999) == 0.0


# ---------------------------------------------------------------------------
# FIX 1: escalate_to_human -- real insert or honest failure
# ---------------------------------------------------------------------------

class TestEscalateToHuman:
    """FIX 1: escalate_to_human behavior depends entirely on Supabase insert result."""

    def _req(self):
        return EscalateToHumanRequest(
            smb_id="smb_001",
            reason=EscalationReason.AUTOMATION_FAILED,
            context=EscalationContext(
                original_operation="schedule_appointment",
                operation_id="op_test_001",
                recommended_next_step="retry",
            ),
        )

    def test_returns_honest_failure_when_supabase_insert_fails(self):
        """If Supabase insert returns None (unreachable), must return failure at cost 0.00."""
        from core.escalate_to_human import handle_escalate_to_human

        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            receipt = run(handle_escalate_to_human(self._req()))

        assert receipt.status == OperationStatus.FAILURE, (
            f"Expected FAILURE when Supabase is down, got {receipt.status!r}"
        )
        assert receipt.reason_code == "escalation_not_recorded"
        assert receipt.cost.amount == 0.0, (
            f"Must not charge when insert failed, got {receipt.cost.amount}"
        )
        # Must not contain a "60-300s" promise
        assert "60" not in (receipt.human_message or "")
        assert "300" not in (receipt.human_message or "")

    def test_returns_success_only_on_real_insert(self):
        """When Supabase insert succeeds, must return SUCCESS with the real row id."""
        from core.escalate_to_human import handle_escalate_to_human

        fake_row = {"id": "real-uuid-1234", "status": "open"}
        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=fake_row):
            receipt = run(handle_escalate_to_human(self._req()))

        assert receipt.status == OperationStatus.SUCCESS
        assert receipt.cost.amount == 0.20
        assert "real-uuid-1234" in receipt.human_message
        ticket_id = receipt.result.get("escalation_ticket_id", "")
        assert ticket_id == "real-uuid-1234", f"ticket_id should be real row id, got {ticket_id!r}"
        # Must not contain "60-300s" promise
        assert "60" not in receipt.human_message
        assert "300" not in receipt.human_message

    def test_no_charge_on_failure(self):
        """Cost must be exactly 0.00 when insert fails."""
        from core.escalate_to_human import handle_escalate_to_human

        with patch("storage.supabase_client.insert_row", new_callable=AsyncMock, return_value=None):
            receipt = run(handle_escalate_to_human(self._req()))

        assert receipt.cost.amount == 0.0
        assert receipt.cost.currency == "USD"


# ---------------------------------------------------------------------------
# FIX 4: hard prod guard disables stubs when RENDER is set
# ---------------------------------------------------------------------------

class TestProdGuard:
    """FIX 4: stubs are ALWAYS disabled in production regardless of ALLOW_STUB_CHANNELS."""

    def test_stubs_disabled_when_render_env_set(self):
        """When RENDER is set, stubs_allowed() must return False even if ALLOW_STUB_CHANNELS=1."""
        from channels.stub_policy import stubs_allowed
        with patch.dict(os.environ, {"RENDER": "true", "ALLOW_STUB_CHANNELS": "1"}):
            assert stubs_allowed() is False, (
                "stubs_allowed() returned True with RENDER=true -- "
                "stub receipts can appear in production!"
            )

    def test_stubs_disabled_when_environment_production(self):
        """ENVIRONMENT=production also forces stubs off."""
        from channels.stub_policy import stubs_allowed
        env = {"ENVIRONMENT": "production", "ALLOW_STUB_CHANNELS": "1"}
        with patch.dict(os.environ, env):
            assert stubs_allowed() is False

    def test_stubs_allowed_in_dev_with_flag(self):
        """Outside production, ALLOW_STUB_CHANNELS=1 should still work for local tests."""
        from channels.stub_policy import stubs_allowed
        env_overrides = {"ALLOW_STUB_CHANNELS": "1"}
        # Remove production indicators
        with patch.dict(os.environ, env_overrides):
            os.environ.pop("RENDER", None)
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("ENV", None)
            result = stubs_allowed()
        assert result is True

    def test_stubs_off_by_default_without_flag(self):
        """Without ALLOW_STUB_CHANNELS, stubs are off in any environment."""
        from channels.stub_policy import stubs_allowed
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENDER", None)
            os.environ.pop("ALLOW_STUB_CHANNELS", None)
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("ENV", None)
            assert stubs_allowed() is False
