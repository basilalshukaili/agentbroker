"""Assignment #8 -- idempotency must hold under CONCURRENT and INTERRUPTED
execution, not just sequential replay.

THE DEFECT, as found in agent_interface/mcp_server.py's `_h_tools_call`
(idempotency dispatch) before this fix:

    _hit = await _ig.get(_scope, name, idem_key)      # 1. check
    if _hit is not None:
        ... return cached ...
    resp = await _h_tools_call_impl(params, headers)  # 2. execute
    if not resp.get("isError"):
        await _ig.put(_scope, name, idem_key, _ah, resp)  # 3. record

The record is written ONLY AFTER execution succeeds, and only best-effort.
That has two holes:

  1. CONCURRENT duplicates. Two calls with the SAME idempotency_key that
     arrive close together both reach step 1, both see "not seen yet" (the
     first has not reached step 3), and both proceed to step 2 -- TWO real
     side effects, TWO real charges, for what the caller believed was one
     idempotent request.

  2. INTERRUPTED execution. If this process is killed after step 2 (the
     real booking already happened) but before step 3 ever runs or lands
     durably, nothing durable exists for this key. A retry -- from this
     process after a restart, or a peer process -- sees "not seen yet" and
     re-executes, a SECOND real side effect for a request that already
     succeeded once.

THE FIX replaces the check-then-record pair with an atomic CLAIM:
storage/idempotency_store.py's `reserve()` performs the check-and-mark in one
synchronous call (no `await` in between, so no other coroutine on the same
event loop can observe the store mid-check -- see its own docstring), and
agent_interface/idempotency_gate.py's `claim()` additionally consults a
durable PENDING marker written BEFORE dispatch, so a process that crashed
mid-flight leaves a trace the next claim (from any process) will see and
refuse to run past.

Positive controls reused from tests/unit/test_idempotency_dispatch.py
(sequential replay, conflict, and failure-is-not-pinned) are NOT duplicated
here -- they already pin the sequential contract and must keep passing
unmodified; this file adds the two cases that contract never exercised.
"""
from __future__ import annotations

import asyncio

import pytest

