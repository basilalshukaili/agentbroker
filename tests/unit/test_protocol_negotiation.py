"""The handshake must answer the caller, and must not regress.

WHY THIS EXISTS. The server answered a hardcoded `2024-11-05` to every client
regardless of what they offered. That is not a negotiation, it is a recording,
and it went unnoticed for months because nothing asserted on it — the initialize
response was only ever checked for the PRESENCE of a `protocolVersion` key
(`test_mcp_and_discovery.py`), which a frozen constant satisfies forever.

The cost is not tidiness. URL-mode elicitation — the out-of-band approval channel
the Warrant work depends on — arrived in `2025-11-25`, and a client will not offer
a capability to a server that answers three revisions behind.

THE REGRESSION GATE at the bottom is the point of the file. A version that drifts
backwards is silent: everything keeps working, nothing errors, and the feature we
need simply never becomes available. So the gate names the floor explicitly and
fails on a downgrade, and it proves it can still fail by checking its own
known-bad value.
"""
from __future__ import annotations

import pytest

from agent_interface.mcp_server import (PROTOCOL_VERSION,
                                        SUPPORTED_PROTOCOL_VERSIONS,
                                        negotiate_protocol_version)

# The oldest version we may EVER answer as our own preference. Raising this is a
# deliberate act; drifting below it is the bug this file exists to catch.
MINIMUM_PREFERRED = "2025-11-25"


def test_client_version_is_echoed_when_supported():
    for asked in SUPPORTED_PROTOCOL_VERSIONS:
        assert negotiate_protocol_version({"protocolVersion": asked}) == asked


def test_unknown_version_falls_back_to_our_newest():
    for asked in ("1999-01-01", "not-a-version", "", "2099-12-31"):
        assert negotiate_protocol_version({"protocolVersion": asked}) == PROTOCOL_VERSION


def test_missing_or_malformed_params_never_raise():
    for params in (None, {}, {"protocolVersion": None}, {"protocolVersion": 5},
                   {"protocolVersion": ["2025-06-18"]}, "not a dict", 42):
        assert negotiate_protocol_version(params) == PROTOCOL_VERSION


def test_caller_supplied_string_cannot_reach_the_response():
    """A version we do not support must never be echoed back.

    Echoing input straight into the handshake would let any caller put arbitrary
    text in a field other software reads to decide how to talk to us.
    """
    hostile = "2025-06-18'; DROP TABLE --"
    assert negotiate_protocol_version({"protocolVersion": hostile}) != hostile
    assert negotiate_protocol_version({"protocolVersion": hostile}) in SUPPORTED_PROTOCOL_VERSIONS


def test_preferred_version_is_the_newest_supported():
    assert PROTOCOL_VERSION == SUPPORTED_PROTOCOL_VERSIONS[0]
    assert list(SUPPORTED_PROTOCOL_VERSIONS) == sorted(SUPPORTED_PROTOCOL_VERSIONS,
                                                       reverse=True)


def test_backwards_compatibility_is_retained():
    """Old clients must keep working. Negotiation is not a cutoff."""
    assert "2024-11-05" in SUPPORTED_PROTOCOL_VERSIONS
    assert negotiate_protocol_version({"protocolVersion": "2024-11-05"}) == "2024-11-05"


def test_protocol_version_has_not_regressed():
    """The gate. Fails loudly if our preferred version drifts backwards."""
    assert PROTOCOL_VERSION >= MINIMUM_PREFERRED, (
        f"preferred protocol {PROTOCOL_VERSION} is older than the floor "
        f"{MINIMUM_PREFERRED} - url-mode elicitation needs 2025-11-25 or newer")


def test_the_gate_can_still_fail():
    """Prove the comparison above actually catches a downgrade.

    A gate that inspects nothing gets trusted. This feeds the same comparison the
    known-bad value the server used to return and requires it to fail.
    """
    known_bad = "2024-11-05"
    assert not (known_bad >= MINIMUM_PREFERRED), (
        "the regression check would not catch the very version this fixed")


@pytest.mark.parametrize("asked,expected", [
    ("2025-11-25", "2025-11-25"),
    ("2025-06-18", "2025-06-18"),
    ("2024-11-05", "2024-11-05"),
    ("2020-01-01", PROTOCOL_VERSION),
])
def test_negotiation_table(asked, expected):
    assert negotiate_protocol_version({"protocolVersion": asked}) == expected


def test_edge_mirrors_the_origin_version_list():
    """The edge worker keeps its OWN copy of the supported list, in TypeScript.

    Two hardcoded lists in two languages drift, and this repo has paid for that
    before: the edge answers `initialize` from a snapshot, so when its list falls
    behind, the CANONICAL host negotiates differently from the origin and nothing
    reports it - the origin is correct and the published URL is wrong. Rather than
    trust that a future edit touches both, read the TypeScript and compare.
    """
    import os
    import re

    edge = os.path.join(os.path.dirname(__file__), "..", "..",
                        "edge", "src", "mcp-edge.ts")
    edge = os.path.abspath(edge)
    assert os.path.exists(edge), f"edge source not found at {edge}"
    with open(edge, encoding="utf-8") as fh:
        src = fh.read()

    block = re.search(r"const SUPPORTED_PROTOCOL_VERSIONS\s*=\s*\[(.*?)\]",
                      src, re.S)
    assert block, "edge no longer declares SUPPORTED_PROTOCOL_VERSIONS"
    ts_versions = re.findall(r'"([^"]+)"', block.group(1))

    assert ts_versions == list(SUPPORTED_PROTOCOL_VERSIONS), (
        "edge and origin disagree on supported protocol versions:\n"
        f"  origin: {list(SUPPORTED_PROTOCOL_VERSIONS)}\n"
        f"  edge:   {ts_versions}\n"
        "The canonical host would negotiate differently from the origin.")


def test_edge_snapshot_default_matches_the_origin_preference():
    """The compiled-in snapshot is the THIRD copy of this value.

    The edge answers `initialize` from this file for any caller whose offer it
    cannot match, so the snapshot IS the default - and the default must be the
    origin's own preference or the two hosts disagree about what we prefer.

    It drifted the moment negotiation shipped: the refresher probed the origin
    with a hardcoded "2025-06-18", the newly-negotiating origin honoured that
    exact request, and the answer was written back as though it were the default.
    A refresher that asks a leading question records the answer it asked for.
    """
    import json
    import os

    snap = os.path.join(os.path.dirname(__file__), "..", "..",
                        "edge", "src", "snapshots", "mcp-initialize.json")
    snap = os.path.abspath(snap)
    assert os.path.exists(snap), f"snapshot not found at {snap}"
    with open(snap, encoding="utf-8") as fh:
        doc = json.load(fh)

    got = doc.get("result", {}).get("protocolVersion")
    assert got == PROTOCOL_VERSION, (
        f"edge snapshot defaults to {got}, origin prefers {PROTOCOL_VERSION}. "
        "Re-run scripts/refresh_edge_snapshots.py and redeploy the worker.")
