"""The pre-warm makes the likeliest find_business lookups already be in the cache - politely.

It exists because the cache is in-process (each deploy empties it) and a cold lookup costs 16-25 s on the
public servers. The properties that matter, and are therefore tested rather than assumed:

  * a warm lookup is made the way a REAL call makes it, so a later real call is answered from cache with
    no upstream request (the proof that the cache keys line up);
  * it replaces entries before they expire (refresh), so it does not warm once and then go cold;
  * it never retries into a problem: a 429, a block, an open circuit or the kill switch ends the run;
  * it pauses between lookups, is OFF outside production, and is wired into the app's start-up.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import config
from core import find_business as FB
from core import find_business_prewarm as PW
from core.models import FindBusinessRequest
from supply import osm_client
from tests import osm_fakes


def run(coro):
    return asyncio.run(coro)


def req(capability, city="Atlanta", **kw):
    return FindBusinessRequest(vertical="personal_services", location={"zip_or_city": city},
                               capability=capability, **kw)


async def no_sleep(_s):
    return None


class SleepLog:
    def __init__(self):
        self.calls = []

    async def __call__(self, s):
        self.calls.append(s)


@pytest.fixture
def fake():
    client, clock, log = osm_fakes.make_client()
    osm_client.set_client(client)
    return client, clock, log


# ---------------------------------------------------------------------------
# the cache keys line up with real calls
# ---------------------------------------------------------------------------

def test_a_real_call_after_the_warm_up_is_answered_from_cache_with_no_upstream_request(fake):
    client, _clock, log = fake
    stats = run(PW.run_once(client=client, lookups=[("Atlanta", "haircut")], pause_s=0, sleep=no_sleep))
    assert stats == {"attempted": 1, "warmed": 1, "failed": 0, "aborted": None}
    upstream_after_warm = dict(client.upstream_calls)
    n = len(log)

    r = run(FB.handle_find_business(req("haircut")))
    assert r.reason_code == "businesses_found" and r.result["search"]["cached"] is True
    assert len(log) == n and client.upstream_calls == upstream_after_warm, "the real call must not touch the upstream"


def test_the_kind_may_be_sent_as_a_vertical_word_and_still_hits_the_warm_entry(fake):
    client, _c, log = fake
    run(PW.run_once(client=client, lookups=[("Atlanta", "restaurant")], pause_s=0, sleep=no_sleep))
    n = len(log)
    r = run(FB.handle_find_business(FindBusinessRequest(
        vertical="restaurants", location={"zip_or_city": "Atlanta"})))
    assert r.result["search"]["cached"] is True and len(log) == n


def test_a_dense_area_is_narrowed_fully_and_the_narrowed_answers_are_cached_too(fake):
    """Nobody is waiting, so the pre-warm ignores the caller-facing 2 s narrowing window."""
    dense = [osm_fakes.node(7000 + i, f"Diner {i}", 33.7490 + i * 0.00001, -84.3880, amenity="restaurant")
             for i in range(400)]
    client, _clock, log = osm_fakes.make_client(dataset=dense)
    osm_client.set_client(client)
    run(PW.run_once(client=client, lookups=[("Atlanta", "restaurant")], pause_s=0, sleep=no_sleep))
    assert client.upstream_calls["overpass"] == 1 + FB.MAX_NARROW_STEPS
    n = len(log)
    r = run(FB.handle_find_business(req("restaurant")))
    assert r.result["search"]["radius_narrowed"] is True and len(log) == n, "the narrowed answer was served from cache"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def test_a_second_pass_replaces_the_entry_before_it_expires(fake):
    client, clock, _log = fake
    run(PW.run_once(client=client, lookups=[("Atlanta", "haircut")], pause_s=0, sleep=no_sleep))
    assert client.upstream_calls["overpass"] == 1
    clock.advance(osm_client.OVERPASS_TTL_S - 100)             # nearly expired
    run(PW.run_once(client=client, lookups=[("Atlanta", "haircut")], pause_s=0, sleep=no_sleep))
    assert client.upstream_calls["overpass"] == 2, "refresh=True fetches again even though the entry is still valid"
    clock.advance(500)                                         # past the ORIGINAL expiry
    n = client.upstream_calls["overpass"]
    r = run(FB.handle_find_business(req("haircut")))
    assert r.result["search"]["cached"] is True and client.upstream_calls["overpass"] == n


def test_a_normal_search_never_refreshes(fake):
    client, _c, _l = fake
    run(FB.handle_find_business(req("haircut")))
    run(FB.handle_find_business(req("haircut")))
    assert client.upstream_calls["overpass"] == 1


# ---------------------------------------------------------------------------
# it never retries into a problem
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,reason", [(429, "upstream_rate_limited"), (403, "upstream_blocked")])
def test_a_rate_limit_or_a_block_ends_the_whole_run(status, reason):
    client, _clock, _log = osm_fakes.make_client(overpass_override=lambda request: httpx.Response(status))
    osm_client.set_client(client)
    stats = run(PW.run_once(client=client, lookups=[("Atlanta", "haircut"), ("Atlanta", "barber"), ("Boston", "barber")],
                            pause_s=0, sleep=no_sleep))
    assert stats["aborted"] == reason and stats["attempted"] == 1 and stats["failed"] == 1


def test_the_operators_kill_switch_ends_the_run(monkeypatch, fake):
    client, _c, _l = fake
    monkeypatch.setenv("FIND_BUSINESS_OSM_DISABLED", "1")
    stats = run(PW.run_once(client=client, lookups=[("Atlanta", "haircut"), ("Boston", "barber")], pause_s=0, sleep=no_sleep))
    assert stats["aborted"] == "disabled" and stats["attempted"] == 1
    assert client.upstream_calls == {"nominatim": 0, "overpass": 0}


def test_repeated_failures_end_the_run_instead_of_hammering():
    client, _clock, _log = osm_fakes.make_client(overpass_override=lambda request: httpx.Response(200, json={"nope": 1}))
    osm_client.set_client(client)
    lookups = [(p, "barber") for p in ("Atlanta", "Boston", "Austin", "Muscat", "Atlanta", "Boston")]
    stats = run(PW.run_once(client=client, lookups=lookups, pause_s=0, sleep=no_sleep))
    assert stats["aborted"] == "consecutive_failures" and stats["attempted"] == PW.MAX_CONSECUTIVE_FAILURES
    assert stats["warmed"] == 0


def test_a_success_resets_the_failure_streak(fake):
    client, _c, _l = fake
    stats = run(PW.run_once(client=client, lookups=[("Atlanta", "haircut"), ("Nowhereville Atlantis", "haircut"), ("Boston", "barber")],
                            pause_s=0, sleep=no_sleep))
    assert stats["aborted"] is None and stats["attempted"] == 3
    assert stats["warmed"] == 2, "an unresolvable place is not a failure and not a warm entry"


def test_lookups_are_paced():
    client, _clock, _log = osm_fakes.make_client()
    osm_client.set_client(client)
    sl = SleepLog()
    run(PW.run_once(client=client, lookups=[("Atlanta", "haircut"), ("Atlanta", "barber"), ("Boston", "barber")],
                    pause_s=20.0, sleep=sl))
    assert sl.calls == [20.0, 20.0], "a pause between lookups, none before the first"


# ---------------------------------------------------------------------------
# what is warmed, and when it runs
# ---------------------------------------------------------------------------

def test_every_warm_lookup_is_a_kind_the_category_table_knows():
    from supply import osm_categories as cat
    assert PW.pairs() and len(PW.pairs()) == len(PW.PLACES) * len(PW.KINDS)
    for kind in PW.KINDS:
        assert cat.known_term(kind), kind
    assert len(PW.pairs()) <= 30, "a short list: a few lookups a minute for a few minutes, every five hours"


def test_the_refresh_interval_is_inside_the_cache_lifetime():
    assert PW.REFRESH_EVERY_S < osm_client.OVERPASS_TTL_S


def test_the_first_request_waits_until_after_boot():
    assert PW.START_DELAY_S >= 60


@pytest.mark.parametrize("environment,flag,expected", [
    ("production", "", True), ("development", "", False), ("staging", "", False),
    ("production", "0", False), ("production", "false", False), ("production", "off", False),
    ("development", "1", True), ("development", "true", True),
])
def test_it_is_on_by_default_only_in_production_and_the_flag_overrides(monkeypatch, environment, flag, expected):
    monkeypatch.setattr(config, "ENVIRONMENT", environment)
    monkeypatch.setenv("FIND_BUSINESS_PREWARM", flag)
    assert PW.enabled() is expected


def test_start_does_nothing_when_off_and_returns_a_cancellable_task_when_on(monkeypatch):
    async def go():
        monkeypatch.setattr(config, "ENVIRONMENT", "development")
        monkeypatch.delenv("FIND_BUSINESS_PREWARM", raising=False)
        off = PW.start()
        monkeypatch.setenv("FIND_BUSINESS_PREWARM", "1")
        task = PW.start()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return off

    assert run(go()) is None


def test_the_loop_waits_then_warms_then_waits_again_and_survives_a_bad_pass(fake, monkeypatch):
    client, _c, _l = fake
    sl = SleepLog()
    passes = {"n": 0}
    real = PW.run_once

    async def flaky(**kw):
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("boom")                          # one bad pass must not end the loop
        return await real(**kw)

    monkeypatch.setattr(PW, "run_once", flaky)

    class Stop(Exception):
        pass

    async def sleep(s):
        await sl(s)
        if len(sl.calls) >= 4:
            raise Stop

    with pytest.raises(Stop):
        run(PW.prewarm_loop(client=client, lookups=[("Atlanta", "haircut")], start_delay_s=111.0,
                            interval_s=222.0, pause_s=0, sleep=sleep))
    # sleep, pass 1 (raises), sleep, pass 2, sleep, pass 3, sleep (stop): the loop outlived the bad pass
    assert sl.calls == [111.0, 222.0, 222.0, 222.0] and passes["n"] == 3


# ---------------------------------------------------------------------------
# it is wired into the app
# ---------------------------------------------------------------------------

def test_the_lifespan_starts_the_prewarm_and_stops_it_cleanly(monkeypatch):
    import main
    seen = {}

    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    def fake_start():
        seen["started"] = True
        return asyncio.get_running_loop().create_task(forever())

    monkeypatch.setattr(PW, "start", fake_start)

    async def go():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert seen.get("started") is True

    run(go())
    assert seen.get("cancelled") is True, "shutdown must cancel the pre-warm task"


def test_the_lifespan_does_not_start_it_outside_production(monkeypatch):
    import main
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.delenv("FIND_BUSINESS_PREWARM", raising=False)

    async def go():
        async with main.lifespan(main.app):
            return PW.start()

    assert run(go()) is None
