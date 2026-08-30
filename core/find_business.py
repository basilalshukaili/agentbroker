"""
find_business — core operation handler.
Returns verified SMBs from supply network matching the given criteria.
"""
from __future__ import annotations

import time
import uuid

from core.models import (
    FindBusinessRequest, OutcomeReceipt, OperationStatus, SMBRecord, CostRecord
)
from supply.smb_directory import get_directory
from telemetry.metrics import increment_businesses_found
from billing.pricing import receipt_usd as _receipt_usd


async def handle_find_business(
    request: FindBusinessRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    directory = get_directory()

    smbs = directory.search(
        vertical=request.vertical,
        zip_or_city=request.location.zip_or_city,
        capability=request.capability,
        max_usd=request.price_band.max_usd if request.price_band else None,
        max_results=request.max_results,
    )

    coverage_note = None
    next_actions: list[str] = []
    recovery_payload: dict | None = None
    if not smbs:
        # Honest empty-state: don't blame the agent's query when the directory
        # is genuinely empty. Point at the recovery path explicitly.
        if directory.size() == 0:
            coverage_note = (
                "Directory is currently empty. The supply network grows from "
                "agent demand: call `import_booking_url` with any public booking "
                "URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, "
                "Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) "
                "to add a real business in one round-trip. Future find_business "
                "calls in that vertical+location will return it."
            )
            next_actions = [
                "Call import_booking_url with any URL the user mentioned",
                "Or call /supply/platforms to see all 12 supported booking platforms with examples",
            ]
            recovery_payload = {
                "operation": "import_booking_url",
                "instructions": (
                    "Ask the user for ANY public booking URL on a supported platform "
                    "(Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, "
                    "Square, Acuity, Schedulista, Squarespace, BookMyCity). Then call "
                    "import_booking_url with that URL. The examples below show the "
                    "URL SHAPE for each platform — do NOT call import_booking_url "
                    "with these literal example URLs; substitute a real URL from the user."
                ),
                "example_payloads": [
                    {"booking_url": "https://cal.com/peer", "vertical": request.vertical.value,
                     "note": "Replace 'peer' with the actual Cal.com handle the user gave you."},
                    {"booking_url": "https://calendly.com/acme/intro", "vertical": request.vertical.value,
                     "note": "Replace 'acme/intro' with the actual Calendly path the user gave you."},
                    {"booking_url": "https://www.doctolib.fr/dentiste/paris/jean-dupont", "vertical": "professional_services",
                     "note": "Replace path segments with the actual Doctolib URL the user gave you."},
                    {"booking_url": "https://booksy.com/en-us/123_jane-salon", "vertical": "personal_services",
                     "note": "Replace '123_jane-salon' with the actual Booksy slug the user gave you."},
                    {"booking_url": "https://www.opentable.com/r/acme-bistro-tokyo", "vertical": "personal_services",
                     "note": "Replace 'acme-bistro-tokyo' with the actual OpenTable slug the user gave you."},
                ],
                "supported_platforms_endpoint": "/supply/platforms",
            }
        else:
            coverage_note = (
                f"No verified businesses in our supply network for "
                f"{request.vertical.value} near {request.location.zip_or_city}. "
                f"Directory has {directory.size()} businesses overall — try a "
                "broader location, an adjacent vertical, or call "
                "`import_booking_url` with a specific URL to add one."
            )
            next_actions = ["Call import_booking_url if the user named a specific URL"]

    records = [
        SMBRecord(
            smb_id=smb.smb_id,
            name=smb.name,
            vertical=smb.vertical,
            address=f"{smb.address}, {smb.city}, {smb.state} {smb.zip_code}",
            capabilities=smb.capabilities,
            channels_available=smb.channels_available,
            price_range=smb.price_range,
            verified_at=smb.verified_at,
            rank_score=round(len(smb.channels_available) / 3, 2),
            is_demo=getattr(smb, "is_demo", False),
        )
        for smb in smbs
    ]
    any_demo = any(r.is_demo for r in records)

    result: dict = {
        "businesses": [r.model_dump() for r in records],
        "total_in_supply_network": directory.size(),
    }
    # A FILTER THAT DOES NOT FILTER MUST SAY SO.
    #
    # availability_window is advertised as an object with start_iso/end_iso and
    # is read by nothing: an identical five-result set came back for "no
    # window" and for a one-minute window in 1999. Until today it never even
    # reached the handler, so it was inert twice over. Forwarding it made it
    # VALIDATE - a malformed value now returns a clean -32602 - which reads to
    # an agent as support.
    #
    # Same treatment as screen_sanctions' country and entity_type notes:
    # accepted, not applied, and disclosed in the response rather than left
    # for the caller to discover by comparing result sets.
    if getattr(request, "availability_window", None) is not None:
        result["availability_window_applied"] = False
        result["availability_window_note"] = (
            "availability_window was accepted but did NOT narrow these "
            "results - we do not hold live calendars for the supply network, "
            "so we cannot filter on free/busy without calling each business. "
            "Use schedule_appointment with requested_time to book a specific "
            "slot; it checks real availability and refuses rather than "
            "booking a different time.")
    if any_demo:
        result["sandbox_notice"] = (
            "Some results are sandbox entries (is_demo=true, names prefixed "
            "with [DEMO]). Booking calls on these short-circuit with a "
            "demo_smb_no_live_booking receipt instead of contacting real "
            "businesses. Use import_booking_url to add a real business."
        )
    if coverage_note:
        result["supply_coverage_note"] = coverage_note
    if recovery_payload:
        result["recovery"] = recovery_payload

    if records:
        increment_businesses_found(len(records))

    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="businesses_found" if smbs else "no_results",
        # SAY WHEN A RESULT IS SAMPLE DATA. The records already carry an
        # `is_demo` flag and their names are prefixed [DEMO] - the data was
        # honest and only the prose was not. Asking Atlanta for a haircut
        # returned "[DEMO] Cuts & Co." under the sentence "Found 1 verified
        # businesses", and an agent reads the sentence.
        #
        # Counting them separately rather than hiding them: a demo row is
        # genuinely useful for testing an integration, and useless for booking
        # an actual appointment. The caller has to be able to tell which it got.
        human_message=(
            (
                f"Found {len(smbs)} business(es)"
                + (f", of which {sum(1 for s in smbs if getattr(s, 'is_demo', False))} "
                   f"are SAMPLE DATA (named [DEMO], flagged is_demo) and cannot "
                   f"be transacted with"
                   if any(getattr(s, "is_demo", False) for s in smbs) else "")
                + "."
            ) if smbs
            else "No businesses matched in our supply network, which is still "
                 "small. Use import_booking_url to add a specific URL."
        ),
        result=result,
        cost=CostRecord(amount=_receipt_usd("find_business"), currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        retriable=False,
        trace_id=trace_id,
        next_actions=next_actions,
    )
