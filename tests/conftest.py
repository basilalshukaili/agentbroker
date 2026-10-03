"""
Suite-wide fixtures.

find_business is backed by the public OpenStreetMap servers. No test may reach
them by accident, so every test gets an offline fake OSM installed as the
process-wide client (see tests/osm_fakes.py) and a fresh per-caller limiter.

The one exception is a test marked `live_osm`: it builds its own real client
and only runs when RUN_LIVE_OSM_TESTS=1.
"""
from __future__ import annotations

import pytest

from tests import osm_fakes


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_osm: talks to the real public OpenStreetMap servers; opt in with RUN_LIVE_OSM_TESTS=1",
    )


@pytest.fixture(autouse=True)
def _money_switches_start_off(monkeypatch):
    """No test may depend on the operator's CREDITS_ENABLED / DATA_METERING_ENABLED.

    Both are read at call time (billing/switches.py), and what the discovery descriptor says about
    payments is a function of them, so an exported variable on a developer's shell would change the
    committed edge snapshot comparison and every advertising test. A test that needs one on sets it
    itself (monkeypatch.setenv runs after this fixture and wins).
    """
    monkeypatch.delenv("CREDITS_ENABLED", raising=False)
    monkeypatch.delenv("DATA_METERING_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def _offline_osm(request):
    from supply import osm_client

    if request.node.get_closest_marker("live_osm"):
        previous = osm_client.set_client(None)
        try:
            yield
        finally:
            osm_client.set_client(previous)
        return

    client, _clock, _log = osm_fakes.make_client()
    previous = osm_client.set_client(client)
    osm_client.CALLER_LIMITER._buckets.clear()
    try:
        yield client
    finally:
        osm_client.set_client(previous)
        osm_client.CALLER_LIMITER._buckets.clear()
