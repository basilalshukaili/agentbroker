"""
Async job runner — Celery tasks for async_by_default operations.
Every async job has a terminal state. No fire-and-forget (§9.17).
"""
from __future__ import annotations

import os
from typing import Any

# Celery app — broker/backend from env vars
try:
    from celery import Celery  # type: ignore
    _celery_app = Celery(
        "smb_broker",
        broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    )
    _celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_max_retries=3,
        task_default_retry_delay=30,
    )
    CELERY_AVAILABLE = True
except ImportError:
    _celery_app = None
    CELERY_AVAILABLE = False


def _get_app():
    if not CELERY_AVAILABLE or _celery_app is None:
        raise RuntimeError("Celery not available")
    return _celery_app


if CELERY_AVAILABLE and _celery_app:
    @_celery_app.task(bind=True, name="smb_broker.enqueue_booking", max_retries=3)
    def enqueue_booking(self, operation_id: str, request_data: dict, smb_id: str,
                        agent_id: str | None, trace_id: str | None) -> dict[str, Any]:
        """Execute an async appointment booking."""
        import asyncio
        from storage.outcome_store import get_outcome_store
        from channels.direct_api.calcom import CalComAdapter, BookingOutcomeUnknown
        from channels.voice_ai.vapi import VapiVoiceAdapter
        from supply.smb_directory import get_directory

        store = get_outcome_store()
        store.set_executing(operation_id)

        # AGENT_ID MUST BE PASSED EXPLICITLY ON EVERY set_complete CALL BELOW.
        #
        # This task runs in the Celery WORKER process, which is a separate
        # Python process from the API server that enqueued it — its
        # OutcomeStore is a fresh, empty in-memory singleton, not the one that
        # holds the owner set_pending recorded. set_complete's own
        # "keep the owner we hold, don't let a callsite that forgot it clear
        # it" guard (storage/outcome_store.py) only protects same-process
        # double-writes; here there is no earlier in-memory write to fall
        # back on. Calling set_complete(operation_id, result) with no
        # agent_id creates a fresh, OWNERLESS local record and then durably
        # upserts {"agent_id": None} over Supabase's "operations" row via
        # PostgREST merge-duplicates — which REPLACES the column, clearing
        # the owner the API server had already recorded. Passing agent_id
        # here (this task's own parameter, sourced from
        # core/schedule_appointment.py's _enqueue_async_booking) is the only
        # way this process can know it.
        try:
            loop = asyncio.new_event_loop()
            directory = get_directory()
            smb = directory.get(smb_id)

            if not smb:
                result = {
                    "operation_id": operation_id,
                    "status": "failure",
                    "reason_code": "supply_unreachable",
                    "human_message": f"SMB {smb_id} not found.",
                    "retriable": False,
                }
                store.set_complete(operation_id, result, agent_id=agent_id)
                return result

            # Try Cal.com first
            if smb.calcom_event_type_id:
                # THIS BLOCK CARRIED THE SAME DEFECT PATTERN JUST REPAIRED ON
                # THE SYNCHRONOUS PATH (core/schedule_appointment.py), left
                # unfixed because exercising a Celery task needs a real
                # broker -- proven instead by calling this function's own
                # body directly via `.__wrapped__` with no broker at all;
                # see tests/unit/test_async_booking_defect.py.
                #
                #   - booking.get("status") was never read, so a PENDING
                #     Cal.com response (accepted by us, not by the business)
                #     came back reason_code "appointment_confirmed".
                #   - booking.get("uid", operation_id) fell back to a LOCALLY
                #     GENERATED id whenever Cal.com returned none -- a
                #     booking this process cannot even name at the provider,
                #     reported confirmed.
                #   - Both were charged a HARDCODED FLAT $1.00 -- not even
                #     the sync path's own max, a distinct wrong number with
                #     no basis in billing/pricing.py's table.
                #   - slots[0] was booked unconditionally; nothing here read
                #     the caller's preferred_iso.
                #
                # Fixed by reusing the SYNC PATH'S OWN honesty gate and slot
                # selection (_choose_slot, _CONFIRMED_PROVIDER_STATES),
                # imported directly rather than re-implemented, so the two
                # paths cannot drift apart again -- and by pricing a genuine
                # confirmation from billing/pricing.py instead of a
                # hardcoded number.
                from datetime import datetime, timezone, timedelta
                from core.schedule_appointment import (
                    _choose_slot, _CONFIRMED_PROVIDER_STATES,
                )
                from billing.pricing import receipt_usd as _receipt_usd_async

                adapter = CalComAdapter()

                rt = request_data.get("requested_time") or {}
                pref_raw = rt.get("preferred_iso") if isinstance(rt, dict) else None
                pref = None
                if pref_raw:
                    try:
                        pref = (pref_raw if isinstance(pref_raw, datetime)
                                 else datetime.fromisoformat(
                                     str(pref_raw).replace("Z", "+00:00")))
                        if pref.tzinfo is None:
                            pref = pref.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        pref = None

                date_from = (pref or datetime.now(timezone.utc)).isoformat()
                date_to = (
                    (pref + timedelta(days=1)) if pref
                    else datetime.now(timezone.utc) + timedelta(days=3)
                ).isoformat()

                slots = loop.run_until_complete(
                    adapter.get_availability(smb.calcom_event_type_id, date_from, date_to)
                )

                def _slot_time(s: dict):
                    return s.get("start") or s.get("time")

                if slots:
                    slot = _choose_slot(slots, pref)
                    if slot is None:
                        # THE REQUESTED TIME IS NOT AVAILABLE. Booking a
                        # different one and calling it confirmed is exactly
                        # the defect this exists to stop -- offer, do not
                        # book, and charge nothing. Mirrors the sync path's
                        # own requested_time_unavailable outcome.
                        offered = [t for t in (_slot_time(s) for s in slots[:5]) if t]
                        result = {
                            "operation_id": operation_id,
                            "status": "success",
                            "reason_code": "requested_time_unavailable",
                            "human_message": (
                                f"NOT BOOKED: {smb.name} has nothing at the "
                                f"requested time. Nothing was reserved and "
                                f"nothing was charged."
                            ),
                            "result": {
                                "booked": False,
                                "requested_time": pref.isoformat() if pref else None,
                                "available_slots": offered,
                                "smb_name": smb.name,
                            },
                            "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
                            "retriable": False,
                        }
                        store.set_complete(operation_id, result, agent_id=agent_id)
                        _fire_webhook(operation_id, result, trace_id)
                        return result

                    booking = loop.run_until_complete(adapter.book_slot(
                        event_type_id=smb.calcom_event_type_id,
                        start=_slot_time(slot),
                        name=request_data.get("customer", {}).get("name", "Customer"),
                        email=request_data.get("customer", {}).get("email", "noreply@example.com"),
                    ))

                    # HONESTY GATE -- identical rule to the sync path.
                    # Success requires BOTH a real provider id AND an
                    # accepted/confirmed provider state; anything else is
                    # its own distinguishable, UNCHARGED outcome.
                    provider_booking_id = (
                        booking.get("uid") if isinstance(booking, dict) else None
                    )
                    provider_status_raw = (
                        booking.get("status") if isinstance(booking, dict) else None
                    )
                    provider_status = str(provider_status_raw or "").strip().lower()

                    if not provider_booking_id:
                        result = {
                            "operation_id": operation_id,
                            "status": "failure",
                            "reason_code": "booking_id_missing",
                            "human_message": (
                                f"Cal.com returned no booking id for "
                                f"{smb.name}, so this cannot be reported as "
                                f"a confirmed appointment. Nothing was "
                                f"charged."
                            ),
                            "result": {
                                "booked": False,
                                "smb_name": smb.name,
                                "provider_status": provider_status_raw,
                            },
                            "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
                            "retriable": False,
                        }
                        store.set_complete(operation_id, result, agent_id=agent_id)
                        _fire_webhook(operation_id, result, trace_id)
                        return result

                    if provider_status not in _CONFIRMED_PROVIDER_STATES:
                        result = {
                            "operation_id": operation_id,
                            "status": "partial",
                            "reason_code": "booking_pending_provider_confirmation",
                            "human_message": (
                                f"{smb.name} has NOT confirmed this "
                                f"appointment yet -- Cal.com reports status "
                                f"'{provider_status_raw or 'unknown'}' for "
                                f"booking {provider_booking_id}. Nothing was "
                                f"charged."
                            ),
                            "result": {
                                "booked": False,
                                "appointment_id": provider_booking_id,
                                "provider_status": provider_status_raw,
                                "requested_time": _slot_time(slot),
                                "smb_name": smb.name,
                            },
                            "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
                            "retriable": False,
                        }
                        store.set_complete(operation_id, result, agent_id=agent_id)
                        _fire_webhook(operation_id, result, trace_id)
                        return result

                    # THE ~15-MINUTE SHIFT, MADE EXPLICIT -- same disclosure
                    # as the sync path.
                    booked_time_str = _slot_time(slot)
                    shift_minutes = 0.0
                    shift_note = ""
                    if pref is not None:
                        try:
                            booked_dt = datetime.fromisoformat(
                                str(booked_time_str).replace("Z", "+00:00"))
                            if booked_dt.tzinfo is None:
                                booked_dt = booked_dt.replace(tzinfo=timezone.utc)
                            shift_minutes = (booked_dt - pref).total_seconds() / 60.0
                        except (TypeError, ValueError):
                            shift_minutes = 0.0
                        if abs(shift_minutes) >= 1:
                            shift_note = (
                                f" Note: this is {abs(shift_minutes):.0f} "
                                f"minute(s) "
                                f"{'after' if shift_minutes > 0 else 'before'} "
                                f"your requested time."
                            )

                    result = {
                        "operation_id": operation_id,
                        "status": "success",
                        "reason_code": "appointment_confirmed",
                        "human_message": (
                            f"Appointment confirmed at {smb.name} for "
                            f"{booked_time_str}.{shift_note}"
                        ),
                        "result": {
                            "appointment_id": provider_booking_id,
                            "confirmed_time": booked_time_str,
                            "provider_status": provider_status_raw,
                            "time_shift_minutes": round(shift_minutes, 2),
                            "smb_name": smb.name,
                            "channel_used": "direct_api:calcom",
                        },
                        "cost": {
                            "amount": _receipt_usd_async("schedule_appointment", at_max=True),
                            "currency": "USD",
                            "basis": "per_confirmed_booking",
                        },
                        "retriable": False,
                    }
                    store.set_complete(operation_id, result, agent_id=agent_id)
                    _fire_webhook(operation_id, result, trace_id)
                    return result

            # FIX 2a: voice AI path is not configured on this deployment.
            # Never fabricate a confirmation -- return an honest failure.
            result = {
                "operation_id": operation_id,
                "status": "failure",
                "reason_code": "voice_not_provisioned",
                "human_message": (
                    f"Voice AI channel (VAPI_API_KEY) is not configured on this deployment. "
                    f"No booking was created at {smb.name} and nothing was charged."
                ),
                "result": {"channel_used": None, "smb_name": smb.name},
                "cost": {"amount": 0.0, "currency": "USD", "basis": "no_charge"},
                "retriable": False,
            }
            store.set_complete(operation_id, result, agent_id=agent_id)
            _fire_webhook(operation_id, result, trace_id)
            return result

        except BookingOutcomeUnknown as exc:
            # SAME DANGEROUS CASE AS THE SYNC PATH (core/schedule_appointment.py),
            # and arguably MORE dangerous here: the generic `except Exception`
            # below does not just report a false "nothing was booked" -- it
            # calls `self.retry(...)`, which is Celery's OWN automatic retry.
            # Left uncaught here, a timeout that happened AFTER Cal.com
            # accepted the booking would be reported "execution_failure" /
            # retriable=True AND Celery would automatically re-run this exact
            # task (up to max_retries=3) with NO human or agent in the loop --
            # compounding the double-booking hazard instead of merely risking
            # it. Reported as its own terminal state instead: uncharged, NOT
            # retriable, and explicitly NOT retried by Celery (no self.retry
            # call) -- an uncertain outcome must stop here, not be retried
            # automatically. See tests/unit/test_async_booking_defect.py's
            # sibling coverage of this exact function.
            result = {
                "operation_id": operation_id,
                "status": "unknown",
                "reason_code": "booking_outcome_unknown",
                "human_message": (
                    f"OUTCOME UNKNOWN booking an appointment at {smb.name}: "
                    f"{exc} This is UNKNOWN, not a confirmed failure -- "
                    f"Cal.com may already have accepted this booking before "
                    f"the error occurred. Nothing was charged, but this "
                    f"task will NOT be retried automatically: verify "
                    f"directly with {smb.name} before submitting a new "
                    f"booking request."
                ),
                "result": {"booked": None, "smb_name": smb.name},
                "cost": {"amount": 0.0, "currency": "USD",
                         "basis": "no_charge_outcome_unknown"},
                "retriable": False,
            }
            store.set_complete(operation_id, result, agent_id=agent_id)
            _fire_webhook(operation_id, result, trace_id)
            return result

        except Exception as exc:
            result = {
                "operation_id": operation_id,
                "status": "failure",
                "reason_code": "execution_failure",
                "human_message": str(exc),
                "retriable": True,
            }
            store.set_complete(operation_id, result, agent_id=agent_id)
            raise self.retry(exc=exc, countdown=30)


def _fire_webhook(operation_id: str, outcome: dict, trace_id: str | None) -> None:
    """Fire signed webhook delivery for this outcome. Non-blocking."""
    try:
        from reliability.webhook_delivery import deliver_webhook
        deliver_webhook.delay(operation_id, outcome, trace_id)
    except Exception:
        pass  # webhook delivery failure does not block outcome storage
