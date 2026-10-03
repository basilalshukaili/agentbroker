"""The post-deploy probe (scripts/probe_mcp_2026.py) runs its eight checks against the app in-process.

Two jobs: prove the probe itself works (a probe that cannot fail is worse than none), and keep it in step with
the server - if the server's behaviour changes, this fails here instead of on the first deploy that uses it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from billing import usage_logger as ul

ROOT = Path(__file__).resolve().parents[2]


def _probe():
    spec = importlib.util.spec_from_file_location("probe_mcp_2026", ROOT / "scripts" / "probe_mcp_2026.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: None)
    from fastapi.testclient import TestClient
    import main
    main._rl_buckets.clear()
    yield TestClient(main.app, raise_server_exceptions=False)
    main._rl_buckets.clear()


def _sender(client):
    def send(url, headers, body):
        r = client.post(url, json=body, headers=headers)
        return r.status_code, (r.json() if r.content else None)
    return send


def test_all_eight_checks_pass_against_the_app(app_client):
    probe = _probe()
    results = probe.run_checks(_sender(app_client), "/mcp", "/mcp")
    assert len(results) == 8
    assert [n for n, ok, d in results if not ok] == [], results


def test_the_probe_fails_when_the_server_does_not_speak_the_revision(app_client):
    """Point it at a sender that answers like the pre-change server (-32601 for discover): it must say FAIL."""
    probe = _probe()

    def old_server(url, headers, body):
        if body["method"] in ("server/discover", "resources/templates/list"):
            return 200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "nope"}}
        return _sender(app_client)(url, {k: v for k, v in headers.items() if k.lower() != "mcp-protocol-version"}, body)

    results = probe.run_checks(old_server, "/mcp", "/mcp")
    failed = {n for n, ok, d in results if not ok}
    assert any("server/discover" in n for n in failed)
    assert any("templates" in n for n in failed)
    assert len(failed) >= 4


def test_a_probe_check_that_raises_is_reported_not_propagated():
    probe = _probe()

    def broken(url, headers, body):
        raise ConnectionError("down")

    results = probe.run_checks(broken, "http://x/mcp", "http://x/mcp")
    assert len(results) == 8 and not any(ok for _, ok, _ in results)
    assert all("ConnectionError" in d for _, _, d in results)


def test_the_probe_never_places_a_tool_call_or_sends_a_message(app_client):
    seen = []
    send = _sender(app_client)

    def spy(url, headers, body):
        seen.append(body["method"])
        return send(url, headers, body)

    _probe().run_checks(spy, "/mcp", "/mcp")
    assert "tools/call" not in seen
    assert set(seen) <= {"server/discover", "tools/list", "resources/templates/list", "initialize", "no/such/method"}


def test_the_probe_identifies_itself_as_our_own_infrastructure():
    assert _probe().UA.startswith("hatchloop-")
