"""A record we stored is readable by the caller that created it. Nobody else.

THE HOLE, IN TWO HALVES, EITHER OF WHICH ALONE WOULD HAVE BEEN HARMLESS.

Half one: the MCP dispatcher built the send_message request with on_behalf_of,
business_id and send_at_iso and no caller identity at all, so every
conversation opened through the MCP surface was stored with agent_id NULL.

Half two: the ownership guard in core/get_conversation.py read

    if agent_id and row.get("agent_id") and row["agent_id"] != agent_id:

whose comment said rows with a NULL agent_id stay readable "so nothing
breaks". Given half one, that was every row on the live server.

Together: any caller holding a conversation id could read somebody else's
message thread, and the id did not even have to be held. get_conversation
also resolves a FOUR-DIGIT reference scoped by business_number, so the id
could be walked - ten thousand guesses against a number the business
publishes itself.

get_status and get_outcome had no ownership question at all; their receipts
carry the end-user named, the business messaged and the appointment booked.

These tests drive the real MCP dispatcher and a real /ops/* route, because the
wire between the caller's token and the stored row is exactly what was
missing, and a test that called the handler directly would have passed
throughout.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent_interface.identity import TokenRequest, issue_token  # noqa: E402
from agent_interface.mcp_server import _dispatch_operation  # noqa: E402


# --------------------------------------------------------------------------
# Fake Supabase - the same in-memory table the threading tests use, since the
# conversation layer resolves storage.supabase_client through late imports.
# --------------------------------------------------------------------------
class FakeSB:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {"conversations": [], "conversation_messages": []}

    async def insert_row(self, table, row):
        self.rows.setdefault(table, []).append(dict(row))
        return dict(row)

    async def upsert_row(self, table, row, on_conflict="id"):
        for r in self.rows.setdefault(table, []):
            if r.get(on_conflict) == row.get(on_conflict):
                r.update(row)
                return r
        self.rows[table].append(dict(row))
        return dict(row)

    async def select_rows(self, table, filters=None, limit=1000, order=None, gte=None):
        out = []
        for r in self.rows.get(table, []):
            if not all(r.get(k) == v for k, v in (filters or {}).items()):
                continue
            if gte and not all(str(r.get(k, "")) >= str(v) for k, v in gte.items()):
                continue
            out.append(r)
        if order:
            col, _, direction = order.partition(".")
            out.sort(key=lambda r: str(r.get(col, "")), reverse=(direction == "desc"))
        return out[:limit]


@pytest.fixture(autouse=True)
def fake_sb(monkeypatch):
    sb = FakeSB()
    import storage.supabase_client as real
    monkeypatch.setattr(real, "insert_row", sb.insert_row)
    monkeypatch.setattr(real, "upsert_row", sb.upsert_row)
    monkeypatch.setattr(real, "select_rows", sb.select_rows)
    return sb


@pytest.fixture(autouse=True)
def whatsapp_delivers(monkeypatch):
    """WhatsApp is the two-way channel, so it is the one that opens a thread."""
    import core.send_message as sm
    from channels.adapter_interface import ChannelResponse

    async def fake_send(req):
        return ChannelResponse(success=True, provider_message_id="wamid.OWN1")

    monkeypatch.setattr(sm._WHATSAPP_ADAPTER, "send", fake_send)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER", "15556677792")


def _token(agent_id: str) -> str:
    return issue_token(TokenRequest(agent_id=agent_id, principal_id="p_" + agent_id)).token


def _headers(agent_id):
    """A caller's headers. None = a caller that presented no identity at all."""
    return {} if agent_id is None else {"x-agent-identity": _token(agent_id)}


A, B = "agent_alice", "agent_mallory"


def _send_as(agent_id, business="+96890000001", who="Sara") -> dict:
    """One send_message through the real dispatcher. Returns the receipt dict."""
    return asyncio.run(_dispatch_operation("send_message", {
        "recipient": {"id_type": "phone", "id_value": business, "country_code": "OM"},
        "content": {"body": "Can Sara book Tuesday 3pm?"},
        "message_type": "transactional",
        "preferred_channel": "whatsapp",
        "on_behalf_of": who,
    }, _headers(agent_id)))


