"""
schedule_appointment — core operation handler.
Async-by-default. Returns pending_async immediately; Celery worker completes the booking.
Channel chain: direct_api → voice_ai → web_form → escalate_to_human.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from core.models import (
    ScheduleAppointmentRequest, OutcomeReceipt, OperationStatus, CostRecord
)
from storage.outcome_store import get_outcome_store
from supply.smb_directory import get_directory
from channels.direct_api.calcom import CalComAdapter
from billing.pricing import receipt_usd as _receipt_usd


def _has_celery_worker() -> bool:
    """True only when a real Celery broker URL is configured and celery is importable."""
    try:
        from reliability.async_runner import CELERY_AVAILABLE
        if not CELERY_AVAILABLE:
            return False
    except ImportError:
        return False
    # A bare localhost default means no real broker is wired up
    return bool(os.getenv("CELERY_BROKER_URL"))


# How far from the requested time a slot may sit and still count as "the time
# you asked for". Cal.com returns slot starts on the event type's own grid, so
# an exact string match is too strict (a 14:00 request against a 14:00:00.000Z
# slot must match, and so must a 14:00 request on a :05 grid), while anything
# wider starts silently moving the appointment.
_SLOT_TOLERANCE = timedelta(minutes=15)


def _choose_slot(slots: list, preferred):
    """Pick the slot to book, or None if the requested time is unavailable.

    WITHOUT A PREFERRED TIME this is slots[0], which is what the caller means
    when they give a window and no preference.

    WITH ONE, it is the nearest slot within _SLOT_TOLERANCE - and None if
    there isn't one. Returning None is the point: the previous code booked
    slots[0] regardless, so an agent asking for 2pm on the 15th got a real
    booking in a real customer's name at the first free slot (measured: 16
    days earlier), reported as "appointment_confirmed" and charged the full
    per_confirmed_booking fee.
    """
    if not slots:
        return None
    if preferred is None:
        return slots[0]

    def _t(s):
        return s.get("start") or s.get("time")

    best, best_gap = None, None
    for s in slots:
        raw = _t(s)
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        gap = abs(when - preferred)
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    if best is not None and best_gap is not None and best_gap <= _SLOT_TOLERANCE:
        return best
    return None


def _store_terminal(receipt: OutcomeReceipt) -> OutcomeReceipt:
    """Persist a terminal OutcomeReceipt to the outcome store keyed by operation_id.

    Mirrors the storage contract used by reliability/async_runner.py so a
    subsequent get_status / get_outcome call can resolve the same id.
    """
    try:
        get_outcome_store().set_complete(receipt.operation_id, receipt.model_dump(mode="json"))
    except Exception:
        # Storage must never break the user-facing return path.
        pass
    return receipt


async def handle_schedule_appointment(
    request: ScheduleAppointmentRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    """
    Web-tier handler. Validates, enqueues Celery task, returns pending_async.
    For direct_api SMBs with Cal.com: attempts sync booking and returns immediately.
    For voice_ai channel: always async.

    Every return path persists its receipt to outcome_store keyed by
    operation_id so get_status / get_outcome can resolve it later.
    """
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())

    # Argument validation comes FIRST: a malformed request is malformed no
    # matter which SMB it names. Previously the supply/demo short-circuits ran
    # ahead of this, so "cancel with no appointment id" came back as
    # demo_smb_no_live_booking and the caller never learned what was wrong.
    if request.action.value == "cancel" and not request.existing_appointment_id:
        return _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message="existing_appointment_id is required for cancel action.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ))

    directory = get_directory()
    smb = directory.get(request.smb_id)

    if not smb:
        return _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="supply_unreachable",
            human_message=f"SMB {request.smb_id} not found in supply network.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ))

    # CRITICAL-1 fix: demo SMBs must never trigger a real charge.
    # The directory contract (smb_directory.py line 39) promises that bookings
    # against demo SMBs short-circuit with reason_code='demo_smb_no_live_booking'
    # instead of contacting real businesses. This guard honours that promise and
    # returns status=failure so _receipt_is_error() returns True in x402_gate,
    # which causes the SDK to SKIP settlement — no USDC charged.
    if getattr(smb, "is_demo", False):
        return _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="demo_smb_no_live_booking",
            human_message=(
                f"{smb.name} is a sandbox/demo entry. No real action was taken. "
                "Use import_booking_url to add a real business."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_demo"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ))

    # OPT-OUT applies to BOOKING, not only to messaging (added 2026-08-26).
    # Every messaging path passes through compliance/pre_check; this one never
    # did, so a business that replied STOP could still be booked through us -
    # and a booking is contact: Cal.com emails them, and someone turns up.
    # Honouring "stop" on one channel while quietly using another is exactly
    # the leak we fixed in August, wearing a different hat.
    #
    # Deliberately narrower than pre_check: consent, quiet hours and marketing
    # rules describe outbound MESSAGES and do not describe a booking. The one
    # question that does transfer is whether this business asked us to stop.
    try:
        from compliance.consent_store import get_consent_store
        _store = get_consent_store()
        _contacts = [c for c in (getattr(smb, "phone", None),
                                 getattr(smb, "email", None)) if c]
        _blocked = next(
            (c for c in _contacts
             if _store.is_opted_out(c, "sms") or _store.is_opted_out(c, "email")
             or _store.is_opted_out(c, "whatsapp")),
            None)
    except Exception:  # noqa: BLE001
        # Fail CLOSED on the opt-out question. Everywhere else in booking we
        # degrade gracefully; here an unreadable consent store is not licence
        # to contact someone who may have said stop.
        _blocked = "unknown"
    if _blocked:
        return _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="recipient_opted_out",
            human_message=(
                f"{smb.name} has opted out of contact through HatchLoop, so we "
                "will not create a booking with them. This applies to every "
                "agent on the network, not just yours."
                if _blocked != "unknown" else
                "Could not verify this business's contact preferences, so the "
                "booking was not attempted. Retry shortly."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge_opted_out"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=(_blocked == "unknown"),
            trace_id=trace_id,
        ))

    # Fast path: direct_api:calcom
    # Note: CalComAdapter raises RuntimeError when CALCOM_API_KEY is absent and
    # stubs are not allowed (production). Those exceptions are caught below and
    # routed to the no-worker honest-failure path.
    if "direct_api:calcom" in smb.channels_available and smb.calcom_event_type_id:
        adapter = CalComAdapter()
        try:
            if request.action.value == "book":
                rt = request.requested_time
                # PREFERRED_ISO WAS READ NOWHERE, AND slots[0] WAS BOOKED.
                #
                # An agent asking for 2pm on the 15th got a real Cal.com
                # booking in a real customer's name at whatever the first
                # available slot happened to be - measured, 16 days earlier -
                # under reason_code "appointment_confirmed" and charged the
                # full per_confirmed_booking fee. Nothing in the receipt said
                # the requested time had not been honoured, because nothing in
                # the code had looked at it.
                #
                # So preferred_iso now drives the search window when no
                # explicit window is given, and a slot that does not match it
                # is NOT booked - see the honest refusal below.
                pref = rt.preferred_iso if rt and rt.preferred_iso else None
                default_from = pref or datetime.now(timezone.utc)
                default_to = (
                    (pref + timedelta(days=1)) if pref
                    else datetime.now(timezone.utc) + timedelta(days=3)
                )
                date_from = (
                    rt.window_start_iso.isoformat()
                    if rt and rt.window_start_iso else default_from.isoformat()
                )
                date_to = (
                    rt.window_end_iso.isoformat()
                    if rt and rt.window_end_iso else default_to.isoformat()
                )

                # AN INVERTED WINDOW IS A CALLER ERROR, NOT AN OUTAGE.
                # window_start_iso and window_end_iso fall back independently,
                # so supplying only a start produced date_from AFTER date_to.
                # Cal.com 400s, get_availability returns [], and the request
                # fell through to a message blaming absent credentials
                # ("CALCOM_API_KEY absent") with retriable=False - naming a
                # cause that was not true and telling the agent to give up on
                # something one extra field would fix.
                if date_from >= date_to:
                    return _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.FAILURE,
                        reason_code="bad_request",
                        human_message=(
                            f"requested_time window is inverted or empty: "
                            f"start {date_from} is not before end {date_to}. "
                            f"Supply both window_start_iso and window_end_iso, "
                            f"or a preferred_iso on its own."),
                        result=None,
                        cost=CostRecord(amount=0.0, currency="USD",
                                        basis="no_charge"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        retriable=False,
                        trace_id=trace_id,
                    ))

                slots = await adapter.get_availability(smb.calcom_event_type_id, date_from, date_to)
                if not slots:
                    pass  # fall through to async/sync failure path
                else:
                    # Cal.com has used both keys across API versions, and our
                    # own stub emits "start" while this read "time" - a
                    # KeyError that was swallowed and re-emerged as the false
                    # "no booking channel configured" message.
                    def _slot_time(s: dict):
                        return s.get("start") or s.get("time")

                    slot = _choose_slot(slots, pref)
                    if slot is None:
                        # THE REQUESTED TIME IS NOT AVAILABLE. Booking a
                        # different one and calling it confirmed is the defect
                        # this whole block exists to stop. Offer, do not book,
                        # and charge nothing.
                        offered = [t for t in (_slot_time(s) for s in slots[:5]) if t]
                        return _store_terminal(OutcomeReceipt(
                            operation_id=operation_id,
                            status=OperationStatus.SUCCESS,
                            reason_code="requested_time_unavailable",
                            human_message=(
                                f"NOT BOOKED: {smb.name} has nothing at the "
                                f"requested time ({pref.isoformat() if pref else 'n/a'}). "
                                f"Nothing was reserved and nothing was charged. "
                                f"Available instead: {', '.join(offered) or 'no slots'}. "
                                f"Re-send with one of these as preferred_iso, or "
                                f"widen the window."),
                            result={
                                "booked": False,
                                "requested_time": pref.isoformat() if pref else None,
                                "available_slots": offered,
                                "smb_name": smb.name,
                            },
                            cost=CostRecord(amount=0.0, currency="USD",
                                            basis="no_charge"),
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            channel_used="direct_api:calcom",
                            retriable=False,
                            trace_id=trace_id,
                        ))
                    booking = await adapter.book_slot(
                        event_type_id=smb.calcom_event_type_id,
                        start=_slot_time(slot),
                        name=request.customer.name if request.customer else "Customer",
                        email=request.customer.email if request.customer and request.customer.email else "noreply@example.com",
                        notes=request.notes,
                    )
                    return _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.SUCCESS,
                        reason_code="appointment_confirmed",
                        human_message=f"Appointment booked at {smb.name} for {_slot_time(slot)}.",
                        result={
                            "appointment_id": booking.get("uid", operation_id),
                            "confirmed_time": _slot_time(slot),
                            "smb_name": smb.name,
                            "action": "booked",
                        },
                        cost=CostRecord(amount=_receipt_usd("schedule_appointment", at_max=True), currency="USD", basis="per_confirmed_booking"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        retriable=False,
                        trace_id=trace_id,
                    ))

            elif request.action.value == "cancel":
                result = await adapter.cancel_booking(request.existing_appointment_id or "")
                return _store_terminal(OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="cancelled",
                    human_message=f"Appointment {request.existing_appointment_id} cancelled.",
                    result=result,
                    cost=CostRecord(amount=_receipt_usd("schedule_appointment"), currency="USD", basis="per_booking_attempt"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel_used="direct_api:calcom",
                    retriable=False,
                    trace_id=trace_id,
                ))

            elif request.action.value == "check_availability":
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
                return _store_terminal(OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="availability_returned",
                    human_message=f"Found {len(slots)} available slot(s) at {smb.name}.",
                    result={"slots": slots, "smb_name": smb.name, "count": len(slots)},
                    cost=CostRecord(amount=_receipt_usd("schedule_appointment"), currency="USD", basis="per_availability_check"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel_used="direct_api:calcom",
                    retriable=False,
                    trace_id=trace_id,
                ))

        except Exception as exc:
            _calcom_err = str(exc)  # capture for the failure path below
        else:
            _calcom_err = None
    else:
        _calcom_err = "direct_api:calcom not available for this SMB"

    # Async path: voice_ai or web_form via Celery.
    # FIX 2c: if no Celery broker is configured, executing async would leave the
    # operation pending forever. Return an honest synchronous failure instead.
    if not _has_celery_worker():
        return _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="voice_not_provisioned",
            human_message=(
                "No booking channel is configured for this deployment "
                "(CALCOM_API_KEY absent, VAPI_API_KEY absent, no async worker available). "
                "Nothing was booked and nothing was charged."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ))

    # Worker is available — enqueue and return pending_async.
    # ALWAYS register the operation_id as pending in the outcome store first so
    # GET /ops/get_status/<id> resolves immediately.
    get_outcome_store().set_pending(operation_id, "schedule_appointment")

    estimated = datetime.now(timezone.utc) + timedelta(seconds=90)
    _enqueue_async_booking(operation_id, request, smb, agent_id, trace_id)

    channel_chain = ["direct_api:calcom (unavailable)"] if "direct_api:calcom" not in smb.channels_available else []

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.PENDING_ASYNC,
        reason_code="booking_in_progress",
        human_message=f"Booking request submitted for {smb.name}. Estimated completion: {estimated.isoformat()}.",
        cost=CostRecord(amount=_receipt_usd("schedule_appointment"), currency="USD", basis="per_booking_attempt"),
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
    The caller already wrote a 'pending' record to outcome_store, so even if
    Celery is unavailable the operation_id remains queryable via get_status.
    """
    try:
        from reliability.async_runner import enqueue_booking  # type: ignore
        enqueue_booking.delay(operation_id, request.model_dump(), smb.smb_id, agent_id, trace_id)
    except Exception:
        # Celery not available — pending record is already in the store from
        # the handler; nothing further to do here.
        pass


if __name__ == "__main__":  # pragma: no cover
    # Smoke check: write-then-read round trip for the async pending path.
    # Skips the full handler (which needs supply directory + adapters) and
    # exercises just the storage contract this fix relies on.
    import asyncio
    from storage.outcome_store import get_outcome_store
    from core.status_outcome import handle_get_status

    op_id = str(uuid.uuid4())
    get_outcome_store().set_pending(op_id, "schedule_appointment")
    status = asyncio.run(handle_get_status(op_id))
    assert status["status"] == "pending", f"expected pending, got {status}"
    print(f"smoke check passed: operation_id={op_id} resolved to status={status['status']}")
