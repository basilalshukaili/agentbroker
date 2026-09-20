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
from channels.direct_api.calcom import (
    CalComAdapter, DestinationNotBound, BookingOutcomeUnknown,
)
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

# Cal.com booking states that mean the provider actually holds the
# appointment. Anything else -- "pending" (awaiting the business's own
# acceptance), "cancelled", "rejected", or a missing/unrecognised value -- is
# NOT a booking we may report as confirmed, no matter how the HTTP call
# itself went. Reproduced against the real adapter: a PENDING response was
# being returned to the caller as reason_code "appointment_confirmed".
_CONFIRMED_PROVIDER_STATES = frozenset({"accepted", "confirmed"})


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


async def _store_terminal(
    receipt: OutcomeReceipt, agent_id: str | None = None, *, durable: bool = False,
) -> OutcomeReceipt:
    """Persist a terminal OutcomeReceipt to the outcome store keyed by operation_id.

    Mirrors the storage contract used by reliability/async_runner.py so a
    subsequent get_status / get_outcome call can resolve the same id.

    agent_id MUST be threaded through from the caller of this function. This
    is every terminal return path in handle_schedule_appointment - the REST
    route (main.py) authenticates the caller and then called
    handle_schedule_appointment(req) with no agent_id at all, so every one of
    these receipts landed here with agent_id=None and was stored with no
    owner. core/status_outcome.py's unowned-read policy then let ANYONE,
    including an anonymous caller, read it back. Reproduced through the REST
    route in tests/unit/test_schedule_appointment_ownership.py.

    `durable=True` AWAITS the durable Supabase write (OutcomeStore's
    set_complete_durable) instead of firing it and hoping.

    set_complete's own durable write is fire-and-forget
    (asyncio.ensure_future, never awaited), which is right for the common
    case -- a slow or unreachable Supabase must never add latency to every
    booking-adjacent rejection -- but it means the ONLY proof a terminal
    receipt existed anywhere outside this process's memory can vanish with
    the process before the scheduled write ever runs. Measured: with a
    fire-and-forget write, a process that returns and is then killed (a
    redeploy, an OOM, a crash) never durably records a booking it already
    told the caller succeeded, so a different process (a horizontally scaled
    peer, this SAME process after an automatic restart, or the idempotency
    gate checking whether a retry is safe) sees "operation not found" for a
    real appointment. That is not acceptable for the three outcomes that
    matter here: a CONFIRMED booking (real money, real appointment), a
    booking pending the provider's own confirmation (a real provider-side
    id another process must be able to find), a CANCELLATION that actually
    ran, and an outcome whose fate is UNKNOWN (the exact state a retry guard
    must be able to see). Every other terminal receipt on this path -
    validation failures, opt-outs, "nothing was available" - carries no
    charge and no upstream side effect, so it keeps the cheap fire-and-forget
    write unchanged.
    """
    try:
        store = get_outcome_store()
        payload = receipt.model_dump(mode="json")
        if durable:
            await store.set_complete_durable(
                receipt.operation_id, payload, agent_id=agent_id)
        else:
            store.set_complete(receipt.operation_id, payload, agent_id=agent_id)
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
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message="existing_appointment_id is required for cancel action.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ), agent_id=agent_id)

    directory = get_directory()
    smb = directory.get(request.smb_id)

    if not smb:
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="supply_unreachable",
            human_message=f"SMB {request.smb_id} not found in supply network.",
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ), agent_id=agent_id)

    # THE BUSINESS NAME IS A STRANGER'S STRING, AND THIS TOOL PRINTS IT TEN
    # TIMES. `smb.name` is whatever the agent that ran import_booking_url
    # supplied, or the <title> scraped off the remote booking page - never the
    # caller's own input, since the caller passes only `smb_id`. Interpolated
    # bare, it put attacker-chosen prose into the sentence a model reads while
    # deciding whether a booking went through. Fenced ONCE here, used
    # everywhere below; `result.smb_name` is fenced by the dispatcher.
    #
    # The demo branch below is deliberately NOT fenced: those names are
    # literals in supply/smb_directory.py, i.e. ours.
    from core.untrusted import fence as _fence_untrusted
    smb_display = _fence_untrusted(smb.name)

    # CRITICAL-1 fix: demo SMBs must never trigger a real charge.
    # The directory contract (smb_directory.py line 39) promises that bookings
    # against demo SMBs short-circuit with reason_code='demo_smb_no_live_booking'
    # instead of contacting real businesses. This guard honours that promise and
    # returns status=failure so _receipt_is_error() returns True in x402_gate,
    # which causes the SDK to SKIP settlement — no USDC charged.
    if getattr(smb, "is_demo", False):
        return await _store_terminal(OutcomeReceipt(
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
        ), agent_id=agent_id)

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
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="recipient_opted_out",
            human_message=(
                f"{smb_display} has opted out of contact through HatchLoop, so we "
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
        ), agent_id=agent_id)

    # Fast path: direct_api:calcom
    # Note: CalComAdapter raises RuntimeError when CALCOM_API_KEY is absent and
    # stubs are not allowed (production). Those exceptions are caught below and
    # routed to the no-worker honest-failure path.
    # A Cal.com SMB uses the calcom fast path whether or not it carries an
    # explicit calcom_event_type_id. import_booking_url stores NONE, so the old
    # `and smb.calcom_event_type_id` clause sent every imported Cal.com business
    # straight to the async/no-worker branch and the false "CALCOM_API_KEY
    # absent" failure. We now derive an event type on the fly from the wired
    # account when the SMB has none — but only one that is BOUND to this
    # business's own booking page (see the resolution block below).
    is_calcom = "direct_api:calcom" in smb.channels_available
    _calcom_err: str | None = None
    if is_calcom:
        adapter = CalComAdapter()
        try:
            # book / check_availability need an event type id; cancel does not
            # (it targets an existing booking uid), so resolve LAZILY — a cancel
            # must never fail on an event-type lookup it does not use.
            #
            # THE DERIVED EVENT TYPE MUST BELONG TO THIS BUSINESS.
            #
            # This used to read `await adapter.get_default_event_type_id()`,
            # which returns the shortest event type on the ONE connected
            # (single-tenant) Cal.com account, bound to nothing. An imported
            # business stores no calcom_event_type_id, so every imported-SMB
            # booking landed on that account's calendar while the receipt
            # below said "Appointment booked at <the business the caller
            # named>". The caller was told it succeeded, so nobody went
            # looking — which makes it worse than a failure, not better.
            #
            # resolve_event_type_id_for_business binds through the business's
            # own booking URL and raises DestinationNotBound when it cannot.
            # We refuse HERE, before availability and before book_slot, because
            # a Cal.com booking emails a real person and there is no undo.
            event_type_id = smb.calcom_event_type_id
            if request.action.value in ("book", "check_availability") and not event_type_id:
                try:
                    event_type_id = await adapter.resolve_event_type_id_for_business(
                        getattr(smb, "website", None))
                except DestinationNotBound as exc:
                    return await _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.FAILURE,
                        reason_code="booking_destination_unmapped",
                        human_message=(
                            f"NOT BOOKED: we hold no calendar mapping for "
                            f"{smb_display}, so a booking would have landed on "
                            f"someone else's calendar. {exc}. Nothing was "
                            f"booked and nothing was charged. Use the "
                            f"business's own booking URL, or ask them to "
                            f"connect their calendar to this network."
                        ),
                        result={"booked": False, "smb_name": smb.name},
                        cost=CostRecord(amount=0.0, currency="USD",
                                        basis="no_charge"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        # Retrying resolves nothing: the mapping does not exist
                        # yet, and only the business can create it.
                        retriable=False,
                        trace_id=trace_id,
                    ), agent_id=agent_id)
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
                    return await _store_terminal(OutcomeReceipt(
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
                    ), agent_id=agent_id)

                slots = await adapter.get_availability(event_type_id, date_from, date_to)
                if not slots:
                    pass  # fall through to the honest calcom failure below
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
                        return await _store_terminal(OutcomeReceipt(
                            operation_id=operation_id,
                            status=OperationStatus.SUCCESS,
                            reason_code="requested_time_unavailable",
                            human_message=(
                                f"NOT BOOKED: {smb_display} has nothing at the "
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
                        ), agent_id=agent_id)
                    booking = await adapter.book_slot(
                        event_type_id=event_type_id,
                        start=_slot_time(slot),
                        name=request.customer.name if request.customer else "Customer",
                        email=request.customer.email if request.customer and request.customer.email else "noreply@example.com",
                        notes=request.notes,
                    )

                    # HONESTY GATE. Reproduced against the real adapter by an
                    # adversarial reviewer:
                    #   (a) booking.get("status") was never read, so a Cal.com
                    #       PENDING response (accepted by us, not yet accepted
                    #       by the business) came back as reason_code
                    #       "appointment_confirmed".
                    #   (b) booking.get("uid", operation_id) FELL BACK TO A
                    #       LOCALLY GENERATED UUID whenever Cal.com's response
                    #       carried no id at all -- a booking we cannot even
                    #       name at the provider was reported confirmed.
                    #   Both were charged the full $0.50 per_confirmed_booking
                    #   fee. Success now requires BOTH a real provider id AND
                    #   an accepted/confirmed provider state; anything else is
                    #   its own distinguishable, UNCHARGED outcome. See
                    #   tests/unit/test_booking_confirmation_honesty.py.
                    provider_booking_id = (
                        booking.get("uid") if isinstance(booking, dict) else None
                    )
                    provider_status_raw = (
                        booking.get("status") if isinstance(booking, dict) else None
                    )
                    provider_status = str(provider_status_raw or "").strip().lower()

                    if not provider_booking_id:
                        return await _store_terminal(OutcomeReceipt(
                            operation_id=operation_id,
                            status=OperationStatus.FAILURE,
                            reason_code="booking_id_missing",
                            human_message=(
                                f"Cal.com returned no booking id for "
                                f"{smb_display}, so this cannot be reported "
                                f"as a confirmed appointment. Nothing was "
                                f"charged. Verify directly with the business "
                                f"before assuming a reservation exists -- do "
                                f"not blindly retry, since the request may "
                                f"already have gone through on Cal.com's "
                                f"side."
                            ),
                            result={
                                "booked": False,
                                "smb_name": smb.name,
                                "provider_status": provider_status_raw,
                            },
                            cost=CostRecord(amount=0.0, currency="USD",
                                            basis="no_charge"),
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            channel_used="direct_api:calcom",
                            retriable=False,
                            trace_id=trace_id,
                        ), agent_id=agent_id)

                    if provider_status not in _CONFIRMED_PROVIDER_STATES:
                        return await _store_terminal(OutcomeReceipt(
                            operation_id=operation_id,
                            status=OperationStatus.PARTIAL,
                            reason_code="booking_pending_provider_confirmation",
                            human_message=(
                                f"{smb_display} has NOT confirmed this "
                                f"appointment yet -- Cal.com reports status "
                                f"'{provider_status_raw or 'unknown'}' for "
                                f"booking {provider_booking_id}. This is not "
                                f"a confirmed booking and nothing was "
                                f"charged. Check back, or contact "
                                f"{smb_display} directly to confirm."
                            ),
                            result={
                                "booked": False,
                                "appointment_id": provider_booking_id,
                                "provider_status": provider_status_raw,
                                "requested_time": _slot_time(slot),
                                "smb_name": smb.name,
                            },
                            cost=CostRecord(amount=0.0, currency="USD",
                                            basis="no_charge"),
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            channel_used="direct_api:calcom",
                            retriable=False,
                            trace_id=trace_id,
                        ), agent_id=agent_id, durable=True)

                    # THE ~15-MINUTE SHIFT, MADE EXPLICIT. _choose_slot may
                    # return a slot up to _SLOT_TOLERANCE away from the
                    # caller's preferred_iso (Cal.com's own grid rarely lands
                    # on the exact minute asked for) -- see the docstring on
                    # _choose_slot. That is a real, if small, difference from
                    # what was agreed, and it used to vanish silently: the
                    # message only ever showed the booked slot time, never
                    # whether or by how much it differed from what was asked.
                    # Surfaced here rather than removed -- rejecting every
                    # non-exact match on a 5- or 15-minute grid would refuse
                    # bookings that should succeed (see
                    # test_the_tolerance_is_minutes_not_days).
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
                                f"your requested time of {pref.isoformat()}."
                            )

                    return await _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.SUCCESS,
                        reason_code="appointment_confirmed",
                        human_message=(
                            f"Appointment booked at {smb_display} for "
                            f"{booked_time_str}.{shift_note}"
                        ),
                        result={
                            "appointment_id": provider_booking_id,
                            "confirmed_time": booked_time_str,
                            "provider_status": provider_status_raw,
                            "requested_time": pref.isoformat() if pref else None,
                            "time_shift_minutes": round(shift_minutes, 2),
                            "smb_name": smb.name,
                            # WHICH CALENDAR IT ACTUALLY LANDED ON. The receipt
                            # named the business and nothing else, so a booking
                            # aimed at the wrong event type was invisible in the
                            # only artefact the caller keeps.
                            "calcom_event_type_id": str(event_type_id),
                            "action": "booked",
                        },
                        cost=CostRecord(amount=_receipt_usd("schedule_appointment", at_max=True), currency="USD", basis="per_confirmed_booking"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        retriable=False,
                        trace_id=trace_id,
                    ), agent_id=agent_id, durable=True)

            elif request.action.value == "cancel":
                # CANCELLATION AUTHORIZATION. Reuses core/ownership.py's
                # read_denial UNCHANGED -- the same "does this caller match
                # the recorded owner, fail closed when the owner cannot be
                # attributed" rule already proven for get_status/get_outcome
                # (tests/unit/test_schedule_appointment_ownership.py). An
                # appointment is exactly the same kind of thing a receipt is:
                # created by one caller, and a different caller presenting a
                # different identity (or none at all) must not be able to
                # act on it just because it can name the provider's booking
                # id.
                #
                # THIS USED TO BE NO CHECK AT ALL. adapter.cancel_booking was
                # called directly against request.existing_appointment_id
                # with nothing upstream of it verifying the caller had
                # anything to do with that booking -- any agent holding (or
                # guessing) a Cal.com uid could cancel any other agent's
                # customer's appointment. Reproduced in
                # tests/unit/test_cancellation_authorization.py.
                #
                # Checked and refused HERE, before adapter.cancel_booking is
                # ever called: a cancellation is an upstream mutation with a
                # real side effect and no undo, so the denial has to land
                # BEFORE the mutation, not as an apology after one that
                # already ran.
                from core.ownership import read_denial

                appointment_owner = await get_outcome_store().get_appointment_owner_async(
                    request.existing_appointment_id or "")
                denial = read_denial(
                    caller_agent_id=agent_id,
                    owner_agent_id=appointment_owner,
                    subject="appointment",
                    unowned_is_readable=False,
                )
                if denial:
                    return await _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.FAILURE,
                        reason_code=denial.reason_code,
                        human_message=denial.human_message,
                        cost=CostRecord(amount=0.0, currency="USD",
                                        basis="no_charge"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        retriable=False,
                        trace_id=trace_id,
                    ), agent_id=agent_id)

                result = await adapter.cancel_booking(request.existing_appointment_id or "")
                return await _store_terminal(OutcomeReceipt(
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
                ), agent_id=agent_id, durable=True)

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
                # DEFECT-4 FIX: get_availability now RAISES on an upstream
                # failure (HTTP error, timeout, transport failure) instead of
                # swallowing it into []. Before this fix, a Cal.com 503 and a
                # genuine "the business has nothing open" both arrived here as
                # an empty list, indistinguishable from each other, so a 503
                # was reported SUCCESS / "availability_returned" / "Found 0
                # available slot(s)" and charged the per_availability_check
                # fee -- a confident, false, CHARGED answer to a question we
                # never got to ask. Caught here, specifically, rather than
                # left to the generic Cal.com-failure handler below, because
                # that handler's message ("Booking via Cal.com did not
                # complete...") describes the wrong action for a call that
                # never attempted a booking.
                try:
                    slots = await adapter.get_availability(event_type_id, date_from, date_to)
                except Exception as exc:
                    return await _store_terminal(OutcomeReceipt(
                        operation_id=operation_id,
                        status=OperationStatus.FAILURE,
                        reason_code="availability_check_failed",
                        human_message=(
                            f"Could not check availability at {smb_display}: "
                            f"the provider could not be reached ({exc}). "
                            f"This is NOT the same as \"no slots\" -- we were "
                            f"unable to ask. Nothing was charged. Retry "
                            f"shortly."
                        ),
                        result=None,
                        cost=CostRecord(amount=0.0, currency="USD",
                                        basis="no_charge"),
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        channel_used="direct_api:calcom",
                        retriable=True,
                        trace_id=trace_id,
                    ), agent_id=agent_id)
                return await _store_terminal(OutcomeReceipt(
                    operation_id=operation_id,
                    status=OperationStatus.SUCCESS,
                    reason_code="availability_returned",
                    human_message=f"Found {len(slots)} available slot(s) at {smb_display}.",
                    result={"slots": slots, "smb_name": smb.name, "count": len(slots)},
                    cost=CostRecord(amount=_receipt_usd("schedule_appointment"), currency="USD", basis="per_availability_check"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    channel_used="direct_api:calcom",
                    retriable=False,
                    trace_id=trace_id,
                ), agent_id=agent_id)

        except BookingOutcomeUnknown as exc:
            # THE DANGEROUS CASE. adapter.book_slot / adapter.cancel_booking
            # raised a failure that does NOT prove the upstream mutation
            # never happened -- a timeout after the request was sent, a 5xx
            # after Cal.com received it, or a 2xx we could not parse. The
            # OLD code caught this in the blanket `except Exception` below
            # and reported reason_code "calcom_booking_failed" /
            # "Nothing was booked and nothing was charged" -- a CONFIDENT,
            # FALSE claim whenever the booking in fact went through upstream.
            # A caller (or an agent) reading that message has every reason
            # to retry, which is exactly how a real business gets
            # double-booked and a real customer gets double-charged.
            #
            # Reported here as its own status (UNKNOWN), its own reason
            # code, uncharged, and NOT retriable -- the caller must verify
            # independently (or poll get_status once it has) before ever
            # sending this request again. Persisted DURABLY (see
            # _store_terminal) so a concurrent or later retry -- through the
            # idempotency gate, or a plain get_status poll -- can see this
            # exact state instead of finding nothing and assuming it is safe
            # to proceed. See tests/unit/test_booking_retry_safety.py.
            is_cancel = request.action.value == "cancel"
            reason_code = (
                "cancellation_outcome_unknown" if is_cancel
                else "booking_outcome_unknown" if request.action.value == "book"
                else "operation_outcome_unknown"
            )
            if is_cancel:
                human_message = (
                    f"OUTCOME UNKNOWN cancelling appointment "
                    f"{request.existing_appointment_id} at {smb_display}: "
                    f"{exc} This is UNKNOWN, not a confirmed failure -- "
                    f"Cal.com may already have cancelled this booking "
                    f"before the error occurred, so this must NOT be read "
                    f"as proof the appointment still stands. Nothing was "
                    f"charged. Verify directly with {smb_display} before "
                    f"assuming either outcome, and before retrying this "
                    f"cancellation."
                )
            else:
                human_message = (
                    f"OUTCOME UNKNOWN booking an appointment at "
                    f"{smb_display}: {exc} This is UNKNOWN, not a confirmed "
                    f"failure -- Cal.com may already have accepted this "
                    f"booking before the error occurred, so this must NOT "
                    f"be read as proof no appointment exists. Nothing was "
                    f"charged for this call, but DO NOT resend this exact "
                    f"request: retrying risks creating a SECOND, real "
                    f"appointment (and a second charge) if the first "
                    f"attempt in fact succeeded. Verify directly with "
                    f"{smb_display}, or poll "
                    f"get_status(operation_id={operation_id!r}) once you "
                    f"have confirmed independently, before deciding whether "
                    f"to retry."
                )
            return await _store_terminal(OutcomeReceipt(
                operation_id=operation_id,
                status=OperationStatus.UNKNOWN,
                reason_code=reason_code,
                human_message=human_message,
                result={
                    "booked": None,
                    "smb_name": smb.name,
                    "action": request.action.value,
                },
                cost=CostRecord(amount=0.0, currency="USD",
                                basis="no_charge_outcome_unknown"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                channel_used="direct_api:calcom",
                next_actions=[
                    f"verify independently with {smb_display} before "
                    f"retrying",
                    f"poll get_status with operation_id {operation_id} "
                    f"after you have verified",
                ],
                # NEVER advertise this as safely retriable -- that is the
                # exact claim this whole branch exists to refuse to make.
                retriable=False,
                trace_id=trace_id,
            ), agent_id=agent_id, durable=True)
        except Exception as exc:
            _calcom_err = str(exc)  # captured for the honest failure below

        # Reaching here for a Cal.com SMB means the sync path returned no
        # receipt: either the book action found no slots (fell through), or a
        # call raised. Report the TRUE reason. The old hardcoded "CALCOM_API_KEY
        # absent / VAPI_API_KEY absent / no async worker" string was false (the
        # key is present and working) and told agents to abandon a live service.
        # status=FAILURE => x402 skips settlement, so this is always no-charge.
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="calcom_booking_failed",
            human_message=(
                f"Booking via Cal.com did not complete for {smb_display}: "
                f"{_calcom_err or 'no availability was returned for the requested window'}. "
                "Nothing was booked and nothing was charged."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            channel_used="direct_api:calcom",
            retriable=True,
            trace_id=trace_id,
        ), agent_id=agent_id)

    # Async path: voice_ai or web_form via Celery (non-Cal.com SMBs only).
    # FIX 2c: if no Celery broker is configured, executing async would leave the
    # operation pending forever. Return an honest synchronous failure instead.
    if not _has_celery_worker():
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="async_channel_not_provisioned",
            human_message=(
                f"{smb_display} can only be booked through an async channel "
                f"({', '.join(smb.channels_available) or 'none'}), which needs a "
                "background worker that is not deployed on this tier. "
                "Nothing was booked and nothing was charged."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        ), agent_id=agent_id)

    # Worker is available — enqueue and return pending_async.
    # ALWAYS register the operation_id as pending in the outcome store first so
    # GET /ops/get_status/<id> resolves immediately.
    #
    # THE OWNER MUST BE SET HERE, NOT ONLY AT COMPLETION. This is the async
    # path's very first write, and it is readable via get_status the instant
    # it lands - before the Celery worker has even picked the job up. An
    # unowned pending row is exactly as readable-by-anyone as an unowned
    # terminal one.
    get_outcome_store().set_pending(operation_id, "schedule_appointment", agent_id=agent_id)

    estimated = datetime.now(timezone.utc) + timedelta(seconds=90)
    if not _enqueue_async_booking(operation_id, request, smb, agent_id, trace_id):
        # The broker is configured but the enqueue itself failed (broker down,
        # import error). Without this check the caller got PENDING_ASYNC with a
        # quoted cost and a 90s estimate for a job that no worker will ever
        # pick up - a booking that wedges in "pending" for the life of the
        # process, silently. Same honest shape as the no-worker branch above.
        return await _store_terminal(OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="async_channel_not_provisioned",
            human_message=(
                f"The booking job for {smb_display} could not be handed to the "
                "background worker (queue unavailable). Nothing was booked and "
                "nothing was charged. Retry in a moment."
            ),
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=True,
            trace_id=trace_id,
        ), agent_id=agent_id)

    channel_chain = ["direct_api:calcom (unavailable)"] if "direct_api:calcom" not in smb.channels_available else []

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.PENDING_ASYNC,
        reason_code="booking_in_progress",
        human_message=f"Booking request submitted for {smb_display}. Estimated completion: {estimated.isoformat()}.",
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


def _enqueue_async_booking(operation_id, request, smb, agent_id, trace_id) -> bool:
    """
    Enqueue a Celery task for async booking execution.
    Returns True only when the task was actually handed to the broker. The
    caller MUST turn False into an honest failure receipt: a swallowed enqueue
    error here used to leave the operation pending forever while the caller
    held a receipt quoting a cost and a completion estimate.
    """
    try:
        from reliability.async_runner import enqueue_booking  # type: ignore
        enqueue_booking.delay(operation_id, request.model_dump(), smb.smb_id, agent_id, trace_id)
        return True
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("smb_broker.schedule_appointment").error(
            "async booking enqueue failed op=%s err=%s", operation_id, exc)
        return False


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
