"""What the durable writer (storage/outcome_store.py) sends: every write carries the status it was MADE with, and
what it sends is something jsonb accepts. (The second half starts at "What the writer sends must be...", below.)

Every durable write carries the status it was MADE with, not the status the record has by the time it runs.

THE DEFECT. `OutcomeStore._fire_persist` scheduled the write with `asyncio.ensure_future(_supabase_upsert(...))`
and handed it the LIVE record dict. `_supabase_upsert` reads `record["status"]` when the task finally runs, and a
task does not run until its caller next yields. `set_pending` and the `set_complete` that follows it in
core/schedule_appointment.py's failed-enqueue branch are two synchronous calls in a row, so by the time the FIRST
task ran the record already said 'failure': both writes went out labelled 'failure', and the first of them with
every other column empty (no outcome was passed with it). When the two HTTP calls then landed in the opposite
order the receipt's reason and result were gone - and, because the write was labelled as final, nothing in the
database could tell it from a real final write (migrations/spine/012_operations_upsert_fix.sql refuses to let an
in-progress write replace a final one, but only if the write SAYS it is in progress).

Measured 2026-10-04 against a real PostgreSQL with an artificial delay on the first write: the durable row ended
(failure, no reason, no result) and a fresh process read the outcome as {}. The database half of the fix is
tested in test_spine_012_operations_upsert_pg.py (opt-in, needs docker); this file is the half that needs neither.
"""
from __future__ import annotations

import asyncio

from storage.outcome_store import OutcomeStore


def _run_and_collect(monkeypatch, steps):
    sent = []

    async def fake_rpc(fn, payload):
        sent.append((fn, dict(payload)))
        return {"operation_id": payload["p_operation_id"]}
    monkeypatch.setattr("storage.supabase_client.rpc", fake_rpc)

    async def go():
        store = OutcomeStore()
        steps(store)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)
    asyncio.run(go())
    return [payload for _fn, payload in sent]


def test_a_pending_write_is_still_labelled_pending_after_the_terminal_write_is_made(monkeypatch):
    """The two calls are synchronous back to back, exactly as in the failed-enqueue branch."""
    def steps(store):
        store.set_pending("op-snap-1", "schedule_appointment", agent_id="agent-a")
        store.set_complete("op-snap-1",
                           {"status": "failure", "reason_code": "async_channel_not_provisioned"},
                           agent_id="agent-a")
    payloads = _run_and_collect(monkeypatch, steps)
    assert [(p["p_status"], p["p_reason_code"]) for p in payloads] == [
        ("pending", None), ("failure", "async_channel_not_provisioned")]
    assert [p["p_result_json"] is None for p in payloads] == [True, False]
    assert {p["p_tool"] for p in payloads} == {"schedule_appointment"}
    assert {p["p_agent_id"] for p in payloads} == {"agent-a"}


def test_every_write_in_a_pending_executing_complete_run_keeps_its_own_status(monkeypatch):
    def steps(store):
        store.set_pending("op-snap-2", "schedule_appointment", agent_id="agent-a")
        store.set_executing("op-snap-2")
        store.set_complete("op-snap-2", {"status": "success", "reason_code": "appointment_confirmed",
                                         "result": {"appointment_id": "appt-snap"}}, agent_id="agent-a")
    payloads = _run_and_collect(monkeypatch, steps)
    assert [p["p_status"] for p in payloads] == ["pending", "executing", "success"]
    # the executing write names the tool the record was created for, even though set_executing is given none
    assert [p["p_tool"] for p in payloads] == ["schedule_appointment"] * 3
    assert [p["p_appointment_id"] for p in payloads] == [None, None, "appt-snap"]


# ---------------------------------------------------------------------------------------------------------------
# What the writer sends must be something jsonb ACCEPTS. A NUL character, a lone surrogate, NaN and Infinity are all
# things a Python dict can hold and the database refuses (22P05 / 22P02), and a refused call lost the WHOLE durable
# record - status, reason and owner included - not just the offending field. The cancellation receipt echoes a
# caller-supplied field, so a caller controls one of these for its own operation. Self-inflicted only; no cross-agent
# effect was found. The real-database proof is in test_spine_012_operations_upsert_pg.py.
# ---------------------------------------------------------------------------------------------------------------
import json  # noqa: E402

