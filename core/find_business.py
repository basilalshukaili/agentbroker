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
                "example_payloads": [
                    {"booking_url": "https://cal.com/<handle>", "vertical": request.vertical.value},
                    {"booking_url": "https://calendly.com/<handle>", "vertical": request.vertical.value},
                    {"booking_url": "https://www.doctolib.fr/<specialty>/<city>/<doctor>", "vertical": "professional_services"},
                    {"booking_url": "https://booksy.com/en-us/<id>_<name>", "vertical": "personal_services"},
                    {"booking_url": "https://www.opentable.com/r/<restaurant-slug>", "vertical": "restaurants"},
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
        )
        for smb in smbs
    ]

    result: dict = {
        "businesses": [r.model_dump() for r in records],
        "total_in_supply_network": directory.size(),
    }
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
        human_message=(
            f"Found {len(smbs)} verified businesses." if smbs
            else "No verified businesses matched. Use import_booking_url to add a specific URL."
        ),
        result=result,
        cost=CostRecord(amount=0.01, currency="USD", basis="per_call"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        retriable=False,
        trace_id=trace_id,
        next_actions=next_actions,
    )
