"""Which delivery channels can this deployment actually use right now - and say so BEFORE a call.

WHY THIS FILE EXISTS. The 2026-09-30 audit found that a key holder learns that SMS cannot be sent,
and that voice was unprovisioned, only by failing: five keyed messaging attempts died on "No
registered 10DLC campaign" / "sms via twilio is not configured". Worse, the failure arrived AFTER
the paid-tool credit hold, so the first signal a customer got was a hold and a refund. The channel
adapters already refuse honestly (channels/stub_policy.not_configured); what was missing was
asking the question once, early, from one place, and putting the answer where an agent plans its
work: tools/list and the very first step of tools/call.

Availability is read from the environment at CALL time, never cached at import: a channel that is
provisioned (or lost) while the process runs changes the answer on the next request.

"Available" here means CONFIGURED - credentials and a sender are present. It does not mean the
provider has been called and has accepted us (that is /healthz/external's job, and probing a paid
carrier on every request would be its own bug), and it does not promise a send will pass the
compliance gate. Nothing in this module places a call or sends a message.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

SMS = "sms"
EMAIL = "email"
VOICE = "voice"
WHATSAPP = "whatsapp"
CHANNELS = (SMS, EMAIL, VOICE, WHATSAPP)

# adapter-chain names (core/send_message._build_channel_chain) -> channel kind
_KIND_OF = {"sms": SMS, "email": EMAIL, "voice_ai": VOICE, "whatsapp": WHATSAPP}

# Tools whose whole value is a delivery channel. Everything else (find_business, screen_sanctions,
# schedule_appointment's Cal.com booking, ...) does not depend on these four and is never gated here.
CHANNEL_TOOLS = ("send_message", "send_transactional_confirmation", "call_business")


def _set(*names: str) -> bool:
    return all(bool(os.getenv(n, "").strip()) for n in names)


def _truthy(name: str) -> bool:
    """An explicit yes only. A flag that is merely present ("false", "0", "no") must not enable anything."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _stubs_allowed() -> bool:
    try:
        from channels.stub_policy import stubs_allowed
        return bool(stubs_allowed())
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class ChannelState:
    channel: str
    available: bool
    reason: Optional[str] = None     # why not, when not
    simulated: bool = False          # True only when stub channels are allowed (never in production)

    def as_dict(self) -> dict:
        d = {"available": self.available}
        if self.reason:
            d["reason"] = self.reason
        if self.simulated:
            d["simulated"] = True
        return d


