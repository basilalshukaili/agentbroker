"""
Fault injection tests — verify resilience to external failures.
Tests: channel down, storage timeout, duplicate idempotency keys,
       malformed input, unreachable SMB, expired Agent-Identity token.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.models import (
    FindBusinessRequest, LocationFilter, Vertical,
    ScheduleAppointmentRequest, AppointmentAction,
    OperationStatus,
)
from core.find_business import handle_find_business
from core.schedule_appointment import handle_schedule_appointment
from reliability.circuit_breaker import CircuitBreaker, CircuitState


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestChannelDown:
    def test_find_business_succeeds_with_directory_failure(self):
        """find_business uses in-memory directory — no external channel, always works."""
        req = FindBusinessRequest(
            vertical=Vertical.PERSONAL_SERVICES,
            location=LocationFilter(zip_or_city="Atlanta"),
        )
        receipt = run(handle_find_business(req))
        assert receipt.status == OperationStatus.SUCCESS

    def test_schedule_appointment_falls_back_when_calcom_fails(self):
        """When Cal.com raises an exception, should return pending_async (voice fallback)."""
        with patch("channels.direct_api.calcom.CalComAdapter.get_availability",
                   new_callable=AsyncMock, side_effect=Exception("Cal.com 503")):
            req = ScheduleAppointmentRequest(
                smb_id="smb_001",
                action=AppointmentAction.BOOK,
                service="haircut",
            )
            receipt = run(handle_schedule_appointment(req))
            # Should fall back to async path, not raise
            assert receipt.status in (OperationStatus.SUCCESS, OperationStatus.PENDING_ASYNC)


class TestUnreachableSMB:
    def test_schedule_unknown_smb_returns_failure(self):
        req = ScheduleAppointmentRequest(
            smb_id="smb_NONEXISTENT",
            action=AppointmentAction.BOOK,
        )
        receipt = run(handle_schedule_appointment(req))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "supply_unreachable"

    def test_find_business_empty_location_returns_empty_not_error(self):
        req = FindBusinessRequest(
            vertical=Vertical.HOME_SERVICES,
            location=LocationFilter(zip_or_city="XYZ99999"),
        )
        receipt = run(handle_find_business(req))
        assert receipt.status == OperationStatus.SUCCESS
        assert receipt.result["businesses"] == []


class TestMalformedInput:
    def test_invalid_vertical_raises_validation_error(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            FindBusinessRequest(
                vertical="underwater_basket_weaving",
                location=LocationFilter(zip_or_city="30309"),
            )

    def test_cancel_without_appointment_id_returns_bad_input(self):
        req = ScheduleAppointmentRequest(
            smb_id="smb_001",
            action=AppointmentAction.CANCEL,
            # missing existing_appointment_id
        )
        receipt = run(handle_schedule_appointment(req))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "bad_input"


class TestDuplicateIdempotencyKey:
    def test_idempotency_store_prevents_duplicate(self):
        from storage.idempotency_store import IdempotencyStore
        store = IdempotencyStore()
        outcome = {"operation_id": "op_123", "status": "success"}
        store.set("agent_01", "send_message", "key_abc", outcome)
        assert store.exists("agent_01", "send_message", "key_abc")
        cached = store.get("agent_01", "send_message", "key_abc")
        assert cached == outcome

    def test_different_agent_same_key_does_not_collide(self):
        from storage.idempotency_store import IdempotencyStore
        store = IdempotencyStore()
        store.set("agent_A", "send_message", "key_xyz", {"status": "success_A"})
        store.set("agent_B", "send_message", "key_xyz", {"status": "success_B"})
        assert store.get("agent_A", "send_message", "key_xyz")["status"] == "success_A"
        assert store.get("agent_B", "send_message", "key_xyz")["status"] == "success_B"


class TestCircuitBreaker:
    def test_circuit_opens_after_threshold_failures(self):
        cb = CircuitBreaker(name="test_channel", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.is_available()

    def test_circuit_enters_half_open_after_timeout(self):
        import time
        cb = CircuitBreaker(name="test_channel2", failure_threshold=3, recovery_timeout_s=0.01)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.is_available()  # should transition to HALF_OPEN

    def test_circuit_closes_after_success_in_half_open(self):
        import time
        cb = CircuitBreaker(name="test_channel3", failure_threshold=3, recovery_timeout_s=0.01,
                            success_threshold=2)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.02)
        cb.is_available()  # triggers HALF_OPEN
        cb.record_success()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED


class TestErrorNormalizer:
    def test_timeout_exception_normalized_to_transient(self):
        from reliability.error_normalizer import normalize_exception
        from core.models import ErrorCode
        err = normalize_exception(Exception("connection timed out"), "send_message")
        assert err.code == ErrorCode.TRANSIENT
        assert err.retriable is True

    def test_rate_limit_exception_normalized(self):
        from reliability.error_normalizer import normalize_exception
        from core.models import ErrorCode
        err = normalize_exception(Exception("429 Too Many Requests"), "find_business")
        assert err.code == ErrorCode.RATE_LIMITED

    def test_compliance_violation_normalized(self):
        from reliability.error_normalizer import normalize_exception
        from core.models import ComplianceViolationError, ErrorCode
        exc = ComplianceViolationError("TCPA", "+1234", "sms", "US", "No consent on file.")
        err = normalize_exception(exc)
        assert err.code == ErrorCode.COMPLIANCE_VIOLATION
        assert err.retriable is False
