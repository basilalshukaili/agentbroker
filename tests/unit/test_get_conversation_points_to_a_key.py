"""A keyless caller of get_conversation is told where a key comes from.

THE EVIDENCE (usage_events, 2026-10-01 .. 2026-10-09, docs/reviews/2026-10-09-agentbroker-request-analysis.md):
get_conversation answered `identity_required` to 59 outside calls (38 distinct address+client pairs) and 42 of our
own, 101 in all - the second most common refusal an outside caller met. The write tools refuse a keyless caller
with `auth_required` plus the free-key address; this read tool, which also needs a key (core/tool_auth.py lists it),
said "send your key as X-Agent-Identity" and never said where to get one.

These tests pin: the refusal carries the free-key address and the header name in `next_actions`; the address
follows PUBLIC_BASE_URL like the write tools' does; the refusal is still decided BEFORE any lookup (a guessable
four-digit reference must never be told apart by which error comes back); and a caller with an identity is not
shown a key pointer.
"""
from __future__ import annotations

import asyncio

import pytest

from core import conversations
from core.get_conversation import handle_get_conversation


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_lookup(monkeypatch):
    """A keyless caller must be refused before storage is touched: any lookup here is a failure."""
    async def boom(*a, **k):
        raise AssertionError("storage was read for a caller with no identity")
    monkeypatch.setattr(conversations, "get_conversation", boom)
    monkeypatch.setattr(conversations, "find_by_ref", boom)


@pytest.mark.parametrize("who", ["anonymous", "", "   "])
def test_a_keyless_caller_is_told_where_a_key_comes_from(who, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    r = _run(handle_get_conversation(conversation_id="c1", agent_id=who))
    assert r.reason_code == "identity_required"
    assert r.status.value == "failure" and r.retriable is False
    joined = " ".join(r.next_actions)
    assert "https://api.hatchloop.dev/keys/request" in joined
    assert "X-Agent-Identity" in joined
    assert "no payment" in joined


def test_the_key_address_follows_public_base_url_like_the_write_tools(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
    r = _run(handle_get_conversation(reference="1234", business_number="+15550100", agent_id="anonymous"))
    assert any("https://example.test/keys/request" in a for a in r.next_actions)


def test_the_refusal_is_the_same_whatever_was_asked_for():
    """No information about what is stored: the same reply for an id, a reference, or nothing at all."""
    a = _run(handle_get_conversation(conversation_id="c1", agent_id="anonymous"))
    b = _run(handle_get_conversation(reference="0001", business_number="+15550100", agent_id="anonymous"))
    c = _run(handle_get_conversation(agent_id="anonymous"))
    assert {x.reason_code for x in (a, b, c)} == {"identity_required"}
    assert a.next_actions == b.next_actions == c.next_actions
    assert a.human_message == b.human_message == c.human_message


def test_a_caller_with_an_identity_is_not_shown_a_key_pointer(monkeypatch):
    async def none(*a, **k):
        return None
    monkeypatch.setattr(conversations, "get_conversation", none)
    r = _run(handle_get_conversation(conversation_id="nope", agent_id="agent_abc123"))
    assert r.reason_code == "conversation_not_found"
    assert r.next_actions == []


def test_the_pointer_survives_the_real_dispatcher_and_reaches_the_wire(monkeypatch):
    """The receipt leaves the server through _dispatch_operation / tools/call, not through the handler."""
    from agent_interface import mcp_server
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    out = _run(mcp_server._dispatch_operation("get_conversation", {"conversation_id": "c1"}, {}))
    assert out["reason_code"] == "identity_required"
    assert any("/keys/request" in a for a in out["next_actions"])
