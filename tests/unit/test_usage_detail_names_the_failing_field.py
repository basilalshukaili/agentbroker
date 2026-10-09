"""The outcome log says WHICH argument or header a refused request got wrong, and whether it came through the edge.

THE GAP (found writing docs/reviews/2026-10-09-agentbroker-request-analysis.md): usage_events recorded that 912
tool calls were refused as `missing_argument` / `invalid_argument`, but not which argument. The analysis could only
infer "missing" fields by comparing the argument NAMES against the schema, and could not tell an `invalid_argument`
at all; 197 `header_mismatch` answers on `server/discover` (about 40 a day, still arriving) could not be split
into "the header was absent" and "the header disagreed with the body" - the one distinction that decides whether
the server is too strict or the client is wrong. And half of all /mcp requests arrive through the Cloudflare edge,
where `ip_hash` is the edge's address, not the caller's, so "distinct callers" cannot be counted; nothing on the row
said which half a row belonged to.

THE CHANGE (usage_events.detail only, still at most 64 characters, still our own vocabulary, never caller text):
  f=<declared argument names>   only names the tool's own inputSchema declares, at most six
  why=<closed vocabulary>       only for a refused 2026-07-28 envelope: hdr-meth-miss, hdr-meth-diff, ...
  via=edge                      when the request carries the edge worker's marker header - the caller's own claim:
                                the header is a fixed value any client can send, so this is a label, not proof of
                                the path (the attribution fix needs an authenticated marker; see the analysis)

The legacy rows keep exactly their old text (tests/unit/test_door_instrumentation.py,
test_mcp_2026_07_28.py pin those).
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface import mcp_server
from agent_interface.mcp_server import handle_mcp_request
from billing import usage_logger as ul

M = "io.modelcontextprotocol/"
V = "2026-07-28"
HEADERS = {"user-agent": "pytest-client/1"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def events(monkeypatch):
    got = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: got.append(e))
    return got


def _call(name, arguments, headers=None, profile=None):
    return _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers=headers or HEADERS, profile=profile))


def _tokens(event):
    return (event.detail or "").split()


# --------------------------------------------------------------------------- the failing argument

def test_a_missing_required_argument_is_named(events):
    _call("get_status", {})
    e = events[-1]
    assert (e.outcome, e.error_code) == ("rpc_error", "missing_argument")
    assert "f=operation_id" in _tokens(e)


def test_a_wrong_type_names_the_argument(events):
    _call("screen_sanctions", {"name": 12345})
    e = events[-1]
    assert e.error_code == "invalid_argument"
    assert "f=name" in _tokens(e)


def test_find_business_names_the_missing_place_then_the_missing_kind(events):
    _call("find_business", {})
    assert (events[-1].outcome, events[-1].error_code) == ("tool_error", "missing_argument")
    assert "f=location" in _tokens(events[-1])
    _call("find_business", {"location": "Muscat, Oman"})
    assert "f=capability" in _tokens(events[-1])


def test_find_business_names_a_bad_max_results(events):
    _call("find_business", {"location": "Muscat, Oman", "capability": "dentist", "max_results": [1]})
    assert events[-1].error_code == "invalid_argument"
    assert "f=max_results" in _tokens(events[-1])


def test_only_names_the_tool_declares_are_ever_recorded():
    f = mcp_server._safe_fields
    assert f("get_status", ["operation_id"]) == ["operation_id"]
    assert f("get_status", ["<script>alert(1)</script>", "operation_id", "x" * 200]) == ["operation_id"]
    assert f("capture_lead", ["prospect.name", "prospect.email", "smb_id"]) == ["prospect", "smb_id"]
    assert f("capture_lead", ["parties[3]"]) == []
    assert f("not_a_tool", ["operation_id"]) == []
    assert f(None, ["operation_id"]) == []
    assert len(f("find_business", ["vertical", "location", "capability", "price_band", "availability_window",
                                    "max_results", "city", "region"])) <= 6


def test_a_success_and_an_unrelated_failure_gain_no_field(events):
    _call("check_quota", {})
    assert events[-1].outcome == "ok" and "f=" not in (events[-1].detail or "")
    _call("no_such_tool_at_all", {})
    assert events[-1].error_code == "unknown_tool" and "f=" not in (events[-1].detail or "")


def test_the_detail_stays_within_64_characters_and_never_cuts_a_token():
    class Obs:
        era_version = "2026-07-28"
        reason = "hdr-meth-diff"
        fields = ["location", "capability", "max_results", "price_band"]
        headers = {"x-edge-source": "cloudflare-workers"}
    text = mcp_server._event_detail("retired:data-enrichment", Obs())
    assert len(text) <= 64
    for token in text.split():
        assert token.split("=")[0] in {"door", "pv", "via", "why", "f"}, text
        assert not token.endswith("="), text
    assert text.startswith("door=retired:data-enrichment pv=2026-07-28 why=hdr-meth-diff"), text


# --------------------------------------------------------------------------- the edge marker

def test_the_edge_workers_marker_is_recorded_and_nothing_else_is(events):
    """Recorded as a label. It is unauthenticated (any client may send this header), which is why the row says only
    that the request carried the marker and nothing downstream may treat `via=edge` as a verified path."""
    _call("check_quota", {}, headers={**HEADERS, "x-edge-source": "cloudflare-workers"})
    assert "via=edge" in _tokens(events[-1])
    _call("check_quota", {}, headers={**HEADERS, "x-edge-source": "somebody-else"})
    assert "via=edge" not in _tokens(events[-1])
    _call("check_quota", {}, headers=HEADERS)
    assert "via=edge" not in _tokens(events[-1])


# --------------------------------------------------------------------------- why a modern request was refused

def _modern(method, params=None, *, headers=None, meta=None):
    p = dict(params or {})
    p["_meta"] = meta if meta is not None else {M + "protocolVersion": V, M + "clientCapabilities": {},
                                                 M + "clientInfo": {"name": "pytest-modern", "version": "1"}}
    h = {**HEADERS, "mcp-protocol-version": V, "mcp-method": method}
    h.update(headers or {})
    return _run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": p}, headers=h))


def _without(name):
    h = {**HEADERS, "mcp-protocol-version": V, "mcp-method": "server/discover"}
    h.pop(name)
    return h


@pytest.mark.parametrize("drop,expected", [("mcp-method", "hdr-meth-miss"),
                                           ("mcp-protocol-version", "hdr-ver-miss")])
def test_a_missing_mirror_header_is_told_apart_from_a_wrong_one(events, drop, expected):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "server/discover",
               "params": {"_meta": {M + "protocolVersion": V, M + "clientCapabilities": {}}}}
    r = _run(handle_mcp_request(payload, headers=_without(drop)))
    assert r["error"]["code"] == -32020
    e = events[-1]
    assert e.error_code == "header_mismatch"
    assert f"why={expected}" in _tokens(e)


def test_a_header_that_disagrees_with_the_body_is_told_apart_from_an_absent_one(events):
    r = _modern("server/discover", headers={"mcp-method": "tools/list"})
    assert r["error"]["code"] == -32020
    assert "why=hdr-meth-diff" in _tokens(events[-1])
    r = _modern("server/discover", headers={"mcp-protocol-version": "2025-11-25"})
    assert r["error"]["code"] == -32020
    assert "why=hdr-ver-diff" in _tokens(events[-1])


def test_the_tool_name_header_cases(events):
    _modern("tools/call", {"name": "check_quota", "arguments": {}})
    assert "why=hdr-name-miss" in _tokens(events[-1])
    _modern("tools/call", {"name": "check_quota", "arguments": {}}, headers={"mcp-name": "get_status"})
    assert "why=hdr-name-diff" in _tokens(events[-1])
    _modern("tools/call", {"name": "check_quota", "arguments": {}}, headers={"mcp-name": "=?base64?!!!?="})
    assert "why=hdr-name-bad" in _tokens(events[-1])


def test_an_envelope_with_no_capabilities_or_a_bad_version_is_tagged(events):
    _modern("tools/list", meta={M + "protocolVersion": V})
    assert events[-1].error_code == "invalid_meta" and "why=meta-caps-miss" in _tokens(events[-1])
    _modern("tools/list", meta={M + "protocolVersion": 7})
    assert events[-1].error_code == "invalid_meta" and "why=meta-ver-bad" in _tokens(events[-1])


def test_nothing_the_caller_sent_reaches_the_detail(events):
    hostile = "<script>alert(1)</script>" + "A" * 300
    _modern("server/discover", headers={"mcp-method": hostile})
    detail = events[-1].detail or ""
    assert "<" not in detail and "AAAA" not in detail and len(detail) <= 64
    assert "why=hdr-meth-diff" in detail.split()
