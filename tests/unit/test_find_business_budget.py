"""find_business has a five-second budget, and a slow lookup is finished in the background, not killed.

THE PROBLEM (live, 2026-10-03): a successful lookup took 16-25 s - p50 16.2 s over eight ok calls - because
the public Overpass server is slow and busy and a dense area was narrowed with up to two more sequential
queries. The whole phase had a 25 s deadline, implemented as `asyncio.wait_for`, which CANCELS the work:
a query that needed 7 s was therefore started and killed for every caller and the cache never warmed.

THE CHANGE: the call returns at CALL_BUDGET_S with what is ready and says `search_in_progress` (status
partial, not an error); the lookup carries on in the background, fills the cache, and an identical repeat
is answered from it. These tests use REAL (short) sleeps, because the budget is wall-clock by design;
the fake OSM's clock only drives the client's request spacing and cache lifetimes.
"""
from __future__ import annotations

import asyncio
import gc
import time

import httpx
import pytest

from core import find_business as FB
from core.models import FindBusinessRequest
from supply import osm_client
from tests import osm_fakes


def run(coro):
    return asyncio.run(coro)


def req(capability="haircut", city="Atlanta", **kw):
    return FindBusinessRequest(vertical="personal_services", location={"zip_or_city": city},
                               capability=capability, **kw)


@pytest.fixture
def install():
    def _install(**kwargs):
        client, clock, log = osm_fakes.make_client(**kwargs)
        osm_client.set_client(client)
        return client, clock, log
    return _install


def slow_overpass(delay: float, seen: dict, *, status: int = 200):
    """An Overpass that takes `delay` real seconds, and records whether it ever finished."""
    async def handler(request: httpx.Request) -> httpx.Response:
        seen["started"] = seen.get("started", 0) + 1
        await asyncio.sleep(delay)
        seen["finished"] = seen.get("finished", 0) + 1
        if status != 200:
            return httpx.Response(status)
        q = dict(httpx.QueryParams(request.content.decode()))["data"]
        return httpx.Response(200, json=osm_fakes.overpass_answer(q, osm_fakes.DATASET))
    return handler


# ---------------------------------------------------------------------------
# the budget itself
# ---------------------------------------------------------------------------

def test_the_default_budget_is_five_seconds_not_twenty_five():
    assert FB.CALL_BUDGET_S == 5.0
    assert not hasattr(FB, "OSM_PHASE_DEADLINE_S"), "the old 25 s deadline constant must not linger beside the new one"


@pytest.mark.parametrize("raw,expected", [("abc", 5.0), ("7", 7.0), ("0.1", 1.0), ("99", 25.0), ("-4", 1.0), ("", 5.0)])
def test_the_budget_can_be_tuned_by_environment_but_only_within_sane_bounds(monkeypatch, raw, expected):
    monkeypatch.setenv("FIND_BUSINESS_BUDGET_S", raw)
    assert FB._budget_from_env() == expected


def test_the_budget_is_five_seconds_when_the_variable_is_not_set(monkeypatch):
    monkeypatch.delenv("FIND_BUSINESS_BUDGET_S", raising=False)
    assert FB._budget_from_env() == 5.0


def test_a_slow_lookup_returns_at_the_budget_as_pending_and_says_where_it_is_looking(install, monkeypatch):
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.1)
    seen: dict = {}
    install(overpass_override=slow_overpass(0.6, seen))

    async def go():
        t = time.monotonic()
        r = await FB.handle_find_business(req())
        return r, time.monotonic() - t, dict(seen)

    r, took, at_return = run(go())
    assert took < 0.4, "the caller must not wait for the slow upstream"
    assert at_return.get("finished", 0) == 0, "the lookup was still running when the call returned"
    assert r.status.value == "partial" and r.reason_code == "search_in_progress" and r.retriable is True
    s = r.result["search"]
    assert s["status"] == "pending" and s["continues_in_background"] is True
    assert s["within_budget"] is False and s["budget_s"] == 0.1 and s["retry_after_s"] == FB.PENDING_RETRY_AFTER_S
    assert "Atlanta" in s["geocoded_place"]["display_name"], "the resolved place is shown so a wrong place is visible before waiting"
    assert r.result["businesses"] == [] and r.result["result_count"] == 0
    assert "NOT evidence that no matching businesses exist" in r.human_message
    assert "OpenStreetMap is not known to be down" in r.human_message
    assert any("Repeat this exact call" in a for a in r.next_actions)
    assert r.result["attribution"], "attribution rides every result set, partial ones included"


def test_a_pending_answer_is_not_an_mcp_error(install, monkeypatch):
    """`isError` is derived from status == failure; PARTIAL must not read as a broken tool."""
    import json
    from agent_interface.mcp_server import handle_mcp_request
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.1)
    install(overpass_override=slow_overpass(0.6, {}))
    resp = run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "find_business", "arguments": {"location": "Atlanta", "capability": "haircut"}}}, {}))
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"])["reason_code"] == "search_in_progress"