import pytest  # noqa: E402

class _Odd:
    """Not JSON-serialisable, so the serialiser asks `default` for it - and the text it answers with is hostile."""

    def __str__(self):
        return "o\x00d\ud800d"


HOSTILE = {
    "object_with_hostile_text": {"thing": _Odd()},
    "nul": {"human_message": "a\x00b"},
    "nan": {"score": float("nan")},
    "infinity": {"score": float("inf"), "other": float("-inf")},
    "lone_surrogate": {"human_message": "x\ud800y"},
    "nul_in_a_key": {"human\x00key": "ok"},
    "nested": {"a": [{"b": "c\x00d"}, float("nan"), ("t", "u\x00v")]},
}


def _strict_load(text):
    """What PostgreSQL's jsonb_in accepts, as near as Python can say: no NaN/Infinity, nothing a UTF-8 encoder
    refuses (a lone surrogate), no U+0000 anywhere."""
    def refuse(token):
        raise ValueError(f"not JSON: {token}")
    value = json.loads(text, parse_constant=refuse)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            assert "\x00" not in node, "a NUL character would be refused by jsonb (22P05)"
            node.encode("utf-8")               # a lone surrogate raises here (jsonb refuses it: 22P02)
    walk(value)
    return value


def _write(monkeypatch, outcome, *, rpc=None, **kwargs):
    from storage import outcome_store as mod
    sent = []

    async def fake_rpc(fn, payload):
        sent.append(dict(payload))
        return {"operation_id": payload["p_operation_id"]}
    monkeypatch.setattr("storage.supabase_client.rpc", rpc(sent) if rpc else fake_rpc)
    record = {"status": "success", "operation_type": "schedule_appointment"}
    ok = asyncio.run(mod._supabase_upsert("op-x", record, outcome, kwargs.get("tool"), kwargs.get("agent_id")))
    return ok, sent


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_the_result_sent_to_the_database_is_valid_json_whatever_the_receipt_holds(monkeypatch, name):
    outcome = {"status": "success", "reason_code": "appointment_confirmed",
               "result": {"appointment_id": "appt-1"}, **HOSTILE[name]}
    ok, sent = _write(monkeypatch, outcome, tool="schedule_appointment", agent_id="agent-a")
    assert ok is True and len(sent) == 1
    value = _strict_load(sent[0]["p_result_json"])
    assert value["reason_code"] == "appointment_confirmed" and value["result"]["appointment_id"] == "appt-1"
    assert sent[0]["p_status"] == "success" and sent[0]["p_agent_id"] == "agent-a"
    assert sent[0]["p_appointment_id"] == "appt-1"


def test_a_nan_becomes_null_and_a_nul_is_dropped_not_the_neighbouring_text(monkeypatch):
    outcome = {"status": "success", "score": float("nan"), "human_message": "keep\x00this", "ok": 1.5}
    _ok, sent = _write(monkeypatch, outcome)
    assert _strict_load(sent[0]["p_result_json"]) == {"status": "success", "score": None,
                                                      "human_message": "keepthis", "ok": 1.5}


def test_a_lone_surrogate_is_replaced_and_a_valid_pair_is_kept(monkeypatch):
    outcome = {"status": "success", "lone": "x\ud800y", "pair": "😀", "arabic": "مسقط"}
    _ok, sent = _write(monkeypatch, outcome)
    value = _strict_load(sent[0]["p_result_json"])
    assert value["lone"] == "x�y"
    assert value["pair"] == "\U0001f600"
    assert value["arabic"] == "مسقط"


def test_text_the_serialiser_asks_an_object_for_is_cleaned_too(monkeypatch):
    _ok, sent = _write(monkeypatch, {"status": "success", "thing": _Odd()})
    assert _strict_load(sent[0]["p_result_json"])["thing"] == "od�d"


