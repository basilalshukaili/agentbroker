"""A failing or wedged external client must never turn interoperability green."""
from __future__ import annotations

import sys
import asyncio

import pytest

from tests import oauth_sdk_bridge as bridge


def client_script(tmp_path, monkeypatch, source):
    path = tmp_path / "client.py"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(bridge, "CLIENT", path)
    monkeypatch.setenv("MCP_SDK_PYTHON", sys.executable)


def test_a_client_assertion_fails_the_parent_test(tmp_path, monkeypatch):
    client_script(tmp_path, monkeypatch, "raise AssertionError('SDK assertion reached')\n")
    with pytest.raises(AssertionError, match="SDK assertion reached"):
        bridge.run_sdk("http://127.0.0.1:1", "guidance")


def test_a_client_that_exercised_nothing_cannot_pass(tmp_path, monkeypatch):
    client_script(tmp_path, monkeypatch, "pass\n")
    with pytest.raises(AssertionError, match="without exercising"):
        bridge.run_sdk("http://127.0.0.1:1", "guidance")


def test_a_wedged_client_is_killed_and_reaped(tmp_path, monkeypatch):
    client_script(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    real_popen, children = bridge.subprocess.Popen, []

    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(bridge.subprocess, "Popen", start)
    with pytest.raises(TimeoutError, match="deadline"):
        bridge.run_sdk("http://127.0.0.1:1", "guidance", timeout=0.5)
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdin.closed and children[0].stdout.closed


def test_a_stalled_browser_callback_cannot_escape_the_deadline(tmp_path, monkeypatch):
    client_script(tmp_path, monkeypatch,
                  "import json, time\nprint(json.dumps({'event':'redirect','url':'http://127.0.0.1:1'}), flush=True)\ntime.sleep(60)\n")

    class StalledPerson:
        async def redirect_handler(self, url):
            await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        bridge.run_sdk("http://127.0.0.1:1", "register", StalledPerson(), timeout=0.5)


@pytest.mark.parametrize("base", ["https://api.hatchloop.dev", "http://example.org:8080", "https://127.0.0.1:443"])
def test_an_external_or_non_http_target_is_refused_before_starting_a_client(base, monkeypatch):
    def must_not_start(*args, **kwargs):
        pytest.fail("an external target started a client")

    monkeypatch.setattr(bridge.subprocess, "Popen", must_not_start)
    with pytest.raises(AssertionError, match="loopback"):
        bridge.run_sdk(base, "guidance")