def test_the_lookup_is_finished_in_the_background_and_the_repeat_is_instant(install, monkeypatch):
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.1)
    seen: dict = {}
    client, _clock, _log = install(overpass_override=slow_overpass(0.3, seen))

    async def go():
        first = await FB.handle_find_business(req())
        assert seen.get("finished", 0) == 0
        await asyncio.sleep(0.5)                       # the background lookup completes and fills the cache
        t = time.monotonic()
        second = await FB.handle_find_business(req())
        return first, second, time.monotonic() - t

    first, second, repeat_took = run(go())
    assert first.reason_code == "search_in_progress"
    assert second.reason_code == "businesses_found" and second.status.value == "success"
    assert second.result["search"]["cached"] is True and second.result["search"]["within_budget"] is True
    assert repeat_took < 0.05, f"the repeat took {repeat_took:.3f}s; it should be a cache read"
    assert seen["started"] == 1 and seen["finished"] == 1
    assert client.upstream_calls["overpass"] == 1, "the repeat must not start the slow query a second time"


def test_a_repeat_made_while_the_lookup_is_still_running_joins_it_instead_of_starting_another(install, monkeypatch):
    seen: dict = {}
    client, _clock, _log = install(overpass_override=slow_overpass(0.4, seen))

    async def go():
        monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.1)
        first = await FB.handle_find_business(req())
        monkeypatch.setattr(FB, "CALL_BUDGET_S", 2.0)
        second = await FB.handle_find_business(req())      # arrives mid-flight, has time to wait
        return first, second

    first, second = run(go())
    assert first.reason_code == "search_in_progress"
    assert second.reason_code == "businesses_found"
    assert client.upstream_calls["overpass"] == 1 and seen["started"] == 1, "one upstream query served both callers"


def test_finished_background_lookups_are_forgotten(install, monkeypatch):
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.05)
    install(overpass_override=slow_overpass(0.2, {}))

    async def go():
        await FB.handle_find_business(req())
        assert len(FB._BACKGROUND) == 1
        await asyncio.sleep(0.4)
        return len(FB._BACKGROUND)

    assert run(go()) == 0


def test_a_background_lookup_that_fails_leaves_no_unretrieved_exception_behind(install, monkeypatch):
    """The failure belongs to the NEXT caller (it is retried); the orphaned task must not also shout."""
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.05)
    install(overpass_override=slow_overpass(0.15, {}, status=503))
    noise: list = []

    async def go():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: noise.append(ctx))
        first = await FB.handle_find_business(req())
        await asyncio.sleep(0.6)                           # the 503 (and its one retry) arrive after we returned
        gc.collect()
        return first

    first = run(go())
    assert first.reason_code == "search_in_progress"
    assert noise == [], noise
    assert len(FB._BACKGROUND) == 0


def test_over_the_background_cap_the_call_falls_back_to_cancelling_at_the_budget(install, monkeypatch):
    """A burst of distinct slow lookups cannot queue unbounded tasks: past the cap the old behaviour applies."""
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.05)
    monkeypatch.setattr(FB, "MAX_BACKGROUND_PHASES", 0)
    seen: dict = {}
    install(overpass_override=slow_overpass(0.3, seen))

    async def go():
        r = await FB.handle_find_business(req())
        await asyncio.sleep(0.5)
        return r

    r = run(go())
    assert r.reason_code == "search_in_progress"
    assert seen.get("finished", 0) == 0, "inline mode cancels the lookup at the budget"
    assert len(FB._BACKGROUND) == 0


def test_self_test_stays_green_when_the_upstream_is_merely_slow(install, monkeypatch):
    """self_test is what monitors and directory testers poll. A slow Overpass answering 'search_in_progress'
    is the contract working, not a failure - otherwise every slow minute on a volunteer-run server would
    turn the health check red (it passed through osm_temporarily_unavailable but not through this)."""
    from agent_interface import self_test
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.05)
    install(overpass_override=slow_overpass(0.3, {}))
    check = run(self_test._check_find_business())
    assert check.passed is True and "search_in_progress" in check.error


def test_a_fast_lookup_is_unchanged_and_reports_it_was_within_budget(install):
    install()
    r = run(FB.handle_find_business(req()))
    assert r.reason_code == "businesses_found"
    assert r.result["search"]["within_budget"] is True and r.result["search"]["budget_s"] == FB.CALL_BUDGET_S
    assert r.result["result_count"] == len(r.result["businesses"])
    assert "continues_in_background" not in r.result["search"]


def test_a_real_outage_is_still_an_outage_not_pending(install, monkeypatch):
    """Pending is only for 'ran out of budget'. A 503 inside the budget keeps its old, different answer."""
    install(overpass_override=lambda request: httpx.Response(503))
    r = run(FB.handle_find_business(req()))
    assert r.reason_code == "osm_temporarily_unavailable" and r.result["search"]["status"] == "unavailable"


def test_an_unresolvable_place_is_not_pending(install):
    r = run(FB.handle_find_business(req(city="Nowhereville Atlantis")))
    assert r.result["search"]["status"] == "location_not_found" and r.reason_code == "no_results"


def test_the_supply_network_half_is_still_returned_when_the_search_is_pending(install, monkeypatch):
    import supply.smb_directory as sd
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.05)
    install(overpass_override=slow_overpass(0.3, {}))
    monkeypatch.setitem(sd._DIRECTORY, "smb_budget_real", sd.SMBEntry(
        smb_id="smb_budget_real", name="Budget Test Salon", vertical=req().vertical, address="1 A St",
        city="Atlanta", state="GA", zip_code="30303", capabilities=["haircut"],
        channels_available=["direct_api:calcom"], is_demo=False))
    r = run(FB.handle_find_business(req()))
    assert r.reason_code == "search_in_progress" and r.status.value == "partial"
    assert r.result["result_count"] == 1 and r.result["businesses"][0]["source"] == "supply_network"
    assert "Returning 1 supply-network row(s)" in r.human_message
