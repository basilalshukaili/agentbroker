"""The retired Render address redirects every request to the live server, method and body intact.

Stale directory listings still send POST /mcp to smb-broker.onrender.com. A 301/302 would let a
client re-send it as GET and the call would quietly fail; 308 repeats the request unchanged.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "deploy" / "render_redirect" / "redirect_server.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("render_redirect_server", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def port(mod):
    srv = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Redirect)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _req(port, method, target, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, target, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data)
    c.close()
    return out


LIVE = "https://api.hatchloop.dev"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_every_method_gets_308_to_the_same_path(port, method):
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}' if method in ("POST", "PUT", "PATCH") else None
    st, h, _ = _req(port, method, "/mcp", body=body, headers={"Content-Type": "application/json"})
    assert st == 308
    assert h["location"] == LIVE + "/mcp"


def test_path_and_query_are_kept_exactly(port):
    st, h, _ = _req(port, "GET", "/mcp/sanctions-screening/?a=1&b=%20x&b=2")
    assert st == 308
    assert h["location"] == LIVE + "/mcp/sanctions-screening/?a=1&b=%20x&b=2"


def test_root_and_odd_paths(port):
    assert _req(port, "GET", "/")[1]["location"] == LIVE + "/"
    assert _req(port, "GET", "/.well-known/mcp.json")[1]["location"] == LIVE + "/.well-known/mcp.json"


def test_a_double_slash_cannot_be_used_to_leave_our_host(port):
    st, h, _ = _req(port, "GET", "//evil.example/x")
    assert st == 308
    # the stdlib server collapses a leading "//" to "/" before we see it; either way the host is ours
    from urllib.parse import urlsplit
    assert urlsplit(h["location"]).hostname == "api.hatchloop.dev"
    assert h["location"].startswith(LIVE + "/")


def test_absolute_form_target_keeps_only_the_path(mod):
    assert mod.location_for("http://evil.example/mcp?x=1") == LIVE + "/mcp?x=1"
    assert mod.location_for("") == LIVE + "/"
    assert mod.location_for("*") == LIVE + "/*"


def test_control_characters_never_reach_the_location_header(mod):
    loc = mod.location_for("/a\r\nSet-Cookie: x=1")
    assert "\r" not in loc and "\n" not in loc


def test_health_answers_200_with_a_pointer(port):
    st, h, data = _req(port, "GET", "/health")
    assert st == 200 and h["content-type"].startswith("application/json")
    j = json.loads(data)
    assert j["live"] == LIVE and j["mcp"] == LIVE + "/mcp"
    assert _req(port, "HEAD", "/health")[0] == 200


def test_options_is_answered_not_redirected(port):
    st, h, _ = _req(port, "OPTIONS", "/mcp", headers={"Access-Control-Request-Headers": "x-agent-identity"})
    assert st == 204
    assert h["access-control-allow-origin"] == "*"
    assert h["access-control-allow-headers"] == "x-agent-identity"


def test_an_unread_post_body_does_not_poison_the_next_request(port):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/mcp", body=b"x" * 5000, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    assert r.status == 308
    assert (r.getheader("Connection") or "").lower() == "close"
    c.close()


def test_the_query_string_is_never_printed(port, capsys):
    _req(port, "GET", "/health?token=SECRETVALUE123")
    _req(port, "GET", "/mcp?token=SECRETVALUE123")
    out = capsys.readouterr().out
    assert "GET /mcp" in out
    assert "SECRETVALUE123" not in out
