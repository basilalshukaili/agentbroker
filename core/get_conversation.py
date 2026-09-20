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
from core.ownership import Denial, owner_for_storage, read_denial


def _refuse(denial: Denial, operation_id: str, t0: float,
            trace_id: Optional[str]) -> OutcomeReceipt:
    """A denial, in the same shape as every other refusal this handler makes."""
    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code=denial.reason_code,
        human_message=denial.human_message,
        result={},
        cost=CostRecord(amount=0.0, currency="USD", basis="no_charge"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )


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

    # AN UNIDENTIFIED CALLER IS REFUSED BEFORE WE LOOK ANYTHING UP.
    #
    # `reference` is FOUR DIGITS (core/conversations.new_ref_token) scoped only
    # by a business_number the business itself publishes, so the id a caller
    # needs is not a secret - it is ten thousand guesses. Answering after the
    # lookup would let those guesses be told apart by which error came back;
    # answering before means the reply carries no information about what is
    # stored. agent_id is None only for an in-process call (core/ownership.py).
    if agent_id is not None and owner_for_storage(agent_id) is None:
        return _refuse(
            Denial(reason_code="identity_required",
                   human_message=(
                       "A conversation is readable only by the agent identity "
                       "that opened it, so this tool needs one. Send your key "
                       "as X-Agent-Identity - the same key you send with "
                       "send_message.")),
            operation_id, t0, trace_id)

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

    # Ownership: an agent may only read its own threads.
    #
    # This guard used to carry `and row.get("agent_id")`, with a comment saying
    # rows created before agent attribution stayed readable so nothing would
    # break. The dispatcher never bound a caller onto send_message, so EVERY
    # thread opened through MCP was such a row: the exception was the rule, and
    # a live guard protected nothing. Verified by driving both halves in
    # tests/unit/test_conversation_ownership.py before the fix.
    #
    # unowned_is_readable=False - an unowned thread is released to NOBODY.
    # "We cannot prove this is yours" must not resolve to "yes" on a public
    # server, and here it cannot even be argued that the id is a capability:
    # a four-digit reference is guessable (see the guard above). What this
    # costs is stated in docs/PRICING.md: threads opened before this shipped,
    # and threads opened by a caller that sent no key, can no longer be read
    # back by id - their replies still arrive through the inbound webhook.
    denial = read_denial(
        caller_agent_id=agent_id,
        owner_agent_id=row.get("agent_id"),
        subject="conversation",
        unowned_is_readable=False,
    )
    if denial:
        return _refuse(denial, operation_id, t0, trace_id)

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