def test_a_bare_nan_is_never_sent_even_if_the_cleaner_is_bypassed(monkeypatch):
    """allow_nan=False is the last line behind the cleaner. With the cleaner out of the way a NaN makes the
    serialiser raise, the result is dropped and the row still goes - and the token NaN never reaches the database."""
    from storage import outcome_store as mod
    monkeypatch.setattr(mod, "_json_safe", lambda value: value)
    ok, sent = _write(monkeypatch, {"status": "success", "score": float("nan"), "result": {"appointment_id": "a-9"}},
                      agent_id="agent-a")
    assert ok is True and len(sent) == 1
    assert sent[0]["p_result_json"] is None
    assert sent[0]["p_appointment_id"] == "a-9" and sent[0]["p_agent_id"] == "agent-a"


def test_the_text_columns_are_cleaned_too(monkeypatch):
    outcome = {"status": "success", "reason_code": "r\x00c", "result": {"appointment_id": "appt\x00-9"}}
    _ok, sent = _write(monkeypatch, outcome, tool="t\x00ool", agent_id="ag\x00ent")
    p = sent[0]
    assert (p["p_tool"], p["p_reason_code"], p["p_appointment_id"], p["p_agent_id"]) == ("tool", "rc", "appt-9", "agent")


def test_a_result_that_cannot_be_serialised_is_dropped_but_the_row_is_still_written(monkeypatch):
    loop = {"status": "success", "reason_code": "appointment_confirmed", "result": {"appointment_id": "appt-2"}}
    loop["result"]["self"] = loop["result"]
    ok, sent = _write(monkeypatch, loop, tool="schedule_appointment", agent_id="agent-a")
    assert ok is True and len(sent) == 1
    assert sent[0]["p_result_json"] is None
    assert (sent[0]["p_status"], sent[0]["p_reason_code"], sent[0]["p_appointment_id"], sent[0]["p_agent_id"]) \
        == ("success", "appointment_confirmed", "appt-2", "agent-a")


def _refusing(code, http=400):
    """An rpc that refuses any call carrying a result, the way PostgREST reports a data exception."""
    def make(sent):
        async def rpc(fn, payload):
            sent.append(dict(payload))
            if payload["p_result_json"] is not None:
                raise RuntimeError(f"rpc({fn!r}) failed: HTTP {http} body="
                                   f'{{"code":"{code}","details":null,"hint":null,"message":"refused"}}')
            return {"operation_id": payload["p_operation_id"]}
        return rpc
    return make


def test_a_data_exception_is_retried_once_without_the_result_so_status_and_owner_still_land(monkeypatch):
    outcome = {"status": "success", "reason_code": "appointment_confirmed", "result": {"appointment_id": "appt-3"}}
    ok, sent = _write(monkeypatch, outcome, rpc=_refusing("22023"), tool="schedule_appointment", agent_id="agent-a")
    assert ok is True and len(sent) == 2
    assert sent[0]["p_result_json"] is not None and sent[1]["p_result_json"] is None
    assert (sent[1]["p_status"], sent[1]["p_reason_code"], sent[1]["p_appointment_id"], sent[1]["p_agent_id"]) \
        == ("success", "appointment_confirmed", "appt-3", "agent-a")


@pytest.mark.parametrize("code", ["42501", "42883", "57014", "08006", "XX000"])
def test_anything_else_is_not_retried(monkeypatch, code):
    """A permission refusal in particular must never be answered by trying again with less: the attacker case
    (42501) is exactly the one where a second call would be a second attempt."""
    outcome = {"status": "success", "reason_code": "appointment_confirmed", "result": {"appointment_id": "appt-4"}}
    ok, sent = _write(monkeypatch, outcome, rpc=_refusing(code), tool="schedule_appointment", agent_id="agent-a")
    assert ok is False and len(sent) == 1


def test_a_transport_failure_is_not_retried(monkeypatch):
    def make(sent):
        async def rpc(fn, payload):
            sent.append(dict(payload))
            raise RuntimeError(f"rpc({fn!r}) transport error: timed out")
        return rpc
    ok, sent = _write(monkeypatch, {"status": "success", "result": {"appointment_id": "a"}}, rpc=make)
    assert ok is False and len(sent) == 1
