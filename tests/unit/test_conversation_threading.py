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


# ==========================================================================
# REGRESSION: the 15 defects found by adversarial review (2026-08-26).
# Each of these routed a reply to the WRONG person, hid a flood, or let a
# forged request in. They must never come back.
# ==========================================================================

def test_bare_numbers_are_never_treated_as_references():
    """CRITICAL#1: "come at 1430" must NOT match ref_token 1430."""
    assert C.parse_ref("yes, come at 1430") is None
    assert C.parse_ref("ok, 1000 OMR") is None
    assert C.parse_ref("booked for 2026-08-27") is None
    assert C.parse_ref("confirmed #4821") == "4821"       # sigiled still works
    assert C.parse_ref("sure #  4821 ok") == "4821"       # tolerant of spacing


def test_price_before_a_real_ref_does_not_shadow_it():
    """CRITICAL#1b: first-match-wins let a price hijack a correct reply."""
    assert C.parse_refs("we can do 2500 for #4821") == ["4821"]
    assert C.parse_refs("#1111 and #2222") == ["1111", "2222"]


def test_clock_time_reply_with_two_live_threads_asks_instead_of_misrouting():
    """CRITICAL#1 end-to-end: the exact misroute the review reproduced."""
    sara = _open(end_user_ref="Sara")
    _open(end_user_ref="Ali")
    # Sara's thread happens to hold a token that looks like a time.
    for r in fake_rows(sara["conversation_id"]):
        r["ref_token"] = "1430"
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body="yes, come at 1430"))
    assert m.ambiguous is True and m.conversation is None


def fake_rows(conv_id):
    """Helper: reach into the active FakeSB rows for a conversation."""
    import storage.supabase_client as real
    store = real.select_rows.__self__            # bound method -> FakeSB
    return [r for r in store.rows["conversations"] if r["conversation_id"] == conv_id]


def test_two_quoted_refs_are_ambiguous_not_first_wins():
    c1 = _open(end_user_ref="Sara")
    c2 = _open(end_user_ref="Ali")
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body=f"#{c1['ref_token']} yes and #{c2['ref_token']} no"))
    assert m.ambiguous is True and m.conversation is None


def test_ref_without_business_scope_never_matches():
    """CRITICAL#4: an unscoped 4-digit token could hit another business."""
    conv = _open()
    assert asyncio.run(C.find_by_ref(conv["ref_token"], None)) is None
    assert asyncio.run(C.find_by_ref(conv["ref_token"], "")) is None


def test_truncated_pair_window_forces_ambiguous_not_false_uniqueness(fake_sb):
    """CRITICAL#2: a full window means we cannot prove there is only one thread."""
    for i in range(3):
        fake_sb.rows["conversations"].append({
            "conversation_id": f"live{i}", "our_number": "15556677792",
            "business_number": "96890000001", "state": "open",
            "ref_token": f"90{i}0", "expires_at": None})
    live = asyncio.run(C.live_threads_for_pair(
        "15556677792", "96890000001", limit=2))       # force truncation
    assert any(r["conversation_id"] == "__truncated__" for r in live)
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792", body="yes"))
    assert m.ambiguous or not m.matched               # never a false "pair" match


def test_reply_to_an_older_message_still_resolves(fake_sb):
    """HIGH#3: only the LAST wamid was indexed; earlier ones fell through."""
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.FIRST", "first"))
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.SECOND", "second"))
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body="yes", context_wamid="wamid.FIRST"))
    assert m.matched and m.method == "wamid"
    assert m.conversation["conversation_id"] == conv["conversation_id"]


def test_closed_thread_does_not_claim_a_reply_via_wamid(fake_sb):
    """HIGH#5: layer 1 skipped the liveness check, so a dead thread won."""
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.OLD", "x"))
    for r in fake_sb.rows["conversations"]:
        r["state"] = "closed"
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body="yes", context_wamid="wamid.OLD"))
    assert not m.matched


def test_missing_business_number_yields_no_match():
    assert not asyncio.run(C.correlate_inbound(
        business_number="", our_number="15556677792", body="#1234 yes")).matched


def test_budget_counts_recent_traffic_even_with_long_history(fake_sb):
    """HIGH#7/#8: an unordered slice let a busy business escape the budget."""
    now = datetime.now(timezone.utc)
    for i in range(300):          # old noise that used to fill the window
        fake_sb.rows["conversations"].append({
            "conversation_id": f"old{i}", "business_id": "biz_busy",
            "created_at": (now - timedelta(days=5)).isoformat(), "state": "closed"})
    for i in range(6):            # recent, over the small-tier hourly limit
        fake_sb.rows["conversations"].append({
            "conversation_id": f"new{i}", "business_id": "biz_busy",
            "created_at": (now - timedelta(minutes=2)).isoformat(), "state": "open"})
    d = asyncio.run(D.check_budget("biz_busy", tier="small"))
    assert d.allowed is False, "recent flood must still be counted"
    assert d.reason_code == "business_rate_limited"


