"""
Demand queue — the wiring tests.

The digest renderer was fully built and fully tested and STILL dead: nothing
called it. So these tests deliberately assert the WIRING, not just the pure
functions — an over-budget send must land in the queue, an inbound must trigger
dispatch, and a numbered reply must resolve the exact requests the business saw.

A unit test of build_digest() would have passed the whole time it was dead.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core import demand_queue as dq


# ---------------------------------------------------------------------------
# In-memory Supabase double
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []

    # -- storage.supabase_client surface --
    async def insert_row(self, table, row):
        self.tables.setdefault(table, []).append(dict(row))
        return dict(row)

    async def upsert_row(self, table, row, on_conflict="id"):
        rows = self.tables.setdefault(table, [])
        key = row.get(on_conflict)
        for r in rows:
            if r.get(on_conflict) == key:
                r.update(row)
                return r
        rows.append(dict(row))
        return dict(row)

    async def select_rows(self, table, filters=None, limit=1000, order=None, gte=None):
        rows = [dict(r) for r in self.tables.get(table, [])]
        for k, v in (filters or {}).items():
            rows = [r for r in rows if r.get(k) == v]
        for k, v in (gte or {}).items():
            rows = [r for r in rows if str(r.get(k) or "") >= str(v)]
        if order:
            field, _, direction = order.partition(".")
            rows.sort(key=lambda r: str(r.get(field) or ""),
                      reverse=(direction == "desc"))
        return rows[:limit]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "insert_row", fake.insert_row)
    monkeypatch.setattr(sb, "upsert_row", fake.upsert_row)
    monkeypatch.setattr(sb, "select_rows", fake.select_rows)

    async def _send(to, body, our_number=""):
        fake.sent.append((to, body, our_number))
        return {"ok": True, "wamid": f"wamid_{len(fake.sent)}"}

    monkeypatch.setattr(dq, "_send_whatsapp", _send)

    class _Consent:
        def is_opted_out(self, num, ch):
            return num in getattr(fake, "opted_out", set())

    import compliance.consent_store as cs
    monkeypatch.setattr(cs, "get_consent_store", lambda: _Consent())
    fake.opted_out = set()
    return fake


async def _queue(db, n, business_id="biz_1", number="96894639405"):
    out = []
    for i in range(n):
        out.append(await dq.enqueue(
            business_id=business_id, business_number=number,
            agent_id=f"agent_{i}", end_user_ref=f"user_{i}",
            intent=f"table for {i + 2}"))
    return out


# ---------------------------------------------------------------------------
# Queue semantics
# ---------------------------------------------------------------------------

def test_enqueue_persists_so_queued_is_true(db):
    """'Queued' in a receipt must correspond to a stored row."""
    rows = asyncio.run(_queue(db, 3))
    assert all(r and r["state"] == dq.QUEUED for r in rows)
    assert len(db.tables["pending_requests"]) == 3


def test_retry_does_not_double_queue(db):
    """Our own retry_after_ms invites a retry; it must not duplicate the entry.

    Otherwise the business sees the same customer twice in one digest.
    """
    async def go():
        a = await dq.enqueue(business_id="b", business_number="1", agent_id="ag",
                             end_user_ref="u", intent="cut", idempotency_key="k1")
        b = await dq.enqueue(business_id="b", business_number="1", agent_id="ag",
                             end_user_ref="u", intent="cut", idempotency_key="k1")
        return a, b
    a, b = asyncio.run(go())
    assert a["request_id"] == b["request_id"]
    assert len(db.tables["pending_requests"]) == 1


def test_pending_is_oldest_first(db):
    """Fairness: the person who waited longest reaches the business first."""
    async def go():
        await _queue(db, 3)
        rows = db.tables["pending_requests"]
        base = datetime.now(timezone.utc)
        for i, r in enumerate(rows):
            r["created_at"] = (base - timedelta(minutes=10 - i)).isoformat()
        return await dq.pending_for("biz_1")
    pending = asyncio.run(go())
    stamps = [r["created_at"] for r in pending]
    assert stamps == sorted(stamps)


def test_expired_requests_never_reach_the_business(db):
    async def go():
        await _queue(db, 2)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.tables["pending_requests"][0]["expires_at"] = past
        return await dq.pending_for("biz_1")
    assert len(asyncio.run(go())) == 1


# ---------------------------------------------------------------------------
# Dispatch gates
# ---------------------------------------------------------------------------

def test_single_request_does_not_interrupt(db):
    """One request is not a digest — interrupting for it is the flood behaviour."""
    async def go():
        await _queue(db, 1)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=True)
    res = asyncio.run(go())
    assert res["dispatched"] is False
    assert res["reason"] == "below_digest_threshold"
    assert db.sent == []


def test_outside_service_window_stays_queued_and_says_why(db):
    """No window = no send. We must not fabricate a dispatch."""
    async def go():
        await _queue(db, 3)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=False)
    res = asyncio.run(go())
    assert res["dispatched"] is False
    assert res["reason"] == "outside_24h_service_window"
    assert res["pending"] == 3
    assert db.sent == []
    # And crucially they are STILL queued, not silently consumed.
    assert all(r["state"] == dq.QUEUED for r in db.tables["pending_requests"])


def test_opted_out_business_is_never_messaged(db):
    async def go():
        await _queue(db, 3)
        db.opted_out = {"96894639405"}
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=True)
    res = asyncio.run(go())
    assert res["dispatched"] is False
    assert res["reason"] == "business_opted_out"
    assert db.sent == []


def test_optout_store_failure_fails_CLOSED(db, monkeypatch):
    """Everywhere else shaping fails open. Here it must fail closed.

    An unreadable opt-out store is not permission to message someone who said
    stop.
    """
    import compliance.consent_store as cs

    def _boom():
        raise RuntimeError("store down")
    monkeypatch.setattr(cs, "get_consent_store", _boom)

    async def go():
        await _queue(db, 3)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=True)
    res = asyncio.run(go())
    assert res["dispatched"] is False
    assert res["reason"] == "optout_check_unavailable"
    assert db.sent == []


def test_failed_send_leaves_requests_queued(db, monkeypatch):
    """A failed send must NOT mark them dispatched, or they vanish unseen."""
    async def _fail(to, body, our_number=""):
        return {"ok": False, "error": "needs_template: outside window"}
    monkeypatch.setattr(dq, "_send_whatsapp", _fail)

    async def go():
        await _queue(db, 3)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=True)
    res = asyncio.run(go())
    assert res["dispatched"] is False
    assert res["reason"] == "send_failed"
    assert all(r["state"] == dq.QUEUED for r in db.tables["pending_requests"])


# ---------------------------------------------------------------------------
# The inversion: N requests -> 1 message
# ---------------------------------------------------------------------------

def test_dispatch_sends_ONE_message_for_many_requests(db):
    async def go():
        await _queue(db, 5)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792",
                                        business_name="Ali's Barbers",
                                        force_window=True)
    res = asyncio.run(go())
    assert res["dispatched"] is True
    assert res["count"] == 5
    assert len(db.sent) == 1                      # THE INVERSION
    body = db.sent[0][1]
    assert "Ali's Barbers" in body
    for n in range(1, 6):
        assert f"{n})" in body
    assert "STOP" in body
    assert all(r["state"] == dq.DISPATCHED for r in db.tables["pending_requests"])


def test_digest_caps_and_leaves_the_rest_queued(db):
    """Over DIGEST_MAX, the overflow must stay queued — not be silently lost."""
    async def go():
        await _queue(db, 14)
        return await dq.dispatch_digest(business_id="biz_1",
                                        business_number="96894639405",
                                        our_number="15556677792", force_window=True)
    res = asyncio.run(go())
    assert res["count"] == dq.__dict__.get("DIGEST_MAX", 10) or res["count"] == 10
    assert res["remaining_queued"] == 4
    still = [r for r in db.tables["pending_requests"] if r["state"] == dq.QUEUED]
    assert len(still) == 4


# ---------------------------------------------------------------------------
# Reply resolution — scored against what the business SAW
# ---------------------------------------------------------------------------

def test_reply_resolves_the_exact_requests_shown(db):
    async def go():
        await _queue(db, 3)
        await dq.dispatch_digest(business_id="biz_1", business_number="96894639405",
                                 our_number="15556677792", force_window=True)
        return await dq.resolve_reply("15556677792", "96894639405", "1 YES 3 no")
    res = asyncio.run(go())
    assert res["matched"] == 2
    by_id = {r["request_id"]: r for r in db.tables["pending_requests"]}
    ordered = db.tables["demand_digests"][0]["request_ids"]
    assert by_id[ordered[0]]["state"] == dq.ACCEPTED
    assert by_id[ordered[1]]["state"] == dq.DISPATCHED     # unanswered, untouched
    assert by_id[ordered[2]]["state"] == dq.DECLINED


def test_reply_scored_against_snapshot_not_the_live_queue(db):
    """The queue grows between dispatch and reply. "2 YES" must still mean the
    second item the business SAW — not the second item in the queue now."""
    async def go():
        await _queue(db, 2)
        await dq.dispatch_digest(business_id="biz_1", business_number="96894639405",
                                 our_number="15556677792", force_window=True)
        shown = list(db.tables["demand_digests"][0]["request_ids"])
        # three more customers arrive after the digest went out
        await _queue(db, 3)
        res = await dq.resolve_reply("15556677792", "96894639405", "2 YES")
        return shown, res
    shown, res = asyncio.run(go())
    assert res["matched"] == 1
    assert res["results"][0]["request_id"] == shown[1]


def test_unrelated_reply_matches_nothing(db):
    """A conversational reply must never be read as a silent accept."""
    async def go():
        await _queue(db, 3)
        await dq.dispatch_digest(business_id="biz_1", business_number="96894639405",
                                 our_number="15556677792", force_window=True)
        return await dq.resolve_reply("15556677792", "96894639405",
                                      "sorry we are closed this week")
    res = asyncio.run(go())
    assert res["matched"] == 0


def test_reply_with_no_open_digest_is_not_swallowed(db):
    """Without this, an ordinary 'yes' would be eaten by the digest path."""
    res = asyncio.run(dq.resolve_reply("15556677792", "96894639405", "1 yes"))
    assert res["matched"] == 0
    assert res["reason"] == "no_open_digest"


def test_missing_row_becomes_a_hole_not_a_shift(db):
    """If a shown request disappears, later numbers must NOT renumber onto it —
    that would reassign someone else's answer."""
    async def go():
        await _queue(db, 3)
        await dq.dispatch_digest(business_id="biz_1", business_number="96894639405",
                                 our_number="15556677792", force_window=True)
        ordered = list(db.tables["demand_digests"][0]["request_ids"])
        db.tables["pending_requests"] = [
            r for r in db.tables["pending_requests"] if r["request_id"] != ordered[0]]
        res = await dq.resolve_reply("15556677792", "96894639405", "1 YES 2 YES")
        return ordered, res
    ordered, res = asyncio.run(go())
    # item 1 is gone -> skipped, not backfilled by item 2
    assert res["matched"] == 1
    assert res["results"][0]["request_id"] == ordered[1]


