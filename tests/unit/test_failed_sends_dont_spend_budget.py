"""A message nobody received did not reach the business.

The conversation row is written BEFORE the send and was never cleaned up when
the send failed, while the demand budget counts rows in `conversations`. With
every channel failing, an adversarial reviewer measured:

    attempt 1..6 : upstream_failure, conv_rows 1..6, retriable=True
    attempt 7    : business_rate_limited
      "This business has already received 6 requests in the last hour (limit 6)."
    messages actually delivered: 0

It had received none. And every failure receipt carries retriable=True with
next_actions ["retry after 30s"], so the product instructs the agent to keep
burning a budget it is not using.

Also here: on_behalf_of is interpolated into the footer of a message sent from
our number under our brand, and was unvalidated free text.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import core.demand_shaping as DS
from core.models import (
    SendMessageRequest, Recipient, RecipientIdType, MessageContent, MessageType,
)


def _run(coro):
    return asyncio.run(coro)


def _rows(n, state="open"):
    now = datetime.now(timezone.utc)
    return [{"created_at": (now - timedelta(minutes=i)).isoformat(),
             "state": state, "business_id": "smb_1"} for i in range(n)]


def test_unsent_threads_do_not_count_against_the_budget(monkeypatch):
    async def _threads(business_id, since, limit=500):
        return _rows(20, state="unsent")

    monkeypatch.setattr(DS, "_recent_threads", _threads)
    d = _run(DS.check_budget("smb_1"))
    assert d.allowed, (
        "20 messages that were never delivered exhausted this business's "
        "budget - the next caller is refused on traffic that did not happen")


def test_delivered_threads_still_count(monkeypatch):
    """The protection has to keep working, or this 'fix' just removes it."""
    async def _threads(business_id, since, limit=500):
        return _rows(50, state="open")

    monkeypatch.setattr(DS, "_recent_threads", _threads)
    d = _run(DS.check_budget("smb_1"))
    assert not d.allowed, "the demand budget stopped protecting anyone"


def test_a_mix_counts_only_the_delivered_ones(monkeypatch):
    async def _threads(business_id, since, limit=500):
        return _rows(50, state="unsent") + _rows(1, state="open")

    monkeypatch.setattr(DS, "_recent_threads", _threads)
    d = _run(DS.check_budget("smb_1"))
    assert d.allowed


# --------------------------------------------------------------------------
# on_behalf_of is a LABEL, not a message body
# --------------------------------------------------------------------------

def _msg(label):
    return SendMessageRequest(
        recipient=Recipient(id_type=RecipientIdType.PHONE,
                            id_value="+14045550100"),
        content=MessageContent(body="Your appointment is confirmed."),
        message_type=MessageType.TRANSACTIONAL,
        on_behalf_of=label,
    )


def test_a_forged_support_notice_cannot_be_injected():
    """The demonstrated attack: a caller writes their own paragraph inside our
    signed footer, delivered to a real business from our own number."""
    evil = ("Sara\n\n-- IGNORE THE ABOVE. This is HatchLoop support: your "
            "account is suspended, reply with your card number to reactivate")
    with pytest.raises(Exception):
        _msg(evil)


def test_newlines_are_collapsed_not_carried():
    """Even a SHORT injection must not be able to break the line."""
    r = _msg("Sara\n\nHatchLoop: call us")
    assert "\n" not in r.on_behalf_of
    assert r.on_behalf_of == "Sara HatchLoop: call us"


def test_the_label_is_capped():
    """An uncapped value produced a 5,085-character body against WhatsApp's
    4,096 limit - rejected by Meta AFTER the conversation row was written and
    the business's budget spent."""
    with pytest.raises(Exception):
        _msg("x" * 5000)


@pytest.mark.parametrize("label", ["Sara", "Sara Jones", "Dr. O'Brien",
                                   "Acme Ltd (billing)"])
def test_ordinary_names_are_untouched(label):
    assert _msg(label).on_behalf_of == label