def test_retry_after_actually_clears_the_window(fake_sb):
    """MEDIUM#10: retrying exactly when told was refused again."""
    now = datetime.now(timezone.utc)
    for i in range(20):                       # far over the limit of 6
        fake_sb.rows["conversations"].append({
            "conversation_id": f"f{i}", "business_id": "biz_flood",
            "created_at": (now - timedelta(minutes=30 - i)).isoformat(), "state": "open"})
    d = asyncio.run(D.check_budget("biz_flood", tier="small"))
    assert d.allowed is False
    # At the advertised retry time, enough threads have aged out to fit one more.
    future = now + timedelta(milliseconds=d.retry_after_ms)
    still_in_window = [
        r for r in fake_sb.rows["conversations"]
        if r["business_id"] == "biz_flood"
        and datetime.fromisoformat(r["created_at"]) > future - timedelta(hours=1)]
    assert len(still_in_window) < 6, "advertised retry time must genuinely free a slot"


def test_digest_reply_cannot_accept_an_unshown_item():
    """LOW#9: header said 12, only 10 rendered, but "12 YES" was accepted."""
    reqs = [{"ref_token": f"{1000+i}", "end_user_ref": f"U{i}", "intent": "x"}
            for i in range(12)]
    msg = D.build_digest("Salon", reqs)
    assert "showing the first 10 of 12" in msg
    assert D.parse_digest_reply("12 YES", reqs) == []      # not addressable
    assert D.parse_digest_reply("10 YES", reqs) == [(reqs[9], True)]


# ==========================================================================
# REGRESSION: webhook hardening (CRITICAL#12, HIGH#11/#14, MEDIUM#13/#15)
# ==========================================================================
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent_interface.whatsapp_webhook import router
    app = FastAPI(); app.include_router(router)
    return TestClient(app)


def test_forged_webhook_is_rejected(monkeypatch):
    """CRITICAL#12: unsigned POSTs could inject fake business replies."""
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "topsecret")
    r = _client().post("/webhooks/whatsapp", json=_wh_payload("yes"))
    assert r.status_code == 403


def test_correctly_signed_webhook_is_accepted(monkeypatch, fake_sb):
    import hashlib, hmac, json as _json
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "topsecret")
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.SIG", "req"))
    body = _json.dumps(_wh_payload("yes", context_wamid="wamid.SIG")).encode()
    sig = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    r = _client().post("/webhooks/whatsapp", content=body,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["received"] == 1


def test_stop_does_not_fall_through_to_correlation(fake_sb, monkeypatch):
    """HIGH#11: a STOP was recorded as a booking reply AND could trigger a send."""
    asked = {}
    import agent_interface.whatsapp_webhook as wh

    async def fake_ask(to, q):
        asked["hit"] = True
    monkeypatch.setattr(wh, "_ask", fake_ask)
    _open(end_user_ref="Sara")
    _open(end_user_ref="Ali")                 # ambiguous pair on purpose
    r = _client().post("/webhooks/whatsapp", json=_wh_payload("STOP"))
    assert r.status_code == 200
    assert not asked, "must not message a number that just opted out"
    assert not [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "in"]
    from compliance.consent_store import get_consent_store
    assert get_consent_store().is_opted_out("96890000001", "whatsapp") is True


def test_duplicate_delivery_is_ignored(fake_sb):
    """MEDIUM#15: Meta retries; a redelivery double-recorded the reply."""
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.D1", "req"))
    c = _client()
    p = _wh_payload("yes", context_wamid="wamid.D1")
    assert c.post("/webhooks/whatsapp", json=p).json()["received"] == 1
    assert c.post("/webhooks/whatsapp", json=p).json()["received"] == 0
    ins = [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "in"]
    assert len(ins) == 1


def test_malformed_message_does_not_abort_the_batch(fake_sb):
    """MEDIUM#13: one bad message used to discard every sibling in the payload."""
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.OK1", "req"))
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": "15556677792"},
        "contacts": [{"wa_id": "96890000001"}],
        "messages": [
            "this is not a dict at all",          # genuinely malformed -> raises
            {"id": "wamid.GOOD", "from": "96890000001", "type": "text",
             "text": {"body": "yes"}, "context": {"id": "wamid.OK1"}},
        ]}}]}]}
    r = _client().post("/webhooks/whatsapp", json=payload)
    assert r.status_code == 200
    ins = [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "in"]
    assert len(ins) == 1, "the good sibling must still be processed"