def _read_as(agent_id, **args) -> dict:
    return asyncio.run(_dispatch_operation("get_conversation", args, _headers(agent_id)))


def _thread_of(receipt: dict) -> dict:
    conv = (receipt.get("result") or {}).get("conversation")
    assert conv, "no conversation was opened; receipt was %s" % (receipt,)
    return conv


def _operation_id_of(receipt: dict) -> str:
    op_id = receipt.get("operation_id")
    assert op_id, receipt
    return op_id


# ==========================================================================
# THE ATTACK: A opens a thread; B and an unidentified caller read it.
# ==========================================================================
def test_another_agent_cannot_read_your_thread_by_id():
    conv = _thread_of(_send_as(A))
    stolen = _read_as(B, conversation_id=conv["conversation_id"])
    assert stolen["status"] == "failure", (
        "a second agent identity read a thread it did not open - this is a "
        "cross-caller read of a message thread on a public server")
    assert stolen["reason_code"] in ("not_your_conversation",
                                    "conversation_owner_unknown")
    assert "messages" not in (stolen.get("result") or {})


def test_an_anonymous_caller_cannot_read_your_thread_by_id():
    conv = _thread_of(_send_as(A))
    stolen = _read_as(None, conversation_id=conv["conversation_id"])
    assert stolen["status"] == "failure", (
        "an unidentified caller read an identified agent's thread")
    assert "messages" not in (stolen.get("result") or {})


def test_the_four_digit_reference_is_not_an_enumeration_door():
    """The id does not have to leak. A reference is four digits and the
    business number is public, so an unidentified caller could walk the
    keyspace and read whatever it hit."""
    conv = _thread_of(_send_as(A))
    walked = _read_as(None, reference=conv["reference"],
                      business_number="96890000001")
    assert walked["status"] == "failure"
    assert "messages" not in (walked.get("result") or {})


def test_the_owner_can_still_read_its_own_thread():
    """The half of the fix that is not a refusal: binding the identity has to
    actually work, or the tool above is merely broken rather than guarded."""
    conv = _thread_of(_send_as(A))
    mine = _read_as(A, conversation_id=conv["conversation_id"])
    assert mine["status"] == "success", mine
    assert mine["result"]["conversation_id"] == conv["conversation_id"]
    assert mine["result"]["messages"], "the owner must still see the transcript"


def test_a_thread_opened_through_mcp_records_its_owner(fake_sb):
    _send_as(A)
    rows = fake_sb.rows["conversations"]
    assert rows, "no conversation row was written"
    assert rows[0]["agent_id"] == A, (
        "the dispatcher did not bind the caller onto the stored thread, so the "
        "ownership guard downstream has nothing to compare")


def test_an_anonymous_send_does_not_create_a_shared_anonymous_owner(fake_sb):
    """'anonymous' is a sentinel, not an identity. Stored as an owner it would
    be one account that every unidentified caller on the internet is in."""
    _send_as(None)
    rows = fake_sb.rows["conversations"]
    assert rows and rows[0].get("agent_id") in (None, ""), (
        "an unidentified caller was recorded as the owner %r"
        % (rows[0].get("agent_id"),))


def test_an_unowned_thread_is_readable_by_nobody(fake_sb):
    """Rows that predate identity binding cannot be attributed to anyone, so
    they are released to nobody - including a caller holding the id."""
    fake_sb.rows["conversations"].append({
        "conversation_id": "conv_legacy01", "ref_token": "4821",
        "agent_id": None, "end_user_ref": "Sara",
        "business_number": "96890000001", "our_number": "15556677792",
        "state": "awaiting_reply",
    })
    denied = _read_as(A, conversation_id="conv_legacy01")
    assert denied["status"] == "failure"
    assert denied["reason_code"] == "conversation_owner_unknown", denied