import agent_interface.idempotency_gate as ig
import agent_interface.mcp_server as mcp
from storage.idempotency_store import get_idempotency_store


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No real Supabase network. Durable layer defaults to empty/no-op
    unless a test injects its own fake store (see _fake_durable below)."""
    async def _no_lookup(*a, **k):
        return None

    async def _no_claim(*a, **k):
        return None

    async def _no_resolve(*a, **k):
        return None

    monkeypatch.setattr(ig, "_durable_lookup", _no_lookup)
    monkeypatch.setattr(ig, "_durable_claim", _no_claim)
    monkeypatch.setattr(ig, "_durable_resolve", _no_resolve)
    get_idempotency_store()._store.clear()
    yield
    get_idempotency_store()._store.clear()


def _params(key=None, body="hi"):
    args = {"recipient": {"id_value": "+15551230000"}, "content": {"body": body}}
    if key:
        args["idempotency_key"] = key
    return {"name": "send_message", "arguments": args}


HDRS = {"x-agent-identity": "test-bearer-token-concurrency"}


# ---------------------------------------------------------------------------
# DEFECT 1 -- CONCURRENT duplicate requests.
# ---------------------------------------------------------------------------

def test_concurrent_duplicate_calls_execute_the_tool_only_once(monkeypatch):
    """Two calls sharing one idempotency_key, launched together. The old
    check-then-record pair let both through to _h_tools_call_impl; the fix's
    atomic reserve() must let exactly one."""
    calls = {"n": 0}

    async def fake_impl(params, headers=None):
        calls["n"] += 1
        # Simulate real work with a genuine suspension point, so a monkeypatch
        # that forgot to protect the WINDOW (not just the initial check) would
        # still be caught even if reserve() were removed.
        await asyncio.sleep(0.01)
        return {"content": [{"type": "text", "text": f"booked-{calls['n']}"}],
                "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)

    async def _both():
        return await asyncio.gather(
            mcp._h_tools_call(_params(key="concurrent-1"), HDRS),
            mcp._h_tools_call(_params(key="concurrent-1"), HDRS),
        )

    r1, r2 = asyncio.run(_both())

    assert calls["n"] == 1, (
        f"the real tool was dispatched {calls['n']} times for one "
        f"idempotency_key issued concurrently -- this is the double-booking "
        f"/ double-charge hole")

    results = [r1, r2]
    in_progress = [r for r in results if "idempotency_in_progress" in
                   r["content"][0]["text"]]
    executed = [r for r in results if "booked-" in r["content"][0]["text"]]
    assert len(in_progress) == 1, (
        "exactly one of the two concurrent callers must be told a request "
        "with this key is already in flight")
    assert len(executed) == 1
    # The refused caller's response must itself be an honest, uncharged
    # failure -- never something a caller could mistake for a second receipt.
    assert in_progress[0]["isError"] is True


def test_the_refused_concurrent_caller_never_reaches_dispatch_at_all(monkeypatch):
    """Every billing branch (x402, credits, free quota) lives INSIDE
    _h_tools_call_impl (see _h_tools_call's own docstring: "Wrapping here
    covers every billing branch"). A caller that never reaches it cannot be
    charged, by construction -- this is the mechanism 'at most one charge'
    rests on for the concurrent case."""
    dispatch_count = {"n": 0}

    async def fake_impl(params, headers=None):
        dispatch_count["n"] += 1
        await asyncio.sleep(0.01)
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(mcp, "_h_tools_call_impl", fake_impl)

    async def _three():
        return await asyncio.gather(
            mcp._h_tools_call(_params(key="concurrent-2"), HDRS),
            mcp._h_tools_call(_params(key="concurrent-2"), HDRS),
            mcp._h_tools_call(_params(key="concurrent-2"), HDRS),
        )

    asyncio.run(_three())
    assert dispatch_count["n"] == 1, (
        f"{dispatch_count['n']} of 3 concurrent identical requests reached "
        f"the billable dispatch path; must be exactly 1")


# ---------------------------------------------------------------------------
# DEFECT 2 -- INTERRUPTED execution (process restart mid-flight).
# ---------------------------------------------------------------------------

def test_a_process_restart_mid_flight_does_not_re_execute(monkeypatch):
    """Simulates: process A claims the key, the real tool call happens, and
    the process is killed BEFORE complete()/release() ever runs (no durable
    write of the outcome, exactly like a real crash). A restarted process
    (modelled here as the SAME process with its in-memory store wiped, which
    is precisely what a restart looks like from this store's point of view)
    must see the durable PENDING marker process A left behind and refuse to
    execute the tool a second time.
    """
    fake_db: dict[tuple, dict] = {}

    def _row_key(scope, tool, key):
        return (scope, tool, key)

    async def _fake_lookup(scope, tool, key):
        return fake_db.get(_row_key(scope, tool, key))

    async def _fake_claim(scope, tool, key):
        fake_db[_row_key(scope, tool, key)] = {
            "agent_scope": scope, "operation": tool, "idem_key": key,
            "status": "pending",
        }

    async def _fake_resolve(scope, tool, key, status, ahash=None, response=None):
        row = fake_db.setdefault(_row_key(scope, tool, key), {})
        row["status"] = status
        if ahash is not None:
            row["args_hash"] = ahash
        if response is not None:
            row["response"] = response

    monkeypatch.setattr(ig, "_durable_lookup", _fake_lookup)
    monkeypatch.setattr(ig, "_durable_claim", _fake_claim)
    monkeypatch.setattr(ig, "_durable_resolve", _fake_resolve)

    side_effects = {"n": 0}

    async def _process_a():
        status, _ = await ig.claim("scope_a", "schedule_appointment", "restart-key-1")
        assert status == "claimed"
        # The real booking happens here...
        side_effects["n"] += 1
        # ...and the process is killed before complete()/release() ever runs.
        # (Deliberately NOT calling ig.complete()/ig.release() here.)

    asyncio.run(_process_a())
    assert side_effects["n"] == 1
    assert fake_db[("scope_a", "schedule_appointment", "restart-key-1")]["status"] == "pending"

    # --- SIMULATE THE RESTART: wipe in-memory state, keep the durable layer. ---
    get_idempotency_store()._store.clear()

    async def _process_b_retries():
        status, outcome = await ig.claim("scope_a", "schedule_appointment", "restart-key-1")
        if status == "claimed":
            side_effects["n"] += 1  # would be a SECOND real booking
        return status, outcome

    status, outcome = asyncio.run(_process_b_retries())

    assert status == "in_progress", (
        f"a retry after a crash mid-flight got status={status!r} instead of "
        f"in_progress -- it would re-execute the tool and double-book")
    assert side_effects["n"] == 1, (
        f"the tool ran {side_effects['n']} times across a crash-then-retry "
        f"sequence for the SAME idempotency_key -- must be exactly 1")


def test_a_claim_that_completes_normally_is_not_mistaken_for_a_crash(monkeypatch):
    """Positive control: the restart-detection path above must not fire for
    the ordinary, successful case -- a key that completed durably must
    replay, not block."""
    fake_db: dict[tuple, dict] = {}

    async def _fake_lookup(scope, tool, key):
        return fake_db.get((scope, tool, key))

    async def _fake_claim(scope, tool, key):
        fake_db[(scope, tool, key)] = {"status": "pending"}

    async def _fake_resolve(scope, tool, key, status, ahash=None, response=None):
        row = fake_db.setdefault((scope, tool, key), {})
        row["status"] = status
        if ahash is not None:
            row["args_hash"] = ahash
        if response is not None:
            row["response"] = response

    monkeypatch.setattr(ig, "_durable_lookup", _fake_lookup)
    monkeypatch.setattr(ig, "_durable_claim", _fake_claim)
    monkeypatch.setattr(ig, "_durable_resolve", _fake_resolve)

    async def _flow():
        status, _ = await ig.claim("scope_b", "op", "clean-key")
        assert status == "claimed"
        await ig.complete("scope_b", "op", "clean-key", "hash123", {"ok": True})

    asyncio.run(_flow())
    get_idempotency_store()._store.clear()  # simulate restart

    async def _replay():
        return await ig.claim("scope_b", "op", "clean-key")

    status, outcome = asyncio.run(_replay())
    assert status == "complete"
    assert outcome["response"] == {"ok": True}


# ---------------------------------------------------------------------------
# Claim/release/complete API, exercised directly (finer-grained proof).
# ---------------------------------------------------------------------------

def test_release_after_a_transient_failure_allows_a_fresh_claim():
    async def _flow():
        status, _ = await ig.claim("scope_c", "op", "k")
        assert status == "claimed"
        await ig.release("scope_c", "op", "k")
        status2, _ = await ig.claim("scope_c", "op", "k")
        return status2

    assert asyncio.run(_flow()) == "claimed"


def test_two_different_keys_never_block_each_other():
    """Non-discriminating positive control: independent keys are independent
    regardless of the claim/complete/release mechanics."""
    async def _flow():
        s1, _ = await ig.claim("scope_d", "op", "key-1")
        s2, _ = await ig.claim("scope_d", "op", "key-2")
        return s1, s2

    s1, s2 = asyncio.run(_flow())
    assert s1 == "claimed"
    assert s2 == "claimed"
