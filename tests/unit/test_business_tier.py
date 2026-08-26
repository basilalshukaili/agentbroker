"""
Business capacity tiering.

demand_shaping sizes its budget by tier and its own comment says a one-chair
barber and a 50-seat restaurant tolerate different volumes - but nothing passed
a tier, so every business got the same "small" allowance (found 2026-08-26).

The tests that matter here are not the happy paths. They are the INVARIANT:

    an INFERRED tier may only LOWER a budget, never raise it.

We have no size data on these businesses, so any tier from a vertical is a
guess. A guess that raises a budget lets 40 requests/hour hit a one-chair
barber; a guess that lowers one merely delays somebody. Not symmetric, so the
code must not be symmetric either.
"""
from __future__ import annotations

import asyncio

import pytest

from core import business_tier as bt


class FakeSMB:
    def __init__(self, **kw):
        self.capacity_tier = kw.get("capacity_tier")
        self.channels_available = kw.get("channels_available", [])
        self.vertical = kw.get("vertical")
        self.calcom_event_type_id = kw.get("calcom_event_type_id")
        self.square_location_id = kw.get("square_location_id")


def _budget(tier):
    from core.demand_shaping import _HOURLY_BUDGET
    return _HOURLY_BUDGET[tier]


# ---------------------------------------------------------------------------
# THE INVARIANT
# ---------------------------------------------------------------------------

def test_inference_can_never_raise_a_budget():
    """The load-bearing property. No inferred tier may exceed the default."""
    default_budget = _budget(bt.DEFAULT_TIER)
    # Every shape of record we could plausibly meet, including hostile ones.
    candidates = [
        FakeSMB(),
        FakeSMB(vertical="personal_services"),
        FakeSMB(vertical="professional_services"),
        FakeSMB(vertical="home_services", calcom_event_type_id="evt_1"),
        FakeSMB(channels_available=["phone"]),
        FakeSMB(channels_available=["email", "sms", "whatsapp", "phone"]),
        FakeSMB(channels_available=[], vertical=None),
        FakeSMB(capacity_tier="not_a_real_tier"),
        FakeSMB(capacity_tier=""),
    ]
    for smb in candidates:
        tier, why = bt.infer_tier(smb)
        assert why != "declared", "these records declare nothing"
        assert _budget(tier) <= default_budget, (
            f"inferred tier {tier!r} ({why}) has budget {_budget(tier)} > "
            f"default {default_budget} - inference must never raise")


def test_cap_helper_clamps_anything_above_default():
    for over in ("medium", "large"):
        assert bt._cap_to_default(over) == bt.DEFAULT_TIER
    assert bt._cap_to_default("micro") == "micro"
    assert bt._cap_to_default("garbage") == bt.DEFAULT_TIER


def test_only_a_declared_tier_may_exceed_the_default():
    """Raising a budget needs evidence, not a guess."""
    tier, why = bt.infer_tier(FakeSMB(capacity_tier="large"))
    assert (tier, why) == ("large", "declared")
    assert _budget(tier) > _budget(bt.DEFAULT_TIER)


def test_declared_tier_is_validated():
    """A junk value must not slip through as if it were declared."""
    tier, why = bt.infer_tier(FakeSMB(capacity_tier="enormous"))
    assert why != "declared"
    assert tier == bt.DEFAULT_TIER


# ---------------------------------------------------------------------------
# The inference itself
# ---------------------------------------------------------------------------

def test_manual_only_channels_infer_micro():
    """Phone/WhatsApp only means a human reads every message, usually mid-job."""
    tier, why = bt.infer_tier(FakeSMB(channels_available=["phone", "whatsapp"]))
    assert tier == "micro"
    assert why == "manual_channels_only"


def test_booking_integration_is_not_micro():
    """Somebody configured software; the calendar absorbs load without a human."""
    tier, _ = bt.infer_tier(FakeSMB(channels_available=["phone"],
                                    calcom_event_type_id="evt_123"))
    assert tier == bt.DEFAULT_TIER


def test_personal_services_without_booking_infers_micro():
    tier, why = bt.infer_tier(FakeSMB(vertical="personal_services",
                                      channels_available=["email"]))
    assert tier == "micro"
    assert why.startswith("vertical:")


def test_enum_vertical_is_handled():
    """The record may carry the enum, not the string."""
    from core.models import Vertical
    tier, _ = bt.infer_tier(FakeSMB(vertical=Vertical.PERSONAL_SERVICES,
                                    channels_available=["email"]))
    assert tier == "micro"


def test_unknown_record_gets_the_default():
    assert bt.infer_tier(None) == (bt.DEFAULT_TIER, "no_record")


# ---------------------------------------------------------------------------
# Resolution never fails open into a bigger budget
# ---------------------------------------------------------------------------

def test_missing_business_id_uses_default():
    assert asyncio.run(bt.resolve_tier(None))[0] == bt.DEFAULT_TIER


def test_directory_failure_falls_back_to_default_not_upward(monkeypatch):
    """A lookup failure must never widen the budget."""
    import supply.smb_directory as sd

    def _boom():
        raise RuntimeError("directory down")
    monkeypatch.setattr(sd, "get_directory", _boom)

    tier, why = asyncio.run(bt.resolve_tier("smb_x"))
    assert tier == bt.DEFAULT_TIER
    assert why == "lookup_failed"
    assert _budget(tier) <= _budget(bt.DEFAULT_TIER)


# ---------------------------------------------------------------------------
# WIRING - a tier computed and not used would be the dead-machinery pattern
# ---------------------------------------------------------------------------

def test_send_message_actually_passes_the_tier(monkeypatch):
    import core.send_message as sm
    import core.demand_shaping as ds
    from core.demand_shaping import BudgetDecision

    seen = {}

    async def _spy(business_id, *, tier="small", **kw):
        seen["tier"] = tier
        return BudgetDecision(allowed=False, reason_code="business_rate_limited",
                              retry_after_ms=60_000, human_message="queued")
    monkeypatch.setattr(ds, "check_budget", _spy)

    async def _micro(_bid):
        return "micro", "manual_channels_only"
    import core.business_tier as _bt
    monkeypatch.setattr(_bt, "resolve_tier", _micro)

    async def _noop_enqueue(**kw):
        return None
    import core.demand_queue as dq
    monkeypatch.setattr(dq, "enqueue", _noop_enqueue)

    from core.models import (SendMessageRequest, Recipient, MessageContent,
                             MessageType, ChannelPreference)
    req = SendMessageRequest(
        recipient=Recipient(id_type="phone", id_value="+96894639405",
                            country_code="OM"),
        content=MessageContent(body="hello"),
        message_type=MessageType.TRANSACTIONAL,
        channel_preference=ChannelPreference.WHATSAPP,
        business_id="smb_1",
    )
    receipt = asyncio.run(sm.handle_send_message(req))
    assert seen.get("tier") == "micro", "the resolved tier must reach check_budget"
    block = receipt.result["demand_shaping"]
    assert block["business_tier"] == "micro"
    assert block["tier_basis"] == "manual_channels_only"


def test_smb_entry_carries_a_declared_tier_field():
    from supply.smb_directory import SMBEntry
    assert "capacity_tier" in SMBEntry.__dataclass_fields__
    assert SMBEntry.__dataclass_fields__["capacity_tier"].default is None