# ==========================================================================
# THE SAME QUESTION FOR OPERATION RECEIPTS. A send_message receipt names the
# end-user and the business; a booking receipt names the customer and the time.
# ==========================================================================
def test_another_agent_cannot_read_your_operation_receipt():
    op_id = _operation_id_of(_send_as(A))
    stolen = asyncio.run(_dispatch_operation(
        "get_outcome", {"operation_id": op_id}, _headers(B)))
    assert stolen["status"] == "failure"
    assert stolen["reason_code"] == "not_your_operation", stolen


def test_an_anonymous_caller_cannot_read_an_owned_operation_receipt():
    op_id = _operation_id_of(_send_as(A))
    stolen = asyncio.run(_dispatch_operation(
        "get_outcome", {"operation_id": op_id}, _headers(None)))
    assert stolen["status"] == "failure"
    assert stolen["reason_code"] == "identity_required", stolen


def test_get_status_asks_the_same_question_as_get_outcome():
    """Two doors into one record. Guarding one of them is guarding neither."""
    op_id = _operation_id_of(_send_as(A))
    stolen = asyncio.run(_dispatch_operation(
        "get_status", {"operation_id": op_id}, _headers(B)))
    assert stolen.get("status") == "forbidden", stolen
    assert stolen.get("reason_code") == "not_your_operation"
    assert "partial_result" not in stolen


def test_the_owner_can_read_its_own_operation_receipt():
    op_id = _operation_id_of(_send_as(A))
    mine = asyncio.run(_dispatch_operation(
        "get_outcome", {"operation_id": op_id}, _headers(A)))
    assert mine["status"] == "success", mine
    status = asyncio.run(_dispatch_operation(
        "get_status", {"operation_id": op_id}, _headers(A)))
    assert status["status"] == "success", status


def test_the_rest_route_guards_the_same_record_as_the_mcp_tool():
    """The second door. /ops/get_outcome reaches the same handler and would
    have stayed open if only the MCP dispatcher had been taught to pass the
    caller - a shape of bug this repo has shipped before."""
    import main
    op_id = _operation_id_of(_send_as(A))
    stolen = asyncio.run(main.get_outcome(op_id, x_agent_identity=_token(B)))
    assert stolen.status.value == "failure"
    assert stolen.reason_code == "not_your_operation"


def test_an_in_process_call_is_not_a_request_and_is_not_denied():
    """core/schedule_appointment.py polls its own operation in-process with no
    caller at all. None means 'no external surface', never 'trusted external
    caller' - every surface passes a string, which the tests above exercise."""
    from core.status_outcome import handle_get_status
    op_id = _operation_id_of(_send_as(A))
    assert asyncio.run(handle_get_status(op_id))["status"] == "success"


# ==========================================================================
# The advertised half. web/facts.py counts "keyless" tools from
# IDENTITY_REQUIRED_READ_TOOLS, and a list that says a tool needs a key while
# the tool happily answers without one is a published number nobody can
# reproduce - so every name on that list is called here with no key.
# ==========================================================================
_ANONYMOUS_PROBE = {
    "get_conversation": {"conversation_id": "conv_probe"},
}


def test_every_tool_we_advertise_as_key_requiring_actually_refuses_without_one():
    from core.ownership import IDENTITY_REQUIRED_READ_TOOLS

    missing = IDENTITY_REQUIRED_READ_TOOLS - set(_ANONYMOUS_PROBE)
    assert not missing, (
        "IDENTITY_REQUIRED_READ_TOOLS names %s with no probe here, so nothing "
        "checks that it refuses a keyless call" % (sorted(missing),))

    for tool in sorted(IDENTITY_REQUIRED_READ_TOOLS):
        out = asyncio.run(_dispatch_operation(tool, _ANONYMOUS_PROBE[tool],
                                              _headers(None)))
        assert out.get("reason_code") == "identity_required", (
            "%s is advertised as needing a key and answered a keyless call "
            "with %r" % (tool, out.get("reason_code")))
