"""
verify_business — core operation handler.
Confirms an SMB is real, operating, and capable of the requested service.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from core.models import VerifyBusinessRequest, OutcomeReceipt, OperationStatus, CostRecord
from supply.smb_directory import get_directory


async def handle_verify_business(
    request: VerifyBusinessRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    directory = get_directory()
    smb = directory.get(request.smb_id)

    if not smb or not smb.active:
        return OutcomeReceipt(
            operation_id=str(uuid.uuid4()),
            status=OperationStatus.FAILURE,
            reason_code="supply_unreachable",
            human_message=f"SMB {request.smb_id} not found in supply network.",
            cost=CostRecord(amount=0.02, currency="USD", basis="per_call"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    capability_confirmed = True
    if request.capability_to_verify:
        capability_confirmed = request.capability_to_verify.lower() in [
            c.lower() for c in smb.capabilities
        ]

    verified = capability_confirmed and bool(smb.channels_available)
    result = {
        "verified": verified,
        "capabilities_confirmed": smb.capabilities if capability_confirmed else [],
        "channels_reachable": smb.channels_available,
        "last_verified_at": smb.verified_at.isoformat() if smb.verified_at else None,
        "verification_method": "directory_lookup",
    }
    # On a capability miss, surface what IS valid so the agent can retry without guessing.
    if not capability_confirmed:
        result["valid_capabilities"] = list(smb.capabilities)

    if verified:
        human_message = "Business verified."
    elif not capability_confirmed:
        valid = ", ".join(smb.capabilities) if smb.capabilities else "(none registered)"
        human_message = (
            f"Capability '{request.capability_to_verify}' not confirmed for this SMB. "
            f"Valid capabilities: {valid}."
        )
    else:
        human_message = f"Capability '{request.capability_to_verify}' not confirmed for this SMB."

    return OutcomeReceipt(
        operation_id=str(uuid.uuid4()),
        status=OperationStatus.SUCCESS if verified else OperationStatus.FAILURE,
        reason_code="verified" if verified else "capability_not_found",
        human_message=human_message,
        result=result,
        cost=CostRecord(amount=0.02, currency="USD", basis="per_call"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )
