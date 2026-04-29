"""
handle_inbound — core operation handler.
Receive, classify, and route inbound messages on behalf of an SMB.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from core.models import HandleInboundRequest, OutcomeReceipt, OperationStatus, CostRecord


_INTENT_SIGNALS = {
    # Higher-specificity intents checked first (dict insertion order = priority order)
    "opt_out": ["stop", "unsubscribe", "remove me", "opt out", "optout"],
    "cancellation": ["cancel", "reschedule", "can't make", "cannot make"],
    "complaint": ["unhappy", "terrible", "awful", "refund", "never again", "disappointed", "no show"],
    "confirmation": ["confirm", "yes", "see you", "i'll be there", "on my way"],
    "inquiry": ["price", "cost", "how much", "what do you", "do you offer", "info", "information"],
    "booking_inquiry": ["book", "appointment", "schedule", "available", "slot", "can i", "do you have"],
}


def _classify_intent(text: str) -> str:
    lower = text.lower()
    for intent, signals in _INTENT_SIGNALS.items():
        if any(s in lower for s in signals):
            return intent
    return "general_inquiry"


_SUGGESTED_ACTIONS = {
    "booking_inquiry": "check_availability",
    "cancellation": "schedule_appointment:cancel",
    "complaint": "escalate_to_human",
    "inquiry": "send_message:reply",
    "confirmation": "no_action_required",
    "opt_out": "consent_revoke",
    "general_inquiry": "send_message:reply",
}


async def handle_inbound(
    request: HandleInboundRequest,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    operation_id = str(uuid.uuid4())

    intent = _classify_intent(request.raw_message)
    suggested_action = _SUGGESTED_ACTIONS.get(intent, "send_message:reply")

    # Enqueue async for complex routing (complaint → escalation, booking → availability check)
    requires_async = intent in ("booking_inquiry", "complaint", "opt_out")
    estimated = datetime.now(timezone.utc) + timedelta(seconds=5 if not requires_async else 15)

    result = {
        "classified_intent": intent,
        "suggested_action": suggested_action,
        "routed_to": f"{suggested_action}_flow",
        "sender": request.sender.model_dump() if request.sender else {},
        "inbound_channel": request.inbound_channel.value,
    }

    if intent == "opt_out" and request.sender:
        from compliance.consent_store import get_consent_store
        phone = request.sender.phone
        if phone:
            get_consent_store().revoke_consent(phone, request.inbound_channel.value, "marketing", "keyword_STOP")
        result["opt_out_processed"] = True

    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code=f"inbound_classified:{intent}",
        human_message=f"Inbound message classified as '{intent}'. Suggested action: {suggested_action}.",
        result=result,
        cost=CostRecord(amount=0.08, currency="USD", basis="per_inbound"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        channel_used=f"inbound:{request.inbound_channel.value}",
        estimated_completion_time=estimated if requires_async else None,
        next_actions=[f"call {suggested_action}" if suggested_action != "no_action_required" else ""],
        retriable=False,
        trace_id=trace_id,
    )
