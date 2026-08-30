"""A parameter that does nothing must not look like one that does.

Two advertised parameters were read by no handler. Until dispatch was fixed
on 2026-08-30 they never even reached one, so they were inert twice over -
and forwarding them made things briefly WORSE, because they started to
VALIDATE. A malformed value now returns a clean -32602, which reads to an
agent as support.

The two need different answers, and the difference is what happens if we
stay quiet:

  send_at_iso         -> a message the caller wanted at 9am is sent NOW, to a
                         real phone. No wording in the response undoes that.
                         REFUSE.
  availability_window -> the result set is merely not narrowed. Nothing
                         happens in the world. DISCLOSE.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from core.models import (
    SendMessageRequest, Recipient, RecipientIdType, MessageContent, MessageType,
)
import core.send_message as SM


def _run(coro):
    return asyncio.run(coro)


def _msg(**kw):
    return SendMessageRequest(
        recipient=Recipient(id_type=RecipientIdType.PHONE,
                            id_value="+15551234567"),
        content=MessageContent(body="Your appointment is confirmed."),
        message_type=MessageType.TRANSACTIONAL,
        **kw,
    )


def test_a_future_send_is_refused_not_sent_now():
    r = _run(SM.handle_send_message(_msg(send_at_iso="2027-01-01T09:00:00Z")))
    assert r.reason_code == "scheduling_not_supported"
    assert r.status.value == "failure"
    assert r.cost.amount == 0.0, "charged for a message that was not sent"
    assert "NOT SENT" in r.human_message
    assert r.channel_used is None, (
        "a channel was used - the message went out despite the schedule")


def test_the_refusal_explains_what_to_do_instead():
    r = _run(SM.handle_send_message(_msg(send_at_iso="2027-01-01T09:00:00Z")))
    assert "omit send_at_iso" in r.human_message


def test_a_send_at_iso_of_now_is_not_refused():
    """"Send it now" expressed as a timestamp is a normal thing for an agent
    to do, and must not be turned into an error. The 2-minute tolerance is
    what keeps clock skew from becoming a refusal."""
    from datetime import datetime, timezone
    r = _run(SM.handle_send_message(
        _msg(send_at_iso=datetime.now(timezone.utc).isoformat())))
    assert r.reason_code != "scheduling_not_supported"


def test_no_send_at_iso_is_unaffected():
    r = _run(SM.handle_send_message(_msg()))
    assert r.reason_code != "scheduling_not_supported"


def test_the_manifest_no_longer_promises_scheduling():
    """The description said "Schedule for future delivery; omit for
    immediate" over a field that scheduled nothing."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))          # tests/unit/.. -> agentbroker
    with open(os.path.join(repo, "manifest", "manifest.json"),
              encoding="utf-8") as fh:
        man = json.load(fh)
    op = next(o for o in man["operations"] if o["name"] == "send_message")
    desc = (op["input_schema"]["properties"]["send_at_iso"]["description"])
    assert "NOT SUPPORTED" in desc.upper()
    assert "Schedule for future delivery; omit for immediate" not in desc


def test_availability_window_is_disclosed_as_not_applied():
    """It is accepted and does not narrow anything. Saying so in the response
    is the difference between a limitation and a lie."""
    import core.find_business as FB
    from core.models import FindBusinessRequest

    r = _run(FB.handle_find_business(FindBusinessRequest(
        vertical="personal_services",
        location={"zip_or_city": "Atlanta"},
        availability_window={"start_iso": "2026-09-15T00:00:00Z",
                             "end_iso": "2026-09-16T00:00:00Z"},
    )))
    res = r.result or {}
    assert res.get("availability_window_applied") is False
    assert "did NOT narrow" in res.get("availability_window_note", "")


def test_no_note_when_no_window_is_sent():
    """A caller who did not use the field should not be told about it."""
    import core.find_business as FB
    from core.models import FindBusinessRequest

    r = _run(FB.handle_find_business(FindBusinessRequest(
        vertical="personal_services", location={"zip_or_city": "Atlanta"})))
    assert "availability_window_applied" not in (r.result or {})
