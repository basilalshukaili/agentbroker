"""
schedule_appointment — core operation handler.
Async-by-default. Returns pending_async immediately; Celery worker completes the booking.
Channel chain: direct_api → voice_ai → web_form → escalate_to_human.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from core.models import (
    ScheduleAppointmentRequest, OutcomeReceipt, OperationStatus, CostRecord
)
from supply.smb_directory import get_directory
from channels.direct_api.calcom import CalComAdapter


async def handle_schedule_appointment(
    request: ScheduleAppointmentRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    """
    Web-tier handler. Validates, enqueues Celery task, returns pending_async.
    For direct_api SMBs with Cal.com: attempts sync booking and returns immediately.
    For voice_ai channel: always async.
    """
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())
    directory = get_directory()
    smb = directory.get(request.smb_id)

    if not smb:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="supply_unreachable",
            human_message=f"SMB {request.smb_id} not found in supply network.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    # Fast path: direct_api:calcom
    if "direct_api:calcom" in smb.channels_available and smb.calcom_event_type_id:
        adapter = CalComAdapter()
        try:
            if request.action.value == "book":
                date_from = (
                    request.requested_time.window_start_iso.isoformat()
                    if request.requested_time and request.requested_time.window_start_iso
                    else datetime.now(timezone.utc).isoformat()
                )
                date_to = (
                    request.requested_time.window_end_iso.isoformat()
                    if request.requested_time and request.requested_time.window_end_iso
                    else (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
                )
                slots = await adapter.get_availability(smb.calcom_event_type_id, date_from, date_to)
                if not slots:
                    channel_chain = ["direct_api:calcom (no_availability)"]
                    # Fall through to voice_ai async path
                else:
                    slot = slots[0]
                    booking = await adapter.book_slot(
                        event_type_id=smb.calcom_event_type_id,
                        start=slot["time"],
                        name=request.customer.name if request.customer else "Customer",
                        email=request.customer.email if request.customer and request.customer.email else "noreply@example.com",
                        notes=request.notes,
                    )
                    return OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.SUCCESS,
                        reason_code="appointment_confirmed",
                        human_message=f"Appointment booked at {smb.name} for {slot['time']}.",
                        result={
                            "appointment_id": booking.get("uid", operation_id),
                            "confirmed_time": slot["time"],
                            "smb_name": smb.name,
                            "action": "booked",
                        },
                        cost=CostRecord(amount=1.00, currency="USD", basis="per_booking_attempt+success_bonus"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        retriable=False,
                        trace_id=trace_id,
                    )

            elif request.action.value == "cancel":
                if not request.existing_appointment_id:
                    return OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.FAILURE,
                        reason_code="bad_input",
                        human_message="existing_appointment_id is required for cancel action.",
                        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
                        retriable=False,
                        trace_id=trace_id,
                    )
                result = await adapter.cancel_booking(request.existing_appointment_id)
                return OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="cancelled",
                    human_message=f"Appointment {request.existing_appointment_id} cancelled.",
                    result=result,
                    cost=CostRecord(amount=0.25, currency="USD", basis="per_booking_attempt"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel_used="direct_api:calcom",
                    retriable=False,
                    trace_id=trace_id,
                )
        except Exception:
            pass  # fall through to async voice path

    # Async path: voice_ai or web_form — enqueue Celery task
    estimated = datetime.now(timezone.utc) + timedelta(seconds=90)
    _enqueue_async_booking(operation_id, request, smb, agent_id, trace_id)

    channel_chain = ["direct_api:calcom (unavailable)"] if "direct_api:calcom" not in smb.channels_available else []
    likely_channel = "voice_ai:vapi" if "voice_ai:vapi" in smb.channels_available else "sms"

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.PENDING_ASYNC,
        reason_code="booking_in_progress",
        human_message=f"Booking request submitted for {smb.name}. Estimated completion: {estimated.isoformat()}.",
        cost=CostRecord(amount=0.25, currency="USD", basis="per_booking_attempt"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        channel_fallback_chain=channel_chain,
        estimated_completion_time=estimated,
        next_actions=[
            f"poll get_status with operation_id {operation_id}",
            "or await webhook callback if Webhook-URL was provided",
        ],
        retriable=False,
        trace_id=trace_id,
    )


def _enqueue_async_booking(operation_id, request, smb, agent_id, trace_id):
    """
    Enqueue a Celery task for async booking execution.
    In test/stub mode (no Celery broker): immediately writes a stub pending record.
    """
    try:
        from reliability.async_runner import enqueue_booking  # type: ignore
        enqueue_booking.delay(operation_id, request.model_dump(), smb.smb_id, agent_id, trace_id)
    except Exception:
        # Celery not available in test mode — store as pending for get_status
        from storage.outcome_store import get_outcome_store
        get_outcome_store().set_pending(operation_id, "schedule_appointment")
