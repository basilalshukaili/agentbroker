"""self_test answers fast even when the public map servers do not.

THE EVIDENCE (usage_events, 2026-10-01 .. 2026-10-09, docs/reviews/2026-10-09-agentbroker-request-analysis.md):
self_test is the free health tool registries and directory testers poll - 619 calls in the window, 127 from outside
callers, second only to the tools people actually use. 51 of the 619 took a second or more, 26 of them 5.0-5.9 s:
its first check runs a real find_business, which waits for the public OpenStreetMap servers up to the whole 5 s
call budget before it concedes "search_in_progress". The module promises "no real external APIs"; a health check
whose latency is a volunteer-run server's is a poor one for a poller that scores reliability.

THE CHANGE: the self_test's own find_business check gets its own, shorter budget (SELF_TEST_FIND_BUSINESS_BUDGET_S).
It still runs the whole OpenStreetMap path - so an internal fault in our code is still caught, which the existing
tests pin - and a lookup that is still running at the shorter budget is the same "upstream slow; contract intact"
pass it always was, only sooner. A caller's own find_business keeps the full budget.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from agent_interface import self_test
from core import find_business as FB
from core.models import FindBusinessRequest
from supply import osm_client
from tests import osm_fakes


def run(coro):
    return asyncio.run(coro)


def slow_overpass(delay: float, seen: dict):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen["started"] = seen.get("started", 0) + 1
        await asyncio.sleep(delay)
        seen["finished"] = seen.get("finished", 0) + 1
        q = dict(httpx.QueryParams(request.content.decode()))["data"]
        return httpx.Response(200, json=osm_fakes.overpass_answer(q, osm_fakes.DATASET))
    return handler


@pytest.fixture
def install():
    def _install(**kwargs):
        client, clock, log = osm_fakes.make_client(**kwargs)
        osm_client.set_client(client)
        return client, clock, log
    yield _install
    # let any background lookup the pending path started wind down before the next test
    for task in list(getattr(FB, "_BACKGROUND", ())):
        task.cancel()


def test_the_default_self_test_budget_is_well_under_the_call_budget():
    assert 0 < self_test.SELF_TEST_FIND_BUSINESS_BUDGET_S <= 2.5
    assert self_test.SELF_TEST_FIND_BUSINESS_BUDGET_S < FB.CALL_BUDGET_S


def test_a_slow_public_server_does_not_make_the_self_test_slow(install, monkeypatch):
    monkeypatch.setattr(self_test, "SELF_TEST_FIND_BUSINESS_BUDGET_S", 0.1)
    seen: dict = {}
    install(overpass_override=slow_overpass(0.8, seen))
    started = time.monotonic()
    check = run(self_test._check_find_business())
    took = time.monotonic() - started
    assert took < 0.5, f"the check waited {took:.2f}s for an upstream that needs 0.8s; it has a 0.1s budget"
    assert check.passed is True
    assert "search_in_progress" in check.error and "contract intact" in check.error


def test_the_budget_is_only_for_the_self_test_not_for_callers(install, monkeypatch):
    """A caller's own find_business still gets the full call budget: the same slow upstream finishes inside it."""
    monkeypatch.setattr(self_test, "SELF_TEST_FIND_BUSINESS_BUDGET_S", 0.1)
    seen: dict = {}
    install(overpass_override=slow_overpass(0.4, seen))
    req = FindBusinessRequest(vertical="personal_services", location={"zip_or_city": "Atlanta"},
                              capability="haircut")
    r = run(FB.handle_find_business(req))
    assert r.reason_code != "search_in_progress", "an ordinary call must not inherit the self-test's short budget"
    assert seen.get("finished", 0) >= 1


def test_a_budget_larger_than_the_call_budget_cannot_widen_it(install, monkeypatch):
    monkeypatch.setattr(FB, "CALL_BUDGET_S", 0.1)
    seen: dict = {}
    install(overpass_override=slow_overpass(0.6, seen))
    req = FindBusinessRequest(vertical="personal_services", location={"zip_or_city": "Atlanta"},
                              capability="haircut")
    started = time.monotonic()
    r = run(FB.handle_find_business(req, budget_s=30))
    assert time.monotonic() - started < 0.45
    assert r.reason_code == "search_in_progress" and r.result["search"]["budget_s"] == 0.1


def test_an_internal_fault_in_our_own_path_still_fails_the_self_test():
    """The shorter budget must not turn the check into one that only watches the clock."""
    previous = osm_client.set_client(object())        # no geocode(): an internal error in our own path
    try:
        check = run(self_test._check_find_business())
    finally:
        osm_client.set_client(previous)
    assert check.passed is False
    assert "Unexpected status" in check.error
