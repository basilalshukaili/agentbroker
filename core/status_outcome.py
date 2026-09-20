"""
get_status / get_outcome — core operation handlers.

FIX 1 (2026-08-23): Both handlers now call get_async() which reads from
Supabase when the operation is not in the same-process in-memory cache,
making async polling work across requests.
"""
from __future__ import annotations

import time

from core.models import OutcomeReceipt, OperationStatus, CostRecord
from core.ownership import read_denial
from storage.outcome_store import get_outcome_store
from billing.pricing import receipt_usd as _receipt_usd


# WHO MAY READ AN OPERATION RECORD. Neither handler below asked, and a stored
# receipt is not a status line: it names the end-user we messaged on someone's
# behalf, the business, and - for a booking - the customer and the time.
#
# THIS USED TO BE True, ON AN ARGUMENT THAT ASSUMED THE REST OF THE PROMISE
# WAS ALREADY KEPT. The reasoning was: an operation_id is a uuid4 handed back
# only to the caller, so it is a capability, and "unowned" only ever means
# "this caller presented no identity" - so releasing an unowned row costs
# nothing, because the only caller who could ask for it BY ID is the one who
# made it.
#
# That premise was false in production. schedule_appointment's REST route
# authenticated the caller with X-Agent-Identity and then called
# handle_schedule_appointment(req) with no agent_id argument at all - the
# identity was CHECKED and then DROPPED before it reached the handler that
# stores the receipt. call_business, escalate_to_human and
# send_transactional_confirmation dropped it the same way, on both the REST
# route and (for the handler-internal write; the MCP dispatcher's own
# after-the-fact persistence step covered that surface separately) the MCP
# path. So "unowned" did not mean "this caller chose not to authenticate" -
# it also meant "this caller authenticated and we lost it anyway", and this
# flag could not tell the two apart. A reviewer proved it: create a
# validation-failure receipt through the authenticated REST route, then fetch
# it with no key at all.
#
# Every producer of an operation record is now required to pass agent_id
# through to storage (storage/outcome_store.py's set_pending/set_complete),
# the same way core/get_conversation.py and core/send_message.py already do.
# But a row minted before that was true, and a row a caller creates while
# genuinely presenting no identity, are BOTH indistinguishable from a row
# whose owner was silently dropped by a bug - there is no field that records
# which one happened, and inventing one that lets whoever reads first claim
# the row would be a takeover primitive, not a fix. So, matching
# get_conversation's already-established answer to the identical question:
# unowned is answered to NOBODY, not to whoever asks.
#
# The cost, same shape as docs/PRICING.md already accepts for
# get_conversation: the free, keyless async-polling flow the docs describe
# for get_status/get_outcome no longer works for a truly-anonymous operation
# - a caller that books with no X-Agent-Identity can no longer poll that
# operation back by id. docs/PRICING.md's "an operation created without one
# is protected by its operation_id" line is now stale and should be updated
# alongside this fix.
_UNOWNED_OPERATIONS_ARE_READABLE = False


async def handle_get_status(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    store = get_outcome_store()
    record = await store.get_async(operation_id)
    if not record:
        out = {
            "operation_id": operation_id,
            "status": "not_found",
            "error": "No operation found with this ID.",
        }
        if trace_id:
            out["trace_id"] = trace_id
        return out

    denial = read_denial(
        caller_agent_id=agent_id,
        owner_agent_id=record.get("agent_id"),
        subject="operation",
        unowned_is_readable=_UNOWNED_OPERATIONS_ARE_READABLE,
    )
    if denial:
        # Two doors into one record: get_outcome returns the receipt, this
        # returns the status and the partial_result that leads to it. Guarding
        # one of them would be guarding neither.
        out = {
            "operation_id": operation_id,
            "status": "forbidden",
            "reason_code": denial.reason_code,
            "error": denial.human_message,
        }
        if trace_id:
            out["trace_id"] = trace_id
        return out

    out = {
        "operation_id": operation_id,
        "status": record.get("status", "pending"),
        "estimated_completion_time": record.get("estimated_completion_time"),
        "last_updated_at": record.get("updated_at"),
        "partial_result": record.get("partial_result"),
    }
    # TRACE_ID IS ADVERTISED FOR CORRELATION AND WAS BEING DROPPED. A caller
    # passing it got nothing back to correlate against, which is the whole
    # point of sending one. Echoing it is the minimum that makes the parameter
    # real; it costs nothing and it is what a caller asked for.
    if trace_id:
        out["trace_id"] = trace_id
    return out


async def handle_get_outcome(
    operation_id: str,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    store = get_outcome_store()
    record = await store.get_async(operation_id)

    if not record:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="not_found",
            human_message=f"No operation found with ID {operation_id}.",
            cost=CostRecord(amount=_receipt_usd("get_status"), currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    denial = read_denial(
        caller_agent_id=agent_id,
        owner_agent_id=record.get("agent_id"),
        subject="operation",
        unowned_is_readable=_UNOWNED_OPERATIONS_ARE_READABLE,
    )
    if denial:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code=denial.reason_code,
            human_message=denial.human_message,
            # get_outcome is free, and a refusal is never charged.
            cost=CostRecord(amount=_receipt_usd("get_outcome"), currency="USD",
                            basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    if record.get("status") in ("pending", "executing"):
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.PENDING_ASYNC,
            reason_code="still_in_progress",
            human_message="Operation is still in progress. Poll get_status or await webhook.",
            cost=CostRecord(amount=_receipt_usd("get_status"), currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    outcome = record.get("outcome")
    if outcome:
        try:
            return OutcomeReceipt(**outcome)
        except Exception:
            # outcome may be a plain dict from Supabase; build best-effort receipt.
            #
            # BEST-EFFORT MUST NOT MEAN OPTIMISTIC. This fallback used to
            # default a missing/invalid status to "success", the message to
            # "Operation completed." and the cost to a hardcoded $0.001 - so a
            # malformed stored FAILURE could be echoed back as a successful,
            # charged operation, and get_outcome (a free tool) reported a
            # charge that never existed. Unknown is unknown: preserve what the
            # record actually says, and where it says nothing, say that.
            try:
                _status = OperationStatus(outcome.get("status"))
            except Exception:  # noqa: BLE001 - missing or unrecognised status
                _status = OperationStatus.FAILURE
            _reason = outcome.get("reason_code") or (
                "completed" if _status != OperationStatus.FAILURE
                else "outcome_record_malformed")
            try:
                _cost = CostRecord(**(outcome.get("cost") or {}))
            except Exception:  # noqa: BLE001
                # get_outcome itself is free; never invent a charge here.
                _cost = CostRecord(amount=0.0, currency="USD", basis="free")
            return OutcomeReceipt(
                operation_id=outcome.get("operation_id", operation_id),
                status=_status,
                reason_code=_reason,
                human_message=outcome.get("human_message") or (
                    "The stored outcome record for this operation could not be "
                    "fully reconstructed. The fields returned are exactly what "
                    "was stored - nothing has been assumed."),
                result=outcome.get("result"),
                cost=_cost,
                latency_ms=int((time.monotonic() - t0) * 1000),
                retriable=False,
                trace_id=trace_id,
            )

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.FAILURE,
        reason_code="outcome_not_stored",
        human_message="Operation completed but outcome record is missing. Contact support.",
        cost=CostRecord(amount=_receipt_usd("get_outcome"), currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
    )
