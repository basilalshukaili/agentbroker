"""Replay real and observed client handshakes against the server.

Three fixtures in tests/fixtures/mcp_handshakes (read its README for what each one is and is not):

  * python_sdk_legacy_before_2026-07-28.json  -- a REAL capture: the official MCP Python SDK client
    (mcp 1.26.0) doing initialize -> notifications/initialized -> tools/list -> tools/call -> ping against the
    server AS IT WAS BEFORE the 2026-07-28 work (commit 1f85885). The assertion is exact: what the old
    server answered is what the new server answers. This is the pin on "older clients keep working".
  * modern_2026-07-28_sequence_from_spec.json -- CONSTRUCTED from the specification's wire examples and the
    TypeScript SDK's documented auto-mode flow. It says so in its provenance, because no 2026-07-28
    client was available to capture.
  * scanner_shapes_observed_2026-10-03.json -- the header shapes of the callers that are asking for this
    revision today (from a read-only aggregate of the production access log); bodies reconstructed.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from billing import usage_logger as ul

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "mcp_handshakes"
LEGACY = "python_sdk_legacy_before_2026-07-28.json"
CONSTRUCTED = "modern_2026-07-28_sequence_from_spec.json"
OBSERVED = "scanner_shapes_observed_2026-10-03.json"


def _shape_fn():
    spec = importlib.util.spec_from_file_location("record_mcp_handshake", ROOT / "scripts" / "record_mcp_handshake.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._shape


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cases():
    out = []
    for fname in (LEGACY, CONSTRUCTED, OBSERVED):
        for i, ex in enumerate(_load(fname)["exchanges"], 1):
            label = ex.get("label") or f"{ex['json'].get('method')} #{i}"
            out.append(pytest.param(fname, ex, id=f"{fname.split('_')[0]}:{label[:70]}"))
    return out


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: None)
    from fastapi.testclient import TestClient
    import main
    main._rl_buckets.clear()
    c = TestClient(main.app, raise_server_exceptions=False)
    yield c
    main._rl_buckets.clear()


def _send(client, ex):
    return client.request(ex["method"], ex["path"], headers=ex["headers"], json=ex["json"])


def _body(resp):
    return resp.json() if resp.content else None


@pytest.mark.parametrize("fname,ex", _cases())
def test_replay(client, fname, ex):
    resp = _send(client, ex)

    if "recorded_response" in ex:
        # A real capture: exact shape equality with what the server of the day answered.
        got = _shape_fn()(resp.status_code, resp.content, resp.headers.get("content-type", ""))
        assert got == ex["recorded_response"], (
            "the server now answers a recorded legacy request differently from the server that was "
            "recorded - older clients would see the change")
        return

    exp = ex["expect"]
    assert resp.status_code == exp["status"], resp.text[:300]
    if exp.get("body") == "empty":
        assert resp.content == b""
        return
    doc = _body(resp)
    if "error_code" in exp:
        assert doc["error"]["code"] == exp["error_code"], doc
        data = doc["error"].get("data") or {}
        for v in exp.get("data_supported_includes", []):
            assert v in data["supported"], data
        if "data_requested" in exp:
            assert data["requested"] == exp["data_requested"]
        return
    result = doc["result"]
    for k in exp.get("result_keys_include", []):
        assert k in result, (k, sorted(result))
    for k in exp.get("result_keys_exclude", []):
        assert k not in result, (k, sorted(result))
    for field in ("resultType", "cacheScope", "protocolVersion"):
        if field in exp:
            assert result[field] == exp[field]
    if "serverInfo_name" in exp:
        info = result.get("serverInfo") or result["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert info["name"] == exp["serverInfo_name"]
    if "supported_versions_start_with" in exp:
        assert result["supportedVersions"][0] == exp["supported_versions_start_with"]
    if "min_tools" in exp:
        assert len(result["tools"]) >= exp["min_tools"]


def test_the_legacy_capture_is_real_and_is_of_the_commit_before_this_work():
    prov = _load(LEGACY)["provenance"]
    assert "official Python SDK" in prov["client"]
    assert prov["server_commit_recorded"].startswith("1f85885")
    methods = [e["json"]["method"] for e in _load(LEGACY)["exchanges"]]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "ping"]


def test_a_constructed_fixture_never_passes_itself_off_as_a_capture():
    for fname in (CONSTRUCTED, OBSERVED):
        kind = _load(fname)["provenance"]["kind"]
        assert kind.startswith(("CONSTRUCTED", "OBSERVED")), (fname, kind)
        assert not any("recorded_response" in e for e in _load(fname)["exchanges"]), (
            f"{fname} carries recorded_response, which is reserved for a real capture")


def test_the_scanner_fixture_covers_every_shape_the_access_log_showed():
    labels = " ".join(e["label"] for e in _load(OBSERVED)["exchanges"])
    for needle in ("scanner-A", "scanner-B", "scanner-F", "scanner-I"):
        assert needle in labels
