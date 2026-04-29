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
    if not smbs:
        coverage_note = (
            f"No verified businesses in our supply network for "
            f"{request.vertical.value} near {request.location.zip_or_city}."
            " Consider expanding radius_miles or trying adjacent verticals."
        )

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

    if records:
        increment_businesses_found(len(records))

    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS,
        reason_code="businesses_found" if smbs else "no_results",
        human_message=f"Found {len(smbs)} verified businesses." if smbs else "No verified businesses matched your criteria.",
        result=result,
        cost=CostRecord(amount=0.01, currency="USD", basis="per_call"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=None,
        retriable=False,
        trace_id=trace_id,
    )
