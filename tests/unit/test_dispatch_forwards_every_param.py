"""The parameters a caller sends must arrive at the handler.

Eight advertised parameters were dropped by _dispatch_operation, which built
each request object field by field and listed only some of them. Nothing was
broken and nothing looked wrong - the parameter was in the manifest, on the
model, validated by pydantic and read by the handler. Only the wire between
them was missing, so it silently became None on every call.

scripts/check_params_reach_dispatch.py is the static guard for the class.
This is the behavioural half: it drives the real dispatch and looks at what
the handler actually receives, because a static check reads code and this
reads consequences.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface.mcp_server import _dispatch_operation


def _capture(monkeypatch, module_path: str, attr: str):
    """Swap a handler for one that records its request and stops."""
    import importlib
    mod = importlib.import_module(module_path)
    box: dict = {}

    class _Stop(Exception):
        pass

    async def _fake(req, *a, **kw):
        box["req"] = req
        raise _Stop

    monkeypatch.setattr(mod, attr, _fake)
    box["_Stop"] = _Stop
    return box


def _drive(box, tool: str, args: dict):
    try:
        asyncio.run(_dispatch_operation(tool, args))
    except box["_Stop"]:
        pass
    except Exception:                               # noqa: BLE001
        # A handler may be reached through a wrapper that catches; the
        # assertion below is what decides the test either way.
        pass
    return box.get("req")


def test_schedule_appointment_delivers_the_time_being_booked(monkeypatch):
    """THE WORST OF THE EIGHT. An agent books 2pm on the 15th, the field is
    dropped in dispatch, and the handler - which reads requested_time in
    eight places - proceeds as though no time was given."""
    box = _capture(monkeypatch, "core.schedule_appointment",
                   "handle_schedule_appointment")
    req = _drive(box, "schedule_appointment", {
        "smb_id": "smb_test",
        "action": "book",
        "service": "drain cleaning",
        "requested_time": {"preferred_iso": "2026-09-15T14:00:00Z",
                           "duration_minutes": 60},
        "customer": {"name": "Sara", "phone": "+15551234567"},
        "notes": "side gate code 4417",
    })
    assert req is not None, "the handler was never reached"
    assert req.requested_time is not None, (
        "the appointment time never reached the handler - the caller asked "
        "for a specific slot and we booked as if they had not")
    assert req.requested_time.preferred_iso.hour == 14
    assert req.customer is not None and req.customer.name == "Sara"
    assert req.notes == "side gate code 4417"


def test_send_message_delivers_the_sender_disclosure(monkeypatch):
    """on_behalf_of is who the message says it is FROM. Dropping it does not
    fail the send; it sends without the disclosure."""
    box = _capture(monkeypatch, "core.send_message", "handle_send_message")
    req = _drive(box, "send_message", {
        "recipient": {"id_type": "phone", "id_value": "+15551234567"},
        "content": {"body": "Your appointment is confirmed."},
        "message_type": "transactional",
        "on_behalf_of": "Sara",
        "business_id": "smb_test",
        "send_at_iso": "2026-09-15T14:00:00Z",
    })
    assert req is not None
    assert req.on_behalf_of == "Sara"
    assert req.business_id == "smb_test"
    assert req.send_at_iso is not None


def test_find_business_delivers_its_filters(monkeypatch):
    box = _capture(monkeypatch, "core.find_business", "handle_find_business")
    req = _drive(box, "find_business", {
        "vertical": "plumbing",
        "location": {"zip_or_city": "Atlanta"},
        "price_band": {"max_usd": 200},
        "availability_window": {"start_iso": "2026-09-15T00:00:00Z",
                                "end_iso": "2026-09-17T00:00:00Z"},
    })
    assert req is not None
    assert req.price_band is not None and req.price_band.max_usd == 200
    assert req.availability_window is not None


@pytest.mark.parametrize("args,field", [
    ({"smb_id": "x", "action": "book", "requested_time": "2026-09-15T14:00:00Z"},
     "requested_time"),
    ({"smb_id": "x", "action": "book", "customer": "Sara"}, "customer"),
])
def test_a_wrong_shape_is_a_typed_error_not_a_500(args, field):
    """Forwarding a parameter also forwards the chance to get it wrong. A
    caller who sends requested_time as a bare ISO string must be told which
    field and why, not handed an internal error."""
    from agent_interface.mcp_server import handle_mcp_request
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "schedule_appointment", "arguments": args}}
    resp = asyncio.run(handle_mcp_request(payload, {"x-agent-identity": "test"}))
    assert "error" in resp, f"a malformed {field} did not produce an error"
    assert resp["error"]["code"] == -32602, (
        f"expected invalid-params, got {resp['error']['code']}")
    assert field in resp["error"]["message"], (
        "the error does not name the offending field")
