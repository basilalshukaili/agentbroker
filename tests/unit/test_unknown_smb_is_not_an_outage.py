"""An smb_id that is not in the supply network is answered as exactly that - not as an outage.

THE EVIDENCE (usage_events, 2026-10-01 .. 2026-10-09, docs/reviews/2026-10-09-agentbroker-request-analysis.md):
verify_business was called 30 times by outside callers with an `smb_id` present and answered `supply_unreachable`
every time. That code is the published outage code (api/errors.md: "server_error ... retriable ... the target SMB
could not be reached"); the truth was "this id is not one of ours". An agent that reads `supply_unreachable`
waits and retries, or escalates to a human; an agent that reads `out_of_supply_network` (client_error, not
retriable, already in the published enum) corrects the id. The same wrong code sat in capture_lead and
schedule_appointment, and the most natural way to meet it is the flow our own find_business notice describes:
most find_business results are OpenStreetMap listings whose `osm:` ids no write or verify tool can use.

These tests pin: the code, that it is not retriable, that nothing is charged, that the message names what the id
is and where a usable one comes from, that the caller's id is never echoed raw, and that the docs say so.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.capture_lead import handle_capture_lead
from core.models import (AppointmentAction, CaptureLeadRequest, OperationStatus, ProspectData,
                         ScheduleAppointmentRequest, VerifyBusinessRequest)
from core.schedule_appointment import handle_schedule_appointment
from core.verify_business import handle_verify_business
from reliability.retry_policy import is_retriable

ROOT = Path(__file__).resolve().parents[2]
CODE = "out_of_supply_network"


def _run(coro):
    return asyncio.run(coro)


def _verify(smb_id):
    return _run(handle_verify_business(VerifyBusinessRequest(smb_id=smb_id)))


def _capture(smb_id):
    return _run(handle_capture_lead(CaptureLeadRequest(smb_id=smb_id, prospect=ProspectData(name="Ghost"),
                                                       source="t")))


def _schedule(smb_id):
    return _run(handle_schedule_appointment(ScheduleAppointmentRequest(smb_id=smb_id,
                                                                       action=AppointmentAction.BOOK)))


@pytest.mark.parametrize("call", [_verify, _capture, _schedule], ids=["verify_business", "capture_lead",
                                                                      "schedule_appointment"])
def test_an_unknown_id_is_out_of_the_supply_network_not_unreachable(call):
    r = call("smb_GHOST")
    assert r.status == OperationStatus.FAILURE
    assert r.reason_code == CODE
    assert r.retriable is False
    assert r.cost.amount == 0.0 or r.cost.basis == "free"
    assert "find_business" in r.human_message
    assert "supply_unreachable" not in (r.reason_code or "")
    assert r.next_actions, "a failure the caller can fix must say how"


@pytest.mark.parametrize("call", [_verify, _capture, _schedule], ids=["verify_business", "capture_lead",
                                                                      "schedule_appointment"])
def test_an_openstreetmap_id_is_named_as_one(call):
    r = call("osm:node/1001")
    assert r.reason_code == CODE
    assert "OpenStreetMap" in r.human_message
    assert any("phone or website" in a for a in r.next_actions)


def test_the_callers_id_is_never_echoed_raw():
    hostile = "<script>alert(1)</script>" + "Z" * 400
    for call in (_verify, _capture, _schedule):
        r = call(hostile)
        text = r.human_message + " ".join(r.next_actions)
        assert "<script" not in text
        assert len(r.human_message) < 400


def test_the_retry_policy_treats_the_two_codes_differently():
    """supply_unreachable is in the retry policy's retriable set (it is the outage code); an unknown id must not be."""
    assert is_retriable("supply_unreachable") is True
    assert is_retriable(CODE) is False


def test_the_published_error_table_says_what_the_two_codes_mean():
    doc = (ROOT / "api" / "errors.md").read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if line.startswith("| `out_of_supply_network`"))
    assert "smb_id" in row, "the table must say that an unknown smb_id is answered with this code"
    outage = next(line for line in doc.splitlines() if line.startswith("| `supply_unreachable`"))
    assert "unknown" in outage.lower() and "out_of_supply_network" in outage
