"""scripts/live_verify_discovery.py: the verifier must fail a WRONG system and must not fail a CORRECT one.

Two promises of that script are pinned here, both found by review of the discovery-hygiene branch (2026-10-04):

  * the x402 check compares what a scanner would act on. Two 404s are the same answer whatever their bodies say (a 404
    carries no claim), and bytes are compared only where there is a document to compare. A check that failed a correct
    system right after the integration it was written for would be a check nobody trusts.
  * "a check that cannot run is a failed check, with its reason": an unreachable host is a printed FAIL and a written
    receipt, never a traceback that leaves no receipt behind.

The network is never touched: `http` is replaced, or `urlopen` is made to refuse.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import urllib.error

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "live_verify_discovery.py"


@pytest.fixture
def lv():
    spec = importlib.util.spec_from_file_location("live_verify_discovery_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CTX = {"base": "https://api.example.test", "site": "https://site.example.test"}


def _serve(monkeypatch, lv, answers: dict):
    """Replace the network: answers maps a path to (status, bytes); the same on both hosts unless a (host, path) key."""
    def fake(method, url, **_kw):
        host, _, path = url.partition("//")[2].partition("/")
        path = "/" + path
        status, raw = answers.get((host, path), answers.get(path, (404, b'{"detail":"Not Found"}')))
        return status, {"content-type": "application/json"}, raw.decode(), raw
    monkeypatch.setattr(lv, "http", fake)


DOC = b'{"x402Version":2}'


@pytest.mark.parametrize("name,answers,expected", [
    ("both 404 with the same body", {"/.well-known/x402": (404, b'{"detail":"a"}'),
                                     "/.well-known/x402.json": (404, b'{"detail":"a"}')}, True),
    ("both 404 with DIFFERENT bodies (a 404 carries no claim)",
     {"/.well-known/x402": (404, b'{"detail":"x402 is not enabled on this host"}'),
      "/.well-known/x402.json": (404, b'{"detail":"Not Found"}')}, True),
    ("both 200 and byte-identical", {"/.well-known/x402": (200, DOC), "/.well-known/x402.json": (200, DOC)}, True),
    ("both 200 and the bytes differ", {"/.well-known/x402": (200, DOC),
                                       "/.well-known/x402.json": (200, b'{"x402Version":1}')}, False),
    ("the alias is a 200 where the primary is a 404", {"/.well-known/x402": (404, b"{}"),
                                                       "/.well-known/x402.json": (200, DOC)}, False),
    ("the alias is a 404 where the primary is a 200", {"/.well-known/x402": (200, DOC),
                                                       "/.well-known/x402.json": (404, b"{}")}, False),
    ("a 500 is never fine", {"/.well-known/x402": (500, b"x"), "/.well-known/x402.json": (500, b"x")}, False),
])
def test_the_x402_check_compares_what_a_scanner_acts_on(lv, monkeypatch, name, answers, expected):
    _serve(monkeypatch, lv, answers)
    assert lv.check_x402(dict(CTX))["ok"] is expected, name


def test_an_unreachable_host_is_a_failed_check_with_a_receipt_not_a_traceback(lv, monkeypatch, tmp_path, capsys):
    def refuse(*_a, **_k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(lv.urllib.request, "urlopen", refuse)
    out = tmp_path / "receipt.json"
    code = lv.main(["--base", "http://127.0.0.1:9", "--site", "http://127.0.0.1:9", "--only", "glama", "--out", str(out)])
    assert code == 1
    assert "[FAIL] glama" in capsys.readouterr().out
    receipt = json.loads(out.read_text("utf-8"))
    assert receipt["all_passed"] is False
    assert receipt["results"]["glama"]["ok"] is False
    assert "URLError" in receipt["results"]["glama"]["error"], "the reason a check could not run is recorded"
    assert receipt["doors"] == [], "no doors could be read, and the receipt says so rather than guessing"
    assert "URLError" in receipt.get("doors_error", "")


def test_a_reachable_host_still_reads_its_doors(lv, monkeypatch, tmp_path):
    mcp_json = json.dumps({"capability_endpoints": [{"name": "b-door"}, {"name": "a-door"}]}).encode()
    _serve(monkeypatch, lv, {"/.well-known/mcp.json": (200, mcp_json)})
    out = tmp_path / "receipt.json"
    lv.main(["--base", CTX["base"], "--site", CTX["site"], "--only", "not_implemented", "--out", str(out)])
    receipt = json.loads(out.read_text("utf-8"))
    assert receipt["doors"] == ["a-door", "b-door"] and "doors_error" not in receipt
    assert receipt["results"]["not_implemented"]["ok"] is True