def channel_state(channel: str) -> ChannelState:
    if channel == SMS:
        twilio_key_auth = _set("TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "TWILIO_ACCOUNT_SID")
        twilio_legacy_auth = _set("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
        sender = _set("TWILIO_MESSAGING_SERVICE_SID") or _set("TWILIO_FROM_NUMBER")
        if (twilio_key_auth or twilio_legacy_auth) and sender:
            return ChannelState(SMS, True)
        if _stubs_allowed():
            return ChannelState(SMS, True, simulated=True)
        return ChannelState(
            SMS, False,
            "SMS is not enabled on this deployment: no carrier account or sender number is "
            "configured, and US business SMS also needs a registered 10DLC campaign.")
    if channel == EMAIL:
        if _set("RESEND_API_KEY") or _set("SENDGRID_API_KEY"):
            return ChannelState(EMAIL, True)
        if _stubs_allowed():
            return ChannelState(EMAIL, True, simulated=True)
        return ChannelState(EMAIL, False, "Email sending is not configured on this deployment.")
    if channel == VOICE:
        if _set("VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID"):
            # Configured is not the same as able to call. The outbound line this deployment holds was
            # issued free by the voice vendor, whose documentation says such numbers are inbound-only
            # and US-national, and the account has never placed a call. Until an operator attests
            # (VAPI_OUTBOUND_VERIFIED) that an outbound call has actually succeeded - or connects a
            # line that is known to place calls - saying "available" would send keyed callers into a
            # credit hold and a refund, the failure the 2026-09-30 audit set out to remove.
            if _truthy("VAPI_OUTBOUND_VERIFIED"):
                return ChannelState(VOICE, True)
            if _stubs_allowed():
                return ChannelState(VOICE, True, simulated=True)
            return ChannelState(
                VOICE, False,
                "Voice calling is not enabled on this deployment yet: the outbound phone line has not "
                "been verified for placing calls.")
        if _stubs_allowed() and _set("VAPI_PHONE_NUMBER_ID"):
            return ChannelState(VOICE, True, simulated=True)
        return ChannelState(
            VOICE, False,
            "Voice calling is not provisioned on this deployment (no voice-AI account or outbound "
            "phone number is configured).")
    if channel == WHATSAPP:
        if _set("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_ID"):
            return ChannelState(WHATSAPP, True)
        if _stubs_allowed():
            return ChannelState(WHATSAPP, True, simulated=True)
        return ChannelState(WHATSAPP, False, "WhatsApp is not configured on this deployment.")
    raise ValueError(f"unknown channel {channel!r}")


def all_channel_states() -> dict:
    return {c: channel_state(c) for c in CHANNELS}


def unavailable_summary() -> list:
    """['sms', 'voice'] - the channels that cannot be used right now, in a stable order."""
    return [c for c, s in all_channel_states().items() if not s.available]


# ---------------------------------------------------------------------------
# Per-call: what channels would THIS call use?
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Unavailable:
    tool: str
    reason: str
    unavailable: tuple
    working_alternatives: tuple = field(default_factory=tuple)


def _kinds_for(preferred_channel, recipient_value: str) -> list:
    """The channel kinds send_message would try, in order, for a PARSED preference and recipient.
    Uses the sender's own chain builder so this can never disagree with what actually happens."""
    from core.send_message import _build_channel_chain

    chain = _build_channel_chain(preferred_channel, recipient_value)
    return [_KIND_OF.get(name.split(":")[0], name.split(":")[0]) for name, _adapter in chain]


def gate(tool: str, *, recipient: Optional[str] = None,
         preferred_channel=None) -> Optional[Unavailable]:
    """None if the call may proceed; otherwise why it cannot, before anything is held or charged.

    The caller passes the recipient and preference ALREADY PARSED by the tool's own request model
    (agent_interface/mcp_server._channel_gate), so a malformed request never reaches here and keeps
    the precise validation error it deserves instead of a channel message.
    """
    if tool not in CHANNEL_TOOLS:
        return None
    states = all_channel_states()

    if tool == "call_business":
        if states[VOICE].available:
            return None
        return Unavailable(tool, states[VOICE].reason or "voice unavailable", (VOICE,))

    if not isinstance(recipient, str) or not recipient:
        return None

    if tool == "send_transactional_confirmation":
        kind = EMAIL if "@" in recipient else SMS
        if states[kind].available:
            return None
        alt = tuple(c for c in (EMAIL,) if kind == SMS and states[c].available)
        return Unavailable(tool, states[kind].reason or f"{kind} unavailable", (kind,), alt)

    # send_message
    if preferred_channel is None:
        return None
    try:
        kinds = _kinds_for(preferred_channel, recipient)
    except Exception:  # noqa: BLE001
        return None
    if not kinds or any(states[k].available for k in kinds):
        return None
    reasons = "; ".join(dict.fromkeys(states[k].reason or f"{k} unavailable" for k in kinds))
    # What would actually work: a phone number can still be reached by WhatsApp if it is up; an
    # email address has no other channel. Reported so the agent can change ONE argument and retry.
    if "@" in recipient:
        others: tuple = ()
    else:
        others = tuple(c for c in (WHATSAPP,) if c not in kinds and states[c].available)
    return Unavailable(tool, reasons, tuple(dict.fromkeys(kinds)), others)


# ---------------------------------------------------------------------------
# tools/list: tell the agent before it plans around a tool that cannot deliver
# ---------------------------------------------------------------------------

def _tool_notice(tool: str, states: dict) -> Optional[dict]:
    """{'available': bool, 'reason': str, 'unavailable_channels': [...]} or None if all is well."""
    if tool == "call_business":
        s = states[VOICE]
        if s.available:
            return None
        return {"available": False, "reason": s.reason, "unavailable_channels": [VOICE]}
    if tool == "send_transactional_confirmation":
        down = [c for c in (SMS, EMAIL) if not states[c].available]
        if not down:
            return None
        if len(down) == 2:
            return {"available": False,
                    "reason": f"{states[SMS].reason} {states[EMAIL].reason}",
                    "unavailable_channels": down}
        return {"available": True, "unavailable_channels": down,
                "reason": states[down[0]].reason}
    if tool == "send_message":
        down = [c for c in CHANNELS if not states[c].available]
        if not down:
            return None
        if len(down) == len(CHANNELS):
            return {"available": False,
                    "reason": "No delivery channel is configured on this deployment.",
                    "unavailable_channels": down}
        return {"available": True, "unavailable_channels": down,
                "reason": "; ".join(dict.fromkeys(states[c].reason or "" for c in down if states[c].reason))}
    return None


def annotate_tools(tools: list) -> list:
    """Add availability to the channel tools of a tools/list result. Returns NEW tool dicts for the
    ones that change and the originals for the rest; never mutates the cached manifest."""
    states = all_channel_states()
    out = []
    for tool in tools:
        notice = _tool_notice(tool.get("name", ""), states) if isinstance(tool, dict) else None
        if not notice:
            out.append(tool)
            continue
        new = dict(tool)
        meta = dict(new.get("_meta") or {})
        meta["hatchloop/availability"] = notice
        new["_meta"] = meta
        if notice["available"]:
            names = ", ".join(c.upper() if c == SMS else c.capitalize()
                              for c in notice["unavailable_channels"])
            new["description"] = f"{tool.get('description', '')} [Not available on this deployment: {names}.]"
        else:
            new["description"] = (
                f"[UNAVAILABLE on this deployment: {notice['reason']}] {tool.get('description', '')}")
        out.append(new)
    return out


def instructions_notice() -> str:
    """One sentence for the `initialize` instructions; '' when every channel is up."""
    down = unavailable_summary()
    if not down:
        return ""
    names = ", ".join(c.upper() if c == SMS else c for c in down)
    return (f"Delivery channels not available on this deployment right now: {names}. Tools that "
            f"depend on them are marked in tools/list and fail immediately with channel_unavailable "
            f"(nothing is held or charged). ")
