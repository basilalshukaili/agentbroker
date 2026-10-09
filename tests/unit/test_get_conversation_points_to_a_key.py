"""A keyless caller of get_conversation is told where a key comes from.

THE EVIDENCE (usage_events, 2026-10-01 .. 2026-10-09, docs/reviews/2026-10-09-agentbroker-request-analysis.md):
get_conversation refused 98 calls with `identity_required`: 59 from outside callers (38 distinct address+client
pairs) and 39 from our own tooling (3 further get_conversation calls got `conversation_not_found`, a different answer;
the first draft of this analysis counted them in, 101 and 42, and the gate's independent recount corrected it). It is
the second most common refusal an outside caller met. The write tools refuse a keyless caller
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


# ---------------------------------------------------------------------------------------------------------------------
# One remedy, in the refusal AND in the published contract (gate finding P2 on d4f34c1)
# ---------------------------------------------------------------------------------------------------------------------
#
# 757724f put the free-key address in the refusal's next_actions, but the manifest's failure_modes entry for the same
# code still told the caller to "Mint one with mint_key". Minting is closed to outside callers without the operator's
# machine-mint secret (agent_interface/well_known.py: the mcp.json descriptor says so), so the published remedy led to a
# route nobody outside can take, on every surface generated from the manifest: /manifest, llms-full.txt, the edge snapshots.

def _identity_required_entry(ops) -> dict:
    op = next(o for o in ops if o["name"] == "get_conversation")
    return next(fm for fm in op["failure_modes"] if fm["reason_code"] == "identity_required")


def _sentence_without_host(monkeypatch) -> str:
    """The refusal's sentence with the host removed, which is the form a static manifest can carry."""
    from agent_interface.key_state import free_key_url
    from core.ownership import free_key_next_actions
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    return free_key_next_actions()[0].replace(free_key_url(), "/keys/request")


def test_the_published_remedy_is_the_one_the_refusal_gives(monkeypatch):
    from agent_interface.manifest_server import get_full_manifest
    published = _identity_required_entry(get_full_manifest()["operations"])["agent_action"]
    assert published.startswith(_sentence_without_host(monkeypatch)), published
    assert "Mint one with mint_key" not in published


def test_the_published_remedy_does_not_send_an_outside_caller_to_mint_key(monkeypatch):
    from agent_interface.manifest_server import get_full_manifest
    published = _identity_required_entry(get_full_manifest()["operations"])["agent_action"]
    assert "mint_key" not in published.replace("mint_key is not a route for outside callers", "")


def test_every_surface_generated_from_the_manifest_carries_that_remedy(monkeypatch):
    import json
    from pathlib import Path
    from agent_interface import well_known
    root = Path(__file__).resolve().parents[2]
    want = _sentence_without_host(monkeypatch)
    full = well_known.get_llms_full_txt()
    section = full.split("## Operation: get_conversation\n", 1)[1].split("### Failure Modes\n", 1)[1].split("\n## ", 1)[0]
    assert want in section, "llms-full.txt must render get_conversation's structured failure modes, remedy included"
    for rel in ("edge/src/snapshots/llms-full.txt",):
        text = (root / rel).read_text(encoding="utf-8")
        assert want in text and "Mint one with mint_key" not in text, rel
    snap = json.loads((root / "edge/src/snapshots/manifest.json").read_text(encoding="utf-8"))
    assert _identity_required_entry(snap["operations"])["agent_action"].startswith(want)


def test_one_free_key_address_in_every_refusal_whatever_PUBLIC_BASE_URL_says(monkeypatch):
    """The read refusal, the write refusal (auth_required), the key-request page and the key diagnostics built the same
    address four separate times. They now call agent_interface.key_state.free_key_url, so they cannot differ."""
    import json
    from agent_interface import key_requests, key_state
    from agent_interface.mcp_server import handle_mcp_request
    from core.ownership import free_key_next_actions
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    for base, want in ((None, "https://api.hatchloop.dev/keys/request"),
                       ("https://example.test", "https://example.test/keys/request"),
                       ("https://example.test/", "https://example.test/keys/request"),
                       ("http://127.0.0.1:9/", "http://127.0.0.1:9/keys/request")):
        if base is None:
            monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("PUBLIC_BASE_URL", base)
        assert key_state.free_key_url() == want
        assert want in free_key_next_actions()[0]
        resp = _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "send_message", "arguments": {}}},
                                       headers={"user-agent": "pytest-one-free-key-address"}))
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["error_code"] == "auth_required"
        assert body["how_to_resolve"]["free_key"]["url"] == want
        assert _run(key_requests.describe_free_key_flow())["how"]["url"] == want
        assert want in key_state.classify_key("env:NOT_A_KEY").hint, "the key diagnostics point at the same address"