# ---------------------------------------------------------------------------
# Service window
# ---------------------------------------------------------------------------

def test_service_window_reads_last_inbound(db):
    async def go(delta_hours):
        db.tables["conversations"] = [{
            "our_number": "15556677792", "business_number": "96894639405",
            "last_inbound_at": (datetime.now(timezone.utc)
                                - timedelta(hours=delta_hours)).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
        return await dq.service_window_open("15556677792", "96894639405")
    assert asyncio.run(go(2)) is True
    assert asyncio.run(go(30)) is False


def test_window_check_fails_CLOSED_when_unreadable(db, monkeypatch):
    """Our own outreach fails closed — guessing wrong means a policy strike."""
    import storage.supabase_client as sb

    async def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(sb, "select_rows", _boom)
    assert asyncio.run(dq.service_window_open("15556677792", "96894639405")) is False


# ---------------------------------------------------------------------------
# WIRING — the tests that would have caught the dead code
# ---------------------------------------------------------------------------

def test_over_budget_send_actually_enqueues(db, monkeypatch):
    """send_message's 'queued' receipt must correspond to a real queue row."""
    import core.send_message as sm
    from core.demand_shaping import BudgetDecision

    async def _over(*a, **k):
        return BudgetDecision(allowed=False, reason_code="business_rate_limited",
                              retry_after_ms=60_000, used_hour=9, limit_hour=6,
                              human_message="queued")
    import core.demand_shaping as ds
    monkeypatch.setattr(ds, "check_budget", _over)

    receipt = asyncio.run(sm.handle_send_message(_send_request()))
    block = receipt.result["demand_shaping"]
    assert block["queued"] is True, "receipt claims queued - it must BE queued"
    assert block["queued_request_id"]
    assert receipt.cost.amount == 0.0          # never charge for a deferral
    assert receipt.retriable is True
    assert len(db.tables["pending_requests"]) == 1
    # Stored digits-only. Meta sends the webhook `from` as bare digits, so an
    # E.164 row here would never be found by dispatch_for_number and the digest
    # would silently never fire — the exact class of bug that cost us a full
    # correlation rewrite.
    assert db.tables["pending_requests"][0]["business_number"] == "96894639405"


def test_webhook_inbound_triggers_dispatch(db, monkeypatch):
    """An inbound OPENS the window — that is the moment the digest can go."""
    import agent_interface.whatsapp_webhook as wh

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(wh, "_already_handled", lambda _id: _noop())

    async def go():
        await _queue(db, 3, number="96894639405")
        await wh._handle_message(
            {"from": "96894639405", "type": "text", "id": "wamid_in_1",
             "text": {"body": "hello, sorry for the delay"}},
            {"96894639405": "Ali's Barbers"}, "15556677792")
    asyncio.run(go())
    assert len(db.sent) == 1, "inbound should have flushed the queued digest"
    assert "Ali's Barbers" in db.sent[0][1]


def test_webhook_digest_reply_does_not_confirm_a_booking_thread(db, monkeypatch):
    """'1 YES' is a digest answer. Correlation must not see it first and read
    the bare 'yes' as confirmation of an unrelated thread."""
    import agent_interface.whatsapp_webhook as wh
    from core import conversations as conv

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(wh, "_already_handled", lambda _id: _noop())

    called = {"correlate": False}

    async def _correlate(**k):
        called["correlate"] = True
        raise AssertionError("digest reply must short-circuit before correlation")
    monkeypatch.setattr(conv, "correlate_inbound", _correlate)

    async def go():
        await _queue(db, 2)
        await dq.dispatch_digest(business_id="biz_1", business_number="96894639405",
                                 our_number="15556677792", force_window=True)
        await wh._handle_message(
            {"from": "96894639405", "type": "text", "id": "wamid_in_2",
             "text": {"body": "1 YES"}},
            {}, "15556677792")
    asyncio.run(go())
    assert called["correlate"] is False


# ---------------------------------------------------------------------------
# The sweeper — a window can be open WITHOUT a fresh inbound
# ---------------------------------------------------------------------------

def _window(db, hours_ago):
    db.tables["conversations"] = [{
        "our_number": "15556677792", "business_number": "96894639405",
        "last_inbound_at": (datetime.now(timezone.utc)
                            - timedelta(hours=hours_ago)).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }]


def test_sweep_dispatches_when_window_open_without_new_inbound(db):
    """The gap the sweeper exists to close: the business messaged us 2h ago,
    requests queued up afterwards, and no further inbound is coming."""
    async def go():
        await _queue(db, 3)
        _window(db, hours_ago=2)
        return await dq.sweep_open_windows()
    res = asyncio.run(go())
    assert res["dispatched"] == 1
    assert len(db.sent) == 1


def test_sweep_respects_a_closed_window(db):
    """The sweeper must ASK — it may not assume a window the way the webhook can."""
    async def go():
        await _queue(db, 3)
        _window(db, hours_ago=30)
        return await dq.sweep_open_windows()
    res = asyncio.run(go())
    assert res["dispatched"] == 0
    assert db.sent == []
    assert all(r["state"] == dq.QUEUED for r in db.tables["pending_requests"])


def test_sweep_loop_survives_a_failing_pass(db, monkeypatch):
    """One bad sweep must not kill the loop for the life of the process."""
    import main
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("supabase blip")
        return {"businesses": 0, "dispatched": 0, "skipped": 0}

    import core.demand_queue as _dq
    monkeypatch.setattr(_dq, "sweep_open_windows", _boom)

    async def go():
        task = asyncio.create_task(main._digest_sweep_loop(interval_s=0))
        for _ in range(50):
            await asyncio.sleep(0)
            if calls["n"] >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert calls["n"] >= 2, "loop should have run again after the failure"


def _send_request():
    from core.models import (SendMessageRequest, Recipient, MessageContent,
                             MessageType, ChannelPreference)
    return SendMessageRequest(
        # E.164 in, digits-only in storage — the normalization boundary that
        # broke every correlation before norm_number() was applied everywhere.
        recipient=Recipient(id_type="phone", id_value="+96894639405",
                            country_code="OM"),
        content=MessageContent(body="Table for two at 7pm?"),
        message_type=MessageType.TRANSACTIONAL,
        channel_preference=ChannelPreference.WHATSAPP,
        business_id="biz_1",
        on_behalf_of="Basil",
    )
