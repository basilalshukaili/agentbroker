"""
How much demand a business can absorb.

demand_shaping.py sizes its per-business budget by tier and says so in its own
comment - "a one-chair barber and a 50-seat restaurant tolerate very different
volumes" - but nothing ever passed a tier, so every business on the network got
the same "small" allowance. This module supplies it.

THE SAFETY INVARIANT, and it is the whole design:

    AN INFERRED TIER MAY ONLY LOWER A BUSINESS'S BUDGET, NEVER RAISE IT.

We do not know how big these businesses are. `SMBEntry` has no size, seat count
or staff field, and there is no directory that would tell us. So any tier we
compute from a vertical is a GUESS, and a guess that raises a budget is a guess
that lets 40 requests an hour hit a one-chair barber. A guess that lowers one
merely delays somebody. Those are not symmetric, so the code is not symmetric:
inference can reach "micro", never "medium" or "large".

Raising a budget requires a DECLARED tier - set explicitly on the business
record, ideally by the business itself. Evidence, not inference.

That is the same principle as the rest of the demand-shaping layer: when we
cannot prove something, we protect the supply side, because a business that
feels spammed leaves the network and never comes back.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("smb_broker.business_tier")

DEFAULT_TIER = "small"

# Ordered least -> most permissive. Used to enforce the invariant, not just to
# document it: anything above DEFAULT_TIER is unreachable by inference.
TIER_ORDER = ("micro", "small", "medium", "large")

# Verticals whose typical operator is a one- or two-person shop. Inference may
# only move a business DOWN to micro; there is deliberately no mapping upward.
_MICRO_VERTICALS = {
    "personal_services",   # barbers, nail techs, single-chair salons
}

# A business reachable ONLY by phone or WhatsApp is answering messages by hand,
# usually while doing the actual job. That is a capacity signal we can observe
# rather than guess at.
_MANUAL_ONLY_CHANNELS = {"phone", "sms", "whatsapp"}


def _cap_to_default(tier: str) -> str:
    """Enforce the invariant. Inference can never exceed the default."""
    try:
        if TIER_ORDER.index(tier) > TIER_ORDER.index(DEFAULT_TIER):
            return DEFAULT_TIER
    except ValueError:
        return DEFAULT_TIER
    return tier


def infer_tier(smb) -> tuple[str, str]:
    """(tier, reason) from what the record actually tells us. Never raises."""
    if smb is None:
        return DEFAULT_TIER, "no_record"

    declared = getattr(smb, "capacity_tier", None)
    if declared and declared in TIER_ORDER:
        # DECLARED - the only path that may exceed the default, because it is
        # evidence rather than a guess.
        return declared, "declared"

    channels = set(getattr(smb, "channels_available", None) or [])
    vertical = getattr(smb, "vertical", None)
    vertical = getattr(vertical, "value", vertical)

    # An online booking integration means somebody set up software and the
    # calendar absorbs load without a human reading messages.
    has_booking = bool(getattr(smb, "calcom_event_type_id", None)
                       or getattr(smb, "square_location_id", None))

    if not has_booking and channels and channels.issubset(_MANUAL_ONLY_CHANNELS):
        return _cap_to_default("micro"), "manual_channels_only"

    if vertical in _MICRO_VERTICALS and not has_booking:
        return _cap_to_default("micro"), f"vertical:{vertical}"

    return DEFAULT_TIER, "default"


async def resolve_tier(business_id: Optional[str]) -> tuple[str, str]:
    """Look the business up and decide its tier. Falls back safely.

    Never lets a directory failure raise a budget: an unknown business gets the
    default, which is the conservative end of the range.
    """
    if not business_id:
        return DEFAULT_TIER, "no_business_id"
    try:
        from supply.smb_directory import get_directory
        smb = get_directory().get(business_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tier_lookup_failed business=%s err=%s", business_id, exc)
        return DEFAULT_TIER, "lookup_failed"
    return infer_tier(smb)