# ==========================================================================
# OUTBOUND integration: sends open a thread and carry their reference.
# ==========================================================================
def test_whatsapp_send_opens_thread_and_carries_reference(fake_sb, monkeypatch):
    import core.send_message as sm
    from core.models import (SendMessageRequest, Recipient, MessageType,
                             MessageContent, ChannelPreference)
    from channels.adapter_interface import ChannelResponse

    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER", "15556677792")
    sent = {}

    async def fake_send(req):
        sent["body"] = req.content
        return ChannelResponse(success=True, provider_message_id="wamid.OUT99")

    monkeypatch.setattr(sm._WHATSAPP_ADAPTER, "send", fake_send)

    receipt = asyncio.run(sm.handle_send_message(SendMessageRequest(
        recipient=Recipient(id_type="phone", id_value="+96890000001", country_code="OM"),
        message_type=MessageType.TRANSACTIONAL,
        content=MessageContent(body="Can Sara book Tuesday 3pm?"),
        preferred_channel=ChannelPreference.WHATSAPP,
        on_behalf_of="Sara", business_id="biz_salon"), agent_id="agent_a"))

    assert receipt.status.value == "success"
    conv = receipt.result["conversation"]
    ref = conv["reference"]
    # identity + reference travelled in the message the business receives
    assert f"#{ref}" in sent["body"] and "Sara" in sent["body"] and "via HatchLoop" in sent["body"]
    # the outbound wamid was bound to the thread (layer 1 substrate)
    outs = [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "out"]
    assert outs and outs[0]["wamid"] == "wamid.OUT99"

    # ...and the business's later free-typed reply resolves back to it exactly
    m = asyncio.run(C.correlate_inbound(
        business_number="96890000001", our_number="15556677792",
        body=f"yes ok #{ref}"))
    assert m.confidence == "exact"
    assert m.conversation["conversation_id"] == conv["conversation_id"]


def test_send_without_on_behalf_of_is_unchanged(fake_sb, monkeypatch):
    """No end-user named -> no thread, body untouched (back-compat)."""
    import core.send_message as sm
    from core.models import (SendMessageRequest, Recipient, MessageType,
                             MessageContent, ChannelPreference)
    from channels.adapter_interface import ChannelResponse
    sent = {}

    async def fake_send(req):
        sent["body"] = req.content
        return ChannelResponse(success=True, provider_message_id="wamid.X")

    monkeypatch.setattr(sm._WHATSAPP_ADAPTER, "send", fake_send)
    receipt = asyncio.run(sm.handle_send_message(SendMessageRequest(
        recipient=Recipient(id_type="phone", id_value="+96890000002", country_code="OM"),
        message_type=MessageType.TRANSACTIONAL,
        content=MessageContent(body="plain message"),
        preferred_channel=ChannelPreference.WHATSAPP), agent_id="agent_a"))
    assert receipt.status.value == "success"
    assert sent["body"] == "plain message"
    assert "conversation" not in receipt.result


def test_fallback_to_sms_does_not_claim_the_whatsapp_thread(fake_sb, monkeypatch):
    """WhatsApp opened a thread then failed; SMS delivered. The receipt must NOT
    advertise a reference the business never received, and the SMS provider id
    must not be bound to the WhatsApp thread."""
    import core.send_message as sm
    from core.models import (SendMessageRequest, Recipient, MessageType,
                             MessageContent, ChannelPreference)
    from channels.adapter_interface import ChannelResponse
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER", "15556677792")

    async def wa_fails(req):
        return ChannelResponse(success=False, error_code="whatsapp_token_expired",
                               error_message="token expired")

    async def sms_ok(req):
        return ChannelResponse(success=True, provider_message_id="SM_sms_123")

    monkeypatch.setattr(sm._WHATSAPP_ADAPTER, "send", wa_fails)
    monkeypatch.setattr(sm._SMS_ADAPTER, "send", sms_ok)

    receipt = asyncio.run(sm.handle_send_message(SendMessageRequest(
        recipient=Recipient(id_type="phone", id_value="+96890000009", country_code="OM"),
        message_type=MessageType.TRANSACTIONAL,
        content=MessageContent(body="Booking?"),
        preferred_channel=ChannelPreference.WHATSAPP,
        on_behalf_of="Sara"), agent_id="agent_a"))

    assert receipt.status.value == "success"
    assert receipt.channel_used == "sms:twilio"
    assert "conversation" not in receipt.result, "must not advertise an unsent reference"
    outs = [m for m in fake_sb.rows["conversation_messages"] if m["direction"] == "out"]
    assert not any(m["wamid"] == "SM_sms_123" for m in outs), "SMS id bound to WA thread"


