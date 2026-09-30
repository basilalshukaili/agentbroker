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
