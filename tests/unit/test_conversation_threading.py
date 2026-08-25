"""Conversation threading + demand shaping (founder scenarios, 2026-08-26).

Scenario A: many users -> one business on our shared number. Which reply
belongs to whom? Never guess.
Scenario B: many users spam one business at once. Shape, don't flood.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core import conversations as C
from core import demand_shaping as D


# --------------------------------------------------------------------------
# Fake Supabase: an in-memory table the module's late imports resolve to.
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

    async def select_rows(self, table, filters=None, limit=1000):
        out = []
        for r in self.rows.get(table, []):
            if all(r.get(k) == v for k, v in (filters or {}).items()):
                out.append(r)
        return out[:limit]


@pytest.fixture(autouse=True)
def fake_sb(monkeypatch):
    sb = FakeSB()
    import storage.supabase_client as real
    monkeypatch.setattr(real, "insert_row", sb.insert_row)
    monkeypatch.setattr(real, "upsert_row", sb.upsert_row)
    monkeypatch.setattr(real, "select_rows", sb.select_rows)
    return sb


def _open(**kw):
    base = dict(agent_id="agent_a", end_user_ref="Sara", business_id="biz_1",
                business_number="96890000001", our_number="15556677792",
                intent="haircut Tue 3pm")
    base.update(kw)
    return asyncio.run(C.open_conversation(**base))


# --------------------------------------------------------------------------
# Layer 1-4 correlation
# --------------------------------------------------------------------------
def test_layer1_wamid_exact_match():
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.AAA", "hi"))
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body="yes ok", context_wamid="wamid.AAA"))
    assert m.matched and m.method == "wamid" and m.confidence == "exact"
    assert m.conversation["conversation_id"] == conv["conversation_id"]


def test_layer2_ref_token_matches_free_typed_reply():
    c1 = _open(end_user_ref="Sara")
    c2 = _open(end_user_ref="Ali", our_number="15556677793")   # different pair
    asyncio.run(C.record_outbound(c2["conversation_id"], "wamid.B", "x"))
    # Business types freely but echoes the reference of c2.
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677793",
        body=f"sure, #{c2['ref_token']} works for us"))
    assert m.method == "ref" and m.confidence == "exact"
    assert m.conversation["conversation_id"] == c2["conversation_id"]
    assert c1["ref_token"] != c2["ref_token"]


def test_layer3_single_open_thread_on_pair_is_inferred():
    conv = _open()
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792", body="yes"))
    assert m.matched and m.method == "pair" and m.confidence == "inferred"
    assert m.conversation["conversation_id"] == conv["conversation_id"]


def test_layer4_two_live_threads_never_guess():
    """THE trust-critical case: ambiguous -> ask, never route blindly."""
    _open(end_user_ref="Sara")
    _open(end_user_ref="Ali")          # same pair on purpose
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792", body="yes ok"))
    assert m.ambiguous is True
    assert m.conversation is None          # <- no guess
    assert len(m.candidates) == 2
    q = C.clarifying_question(m.candidates)
    assert "#" in q and "Which one" in q


def test_pair_conflict_is_surfaced_for_number_pool_routing():
    _open()
    second = _open(end_user_ref="Ali")
    assert second.get("pair_conflict"), "a 2nd live thread on the pair must be flagged"


def test_stale_thread_does_not_claim_new_replies(fake_sb):
    conv = _open()
    for r in fake_sb.rows["conversations"]:
        r["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792", body="yes"))
    assert not m.matched and not m.ambiguous


def test_no_match_when_business_unknown():
    m = asyncio.run(C.correlate_inbound(
        business_number="99999999999", our_number="15556677792", body="hello"))
    assert not m.matched and m.method == "none"


def test_reference_line_carries_identity_and_ref():
    line = C.reference_line("4821", "Sara")
    assert "#4821" in line and "Sara" in line and "via HatchLoop" in line


def test_parse_ref_ignores_non_reference_numbers():
    assert C.parse_ref("see you at 3pm on #4821") == "4821"
    assert C.parse_ref("no numbers here") is None


# --------------------------------------------------------------------------
# Demand shaping
# --------------------------------------------------------------------------
def test_budget_allows_under_limit():
    d = asyncio.run(D.check_budget("biz_new", tier="small"))
    assert d.allowed and d.reason_code == "within_budget"


def test_budget_blocks_flood_with_honest_retry_after(fake_sb):
    now = datetime.now(timezone.utc)
    for i in range(6):                       # small tier hourly limit = 6
        fake_sb.rows["conversations"].append({
            "conversation_id": f"c{i}", "business_id": "biz_hot",
            "created_at": (now - timedelta(minutes=5)).isoformat(), "state": "open"})
    d = asyncio.run(D.check_budget("biz_hot", tier="small"))
    assert d.allowed is False
    assert d.reason_code == "business_rate_limited"
    assert d.retry_after_ms and d.retry_after_ms > 0     # queued, not dropped
    assert "queued rather than dropped" in d.human_message


def test_budget_fails_open_when_store_unreachable(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("supabase down")
    import storage.supabase_client as real
    monkeypatch.setattr(real, "select_rows", boom)
    d = asyncio.run(D.check_budget("biz_1", tier="micro"))
    assert d.allowed is True, "throttle must never break delivery"


def test_digest_aggregates_many_requests_into_one_message():
    reqs = [{"end_user_ref": "Sara", "intent": "haircut Tue 3pm", "ref_token": "1111"},
            {"end_user_ref": "Ali", "intent": "beard trim Wed 10am", "ref_token": "2222"},
            {"end_user_ref": "Maha", "intent": "colour Thu 5pm", "ref_token": "3333"}]
    msg = D.build_digest("Salon X", reqs)
    assert "3 pending requests" in msg
    assert "1) haircut Tue 3pm for Sara" in msg
    assert "#2222" in msg and "1 YES" in msg and "STOP" in msg


def test_digest_reply_parsing():
    reqs = [{"ref_token": "1111"}, {"ref_token": "2222"}, {"ref_token": "3333"}]
    out = D.parse_digest_reply("1 YES 3 no", reqs)
    assert out == [(reqs[0], True), (reqs[2], False)]
    assert D.parse_digest_reply("2 y", reqs) == [(reqs[1], True)]
    assert D.parse_digest_reply("no idea", reqs) == []


# --------------------------------------------------------------------------
# End-to-end through the real webhook HTTP layer
# --------------------------------------------------------------------------
def _post(payload):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_interface.whatsapp_webhook import router
    app = FastAPI(); app.include_router(router)
    return TestClient(app).post("/webhooks/whatsapp", json=payload)


def _wh_payload(body, context_wamid=None, sender="96890000001", our="15556677792"):
    msg = {"id": "wamid.IN1", "from": sender, "type": "text", "text": {"body": body}}
    if context_wamid:
        msg["context"] = {"id": context_wamid}
    return {"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": our},
        "contacts": [{"wa_id": sender, "profile": {"name": "Salon X"}}],
        "messages": [msg]}}]}]}


def test_webhook_correlates_reply_to_right_thread(fake_sb):
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.OUT1", "request"))
    r = _post(_wh_payload("yes that works", context_wamid="wamid.OUT1"))
    assert r.status_code == 200
    ins = [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "in"]
    assert len(ins) == 1
    assert ins[0]["conversation_id"] == conv["conversation_id"]


def test_webhook_asks_instead_of_guessing_when_ambiguous(fake_sb, monkeypatch):
    asked = {}
    import agent_interface.whatsapp_webhook as wh

    async def fake_ask(to, question):
        asked["to"], asked["q"] = to, question

    monkeypatch.setattr(wh, "_ask", fake_ask)
    _open(end_user_ref="Sara")
    _open(end_user_ref="Ali")            # two live threads, same pair
    r = _post(_wh_payload("ok yes"))
    assert r.status_code == 200
    assert asked.get("to") == "96890000001"
    assert "Which one" in asked.get("q", "")
    # and NOTHING was routed to a thread
    assert not [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "in"]