# ==========================================================================
# Demand shaping must be ENFORCED on the send path (it was dead code until
# 2026-08-26 - built, described, but never called).
# ==========================================================================
def _send(monkeypatch, **kw):
    import core.send_message as sm
    from core.models import (SendMessageRequest, Recipient, MessageType,
                             MessageContent, ChannelPreference)
    from channels.adapter_interface import ChannelResponse
    sent = {}

    async def fake_send(req):
        sent["hit"] = True
        return ChannelResponse(success=True, provider_message_id="wamid.Z")

    monkeypatch.setattr(sm._WHATSAPP_ADAPTER, "send", fake_send)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER", "15556677792")
    req = SendMessageRequest(
        recipient=Recipient(id_type="phone", id_value="+96890000077", country_code="OM"),
        message_type=MessageType.TRANSACTIONAL,
        content=MessageContent(body="Booking?"),
        preferred_channel=ChannelPreference.WHATSAPP, **kw)
    return asyncio.run(sm.handle_send_message(req, agent_id="agent_a")), sent


def test_flood_is_queued_not_sent(fake_sb, monkeypatch):
    now = datetime.now(timezone.utc)
    for i in range(6):                      # small-tier hourly limit
        fake_sb.rows["conversations"].append({
            "conversation_id": f"q{i}", "business_id": "biz_busy2",
            "created_at": (now - timedelta(minutes=3)).isoformat(), "state": "open"})
    receipt, sent = _send(monkeypatch, on_behalf_of="Sara", business_id="biz_busy2")
    assert receipt.status.value == "failure"
    assert receipt.reason_code == "business_rate_limited"
    assert receipt.retriable is True                       # queued, not rejected
    assert receipt.result["demand_shaping"]["retry_after_ms"] > 0
    assert receipt.cost.amount == 0.0                      # never charge for a block
    assert not sent, "the message must NOT have been dispatched"


def test_under_budget_still_sends(fake_sb, monkeypatch):
    receipt, sent = _send(monkeypatch, on_behalf_of="Sara", business_id="biz_quiet")
    assert receipt.status.value == "success"
    assert sent.get("hit") is True


def test_no_business_id_bypasses_shaping(fake_sb, monkeypatch):
    now = datetime.now(timezone.utc)
    for i in range(20):
        fake_sb.rows["conversations"].append({
            "conversation_id": f"z{i}", "business_id": "biz_busy3",
            "created_at": now.isoformat(), "state": "open"})
    receipt, sent = _send(monkeypatch, on_behalf_of="Sara")   # no business_id
    assert receipt.status.value == "success"
    assert sent.get("hit") is True


def test_shaping_failure_never_blocks_a_send(fake_sb, monkeypatch):
    import core.demand_shaping as D2
    async def boom(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(D2, "check_budget", boom)
    receipt, sent = _send(monkeypatch, on_behalf_of="Sara", business_id="biz_x")
    assert receipt.status.value == "success", "shaping must fail OPEN"
    assert sent.get("hit") is True


# ==========================================================================
# Thread resolution: a clear yes/no must CLOSE the thread, so it stops
# competing for future replies. (set_state was dead code until 2026-08-26.)
# ==========================================================================
def test_clear_yes_confirms_and_stops_competing(fake_sb):
    conv = _open(end_user_ref="Sara")
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.R1", "req"))
    r = _post(_wh_payload("yes", context_wamid="wamid.R1"))
    assert r.status_code == 200
    row = [x for x in fake_sb.rows["conversations"]
           if x["conversation_id"] == conv["conversation_id"]][0]
    assert row["state"] == C.CONFIRMED
    # a resolved thread no longer claims new replies
    live = asyncio.run(C.live_threads_for_pair("15556677792", "96890000001"))
    assert not any(x["conversation_id"] == conv["conversation_id"] for x in live)


def test_clear_no_closes_the_thread(fake_sb):
    conv = _open(end_user_ref="Ali")
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.R2", "req"))
    _post(_wh_payload("sorry no", context_wamid="wamid.R2"))
    row = [x for x in fake_sb.rows["conversations"]
           if x["conversation_id"] == conv["conversation_id"]][0]
    assert row["state"] == C.CLOSED


def test_vague_reply_leaves_thread_open(fake_sb):
    """Guessing 'resolved' from a vague reply would close a live request."""
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.R3", "req"))
    _post(_wh_payload("let me check with the owner and get back to you",
                      context_wamid="wamid.R3"))
    row = [x for x in fake_sb.rows["conversations"]
           if x["conversation_id"] == conv["conversation_id"]][0]
    assert row["state"] in (C.OPEN, C.AWAITING_REPLY)


def test_reference_prefixed_yes_still_resolves(fake_sb):
    conv = _open()
    asyncio.run(C.record_outbound(conv["conversation_id"], "wamid.R4", "req"))
    _post(_wh_payload(f"#{conv['ref_token']} yes", context_wamid="wamid.R4"))
    row = [x for x in fake_sb.rows["conversations"]
           if x["conversation_id"] == conv["conversation_id"]][0]
    assert row["state"] == C.CONFIRMED
