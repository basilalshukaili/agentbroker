"""The edge tools snapshot can be reproduced without a deployed origin."""
from __future__ import annotations

import json
import os
import sys


AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import refresh_edge_snapshots as refresh  # noqa: E402


def _write(path, doc) -> None:
    path.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_the_local_builder_is_reproducible_and_offline():
    """`--local-tools` really does rebuild tools/list from this checkout."""
    sys.path.insert(0, AB)
    try:
        from agent_interface.mcp_server import _build_tool_list
    finally:
        sys.path.pop(0)

    assert refresh.build_local_tools_snapshot() == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": _build_tool_list()},
    }


def test_committed_snapshot_serves_the_same_tools_as_this_checkout():
    """The committed snapshot tracks the DEPLOYED ORIGIN, not this checkout.

    This test used to assert full equality with ``_build_tool_list()``, and
    that assertion is how the 2026-09-20 incident was manufactured: a commit
    regenerated the snapshot from local source that was AHEAD of the deployed
    origin, and the monitor then read the resulting file difference out as a
    production fault.

    The edge worker answers discovery for the origin but forwards `tools/call`
    TO the origin. A snapshot built from undeployed code therefore advertises
    parameters the origin would reject and hides parameters it accepts - it is
    strictly more dangerous than one that lags the checkout. So the snapshot's
    source of truth is the origin, and equality with local code is NOT an
    invariant; it holds only while the origin is deployed from HEAD.

    What IS an invariant is the tool ROSTER. Adding or removing a tool in this
    checkout without deploying the origin and refreshing would have the
    canonical host advertising a tool nothing can execute, or hiding one that
    exists. Parameter- and description-level differences are the expected,
    benign consequence of undeployed work and are reported - as repo drift,
    never as an outage - by:

        python scripts/refresh_edge_snapshots.py --local-tools --check
    """
    sys.path.insert(0, AB)
    try:
        from agent_interface.mcp_server import _build_tool_list
    finally:
        sys.path.pop(0)

    path = os.path.join(AB, "edge", "src", "snapshots", "mcp-tools-list.json")
    with open(path, encoding="utf-8") as fh:
        committed = json.load(fh)

    local_names = sorted(t["name"] for t in _build_tool_list())
    committed_names = sorted(
        t["name"] for t in committed["result"]["tools"])
    assert committed_names == local_names, (
        "the committed edge snapshot and this checkout disagree about WHICH "
        "tools exist. Deploy the origin, then run "
        "`python scripts/refresh_edge_snapshots.py` to recapture it from the "
        "origin - never from local source.")


def test_local_check_never_calls_the_live_fetcher(monkeypatch, tmp_path):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    _write(
        snapshots / "mcp-tools-list.json",
        refresh.build_local_tools_snapshot(),
    )
    monkeypatch.setattr(refresh, "SNAP", str(snapshots))

    def network_was_called():
        raise AssertionError("offline tools refresh called the live fetcher")

    monkeypatch.setattr(refresh, "fetch_all", network_was_called)
    assert refresh.main(["--local-tools", "--check"]) == 0


def test_local_check_reports_drift_and_write_repairs_it(monkeypatch, tmp_path):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    expected = refresh.build_local_tools_snapshot()
    stale = json.loads(json.dumps(expected))
    stale["result"]["tools"][0]["description"] += " stale"
    path = snapshots / "mcp-tools-list.json"
    _write(path, stale)
    monkeypatch.setattr(refresh, "SNAP", str(snapshots))

    assert refresh.main(["--local-tools", "--check"]) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == stale

    assert refresh.main(["--local-tools"]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == expected


def test_local_routes_rebuild_the_mcp_descriptor_without_the_origin(monkeypatch):
    """`--local-routes mcp.json` answers /.well-known/mcp.json in process. It exists because that snapshot had
    to change with the code (paid_tools used to name the three quota-free tools as paid) and the only other
    way to refresh it was to fetch the OLD origin, which would re-commit the contradiction."""
    def network_was_called():
        raise AssertionError("offline route refresh called the live fetcher")

    monkeypatch.setattr(refresh, "fetch_all", network_was_called)
    doc = refresh.build_local_route_snapshots(["mcp.json"])["mcp.json"]
    pay = doc["payments"]
    assert not set(pay["paid_tools"]) & set(pay["quota_free_tools"])
    assert refresh.build_local_route_snapshots(["mcp.json"])["mcp.json"] == doc, "reproducible"


def test_the_committed_mcp_descriptor_snapshot_is_not_stale():
    assert refresh.main(["--local-routes", "mcp.json", "--check"]) == 0


def test_an_unknown_snapshot_name_fails_closed():
    import pytest
    with pytest.raises(RuntimeError):
        refresh.build_local_route_snapshots(["not-a-snapshot.json"])
