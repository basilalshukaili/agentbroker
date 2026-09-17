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


def test_committed_snapshot_matches_the_exact_served_tool_builder():
    sys.path.insert(0, AB)
    try:
        from agent_interface.mcp_server import _build_tool_list
    finally:
        sys.path.pop(0)

    expected = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": _build_tool_list()},
    }
    path = os.path.join(
        AB, "edge", "src", "snapshots", "mcp-tools-list.json"
    )
    with open(path, encoding="utf-8") as fh:
        committed = json.load(fh)

    assert refresh.build_local_tools_snapshot() == expected
    assert committed == expected


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
