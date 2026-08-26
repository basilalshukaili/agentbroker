"""
get_conversation — let an agent READ the thread it started.

Without this the loop is half-built: we open conversations, carry references,
and correlate business replies exactly - but the agent that sent the message
has no way to see what came back. This is the read side of two-way messaging.

Returns the thread state, every message in order, and - importantly - the
correlation CONFIDENCE of each inbound message, so an autonomous agent can
treat an exactly-matched reply differently from an inferred one.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from core.models import CostRecord, OperationStatus, OutcomeReceipt


async def handle_get_conversation(
    conversation_id: Optional[str] = None,
    reference: Optional[str] = None,
    business_number: Optional[str] = None,
    agent_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    from core import conversations as conv

    operation_id = f"getconv_{int(time.time() * 1000)}"

    row: Optional[dict] = None
    if conversation_id:
        row = await conv.get_conversation(conversation_id)
    elif reference:
        # A reference alone is not unique across businesses, so it must be
        # scoped - the same rule the correlation layer enforces.
        if not business_number:
            return OutcomeReceipt(
                operation_id=operation_id,
                status=OperationStatus.FAILURE,
                reason_code="invalid_argument",
                human_message=("A `reference` must be accompanied by `business_number` "
                               "(4-digit references are reused across businesses). "
                               "Alternatively pass `conversation_id`."),
                result={},
                cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                retriable=False,
                trace_id=trace_id,
            )
        row = await conv.find_by_ref(reference, business_number)

    if not row:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="conversation_not_found",
            human_message=("No conversation matched. Pass the `conversation_id` returned "
                           "by send_message, or a `reference` plus `business_number`."),
            result={},
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    # Ownership: an agent may only read its own threads. Rows created before
    # agent attribution (agent_id NULL) stay readable so nothing breaks.
    if agent_id and row.get("agent_id") and row["agent_id"] != agent_id:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="not_your_conversation",
            human_message="This conversation belongs to a different agent identity.",
            result={},
            cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    messages = await conv.messages_for(row["conversation_id"])
    inbound = [m for m in messages if m.get("direction") == "in"]

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code="conversation_found",
        human_message=(
            f"Conversation {row['conversation_id']} with {row.get('business_number')} "
            f"for {row.get('end_user_ref')}: state={row.get('state')}, "
            f"{len(inbound)} repl{'y' if len(inbound) == 1 else 'ies'} received."
        ),
        result={
            "conversation_id": row["conversation_id"],
            "reference": row.get("ref_token"),
            "state": row.get("state"),
            "on_behalf_of": row.get("end_user_ref"),
            "business_number": row.get("business_number"),
            "intent": row.get("intent"),
            "awaiting_reply": row.get("state") == conv.AWAITING_REPLY,
            "reply_count": len(inbound),
            "messages": [
                {
                    "direction": m.get("direction"),
                    "body": m.get("body"),
                    "at": m.get("created_at"),
                }
                for m in messages
            ],
        },
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )
