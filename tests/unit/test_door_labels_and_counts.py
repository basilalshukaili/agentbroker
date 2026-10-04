"""The three facts every usage row now carries about HOW the caller arrived: door, protocol version, result count.

Verdict item A7 (docs/reviews/2026-10-03-mcp-focus-verdict.md): "without it we cannot see the first buyer".
Before this, the door lived in free text (`detail = 'door=sanctions-screening'`) for the five capability doors
only, the protocol version in `detail` for 2026-07-28 requests only, and the number of results nowhere.

These are the pure functions that produce those values. Every one of them takes input a stranger controls (a
URL path, a header, a tool result) and returns something that is either one of OUR OWN labels, a number, or
None - never caller text. The database function refuses anything else, so a value that escaped from here would
lose the whole row; the last test in this file pins that the two agree.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_interface import door_label, profiles, request_observer as ro, retired_doors

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "spine" / "013_usage_events_door_columns.sql"


# ---------------------------------------------------------------------------
# door labels
# ---------------------------------------------------------------------------

def test_the_full_server_is_one_door_however_it_is_spelled():
    assert door_label.for_profile(None) == "agent-broker"
    assert door_label.for_profile("agent-broker") == "agent-broker"
    for path in ("/mcp", "/mcp/", "/mcp/agent-broker", "/mcp/agent-broker/"):
        assert door_label.for_path(path) == "agent-broker", path


@pytest.mark.parametrize("name", sorted(profiles.PROFILES))
def test_every_capability_door_is_labelled_by_its_own_name(name):
    assert door_label.for_profile(name) == name
    assert door_label.for_path(f"/mcp/{name}") == name
    assert door_label.for_path(f"/mcp/{name}/") == name


@pytest.mark.parametrize("slug", sorted(retired_doors.RETIRED_DOORS))
def test_a_retired_door_is_labelled_retired_so_it_can_never_be_counted_as_a_live_one(slug):
    assert door_label.for_retired(slug) == f"retired:{slug}"
    assert door_label.for_path(f"/mcp/{slug}") == f"retired:{slug}"
    assert door_label.for_path(f"/mcp/{slug}/") == f"retired:{slug}"
    assert door_label.for_path(f"/mcp/{slug}/mcp") == f"retired:{slug}"
    assert door_label.for_profile(slug) == f"retired:{slug}"


def test_a_path_under_mcp_that_is_not_a_door_is_unknown_and_never_echoed():
    for path in ("/mcp/not-a-door", "/mcp/EVIL<script>", "/mcp/a/b/c", "/mcp/sanctions-screening/other",
                 "/mcp/eyJ" + "a" * 120, "/mcp/" + "0123456789abcdef" * 4, "/mcp/with space"):
        assert door_label.for_path(path) == "unknown", path
    assert door_label.for_profile("anything-else") == "unknown"
    assert door_label.for_profile("EVIL<script>") == "unknown"


def test_a_path_that_is_not_an_mcp_door_has_no_door():
    for path in ("/health", "/", "/ops/find_business", "/mcpx", "/mcp-x/anything", "", None, 5, b"/mcp"):
        assert door_label.for_path(path) is None, path


def test_every_label_is_one_the_database_accepts():
    pattern = re.compile(door_label.DOOR_PATTERN)
    labels = {door_label.for_profile(None), door_label.UNKNOWN_DOOR}
    labels |= set(profiles.PROFILES) | {door_label.for_retired(s) for s in retired_doors.RETIRED_DOORS}
    for label in labels:
        assert pattern.fullmatch(label), label
    assert not pattern.fullmatch("Has Capitals"), "a label is lower-case identifier text only"
    assert not pattern.fullmatch("x" * 80), "and bounded"
    assert not pattern.fullmatch(""), "and never empty"


def test_the_sql_and_the_python_agree_on_what_a_door_looks_like():
    sql = MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"p_door\s+!~\s+'([^']+)'", sql)
    assert m, "013 must validate p_door against a pattern"
    # SQL ARE and Python re share this syntax; the pattern is written to be identical in both.
    assert m.group(1) == door_label.DOOR_PATTERN


# ---------------------------------------------------------------------------
# protocol version: only ever one of OUR versions
# ---------------------------------------------------------------------------

KNOWN = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def test_a_supported_protocol_version_passes_through_and_nothing_else_does():
    for v in KNOWN:
        assert ro.safe_protocol_version(v, KNOWN) == v
        assert ro.safe_protocol_version(f"  {v}  ", KNOWN) == v
    for bad in ("2030-01-01", "1999-12-31", "", " ", None, 20250618, ["2025-06-18"], "2025-06-18\n2026-07-28",
                "2025-06-18; drop table usage_events", "x" * 500):
        assert ro.safe_protocol_version(bad, KNOWN) is None, bad


# ---------------------------------------------------------------------------
# result_count: how many things the call handed back, only when that has a plain meaning
# ---------------------------------------------------------------------------

def _tool_reply(payload, *, is_error=False):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}}


def test_a_list_method_counts_what_it_listed():
    assert ro.result_count_of("tools/list", None, {"result": {"tools": [{}, {}, {}]}}) == 3
    assert ro.result_count_of("tools/list", None, {"result": {"tools": []}}) == 0
    assert ro.result_count_of("resources/list", None, {"result": {"resources": [{}] * 5}}) == 5
    assert ro.result_count_of("resources/templates/list", None, {"result": {"resourceTemplates": []}}) == 0
    assert ro.result_count_of("prompts/list", None, {"result": {"prompts": [{}] * 2}}) == 2


def test_find_business_reports_its_own_count_and_falls_back_to_counting_the_list():
    assert ro.result_count_of("tools/call", "find_business", _tool_reply(
        {"status": "success", "result": {"businesses": [{}] * 7, "result_count": 4}})) == 4
    assert ro.result_count_of("tools/call", "find_business", _tool_reply(
        {"status": "success", "result": {"businesses": [{}] * 6}})) == 6
    assert ro.result_count_of("tools/call", "find_business", _tool_reply(
        {"status": "success", "result": {"businesses": [], "result_count": 0}})) == 0


def test_the_other_list_returning_tools_count_their_principal_list():
    assert ro.result_count_of("tools/call", "screen_sanctions", _tool_reply(
        {"status": "success", "result": {"matched": True, "matches": [{}, {}]}})) == 2
    assert ro.result_count_of("tools/call", "screen_sanctions", _tool_reply(
        {"status": "success", "result": {"matched": False, "matches": []}})) == 0
    assert ro.result_count_of("tools/call", "map_trade_restriction", _tool_reply(
        {"status": "success", "result": {"restrictions": [{}] * 3}})) == 3
    assert ro.result_count_of("tools/call", "lookup_us_contracts", _tool_reply(
        {"status": "success", "result": {"awards": [{}] * 9}})) == 9


def test_a_call_that_failed_or_has_no_list_has_no_count():
    assert ro.result_count_of("tools/call", "find_business", _tool_reply(
        {"status": "failure", "result": {"businesses": [{}]}}, is_error=True)) is None
    assert ro.result_count_of("tools/call", "send_message", _tool_reply({"status": "success", "result": {}})) is None
    assert ro.result_count_of("tools/call", "get_status", _tool_reply({"status": "success"})) is None
    assert ro.result_count_of("tools/call", None, _tool_reply({"result": {"businesses": [{}]}})) is None
    assert ro.result_count_of("initialize", None, {"result": {"protocolVersion": "2025-06-18"}}) is None
    assert ro.result_count_of("ping", None, {"result": {}}) is None


def test_a_protocol_error_or_a_notification_has_no_count():
    assert ro.result_count_of("tools/list", None, {"error": {"code": -32601, "message": "x"}}) is None
    assert ro.result_count_of("tools/call", "find_business", {"error": {"code": -32602, "message": "x"}}) is None
    assert ro.result_count_of("tools/list", None, None) is None
    assert ro.result_count_of(None, None, {"result": {"tools": [{}]}}) is None


def test_a_hostile_or_malformed_result_never_raises_and_never_invents_a_number():
    for response in (
        {"result": "not a dict"}, {"result": {"content": "x"}}, {"result": {"content": []}},
        {"result": {"content": [None]}}, {"result": {"content": [{"type": "text"}]}},
        {"result": {"content": [{"type": "text", "text": "{not json"}]}},
        {"result": {"content": [{"type": "text", "text": json.dumps([1, 2, 3])}]}},
        {"result": {"content": [{"type": "text", "text": json.dumps({"result": "x"})}]}},
        {"result": {"content": [{"type": "text", "text": json.dumps({"result": {"businesses": "many"}})}]}},
        {"result": {"tools": "many"}}, {"result": None}, {}, "x", 5, [],
    ):
        for method, tool in (("tools/call", "find_business"), ("tools/list", None)):
            assert ro.result_count_of(method, tool, response) is None, (method, response)


def test_a_count_is_a_plain_non_negative_integer_that_fits_the_column():
    def reply(n):
        return _tool_reply({"status": "success", "result": {"businesses": [], "result_count": n}})
    # a bool is an int in Python; True must not become 1. A negative or absurd figure is not a count.
    for bad in (True, False, -1, 1e999, float("nan"), "3", None, 10 ** 12, 2.5):
        got = ro.result_count_of("tools/call", "find_business", reply(bad))
        assert got in (None, 0), (bad, got)      # None, or the honest fallback of counting the (empty) list
    assert ro.result_count_of("tools/call", "find_business", reply(0)) == 0
    assert ro.result_count_of("tools/call", "find_business", reply(1000000)) == 1000000


def test_a_very_large_result_text_is_not_parsed_just_to_count_it():
    big = json.dumps({"status": "success", "result": {"businesses": [{"name": "x" * 200}] * 5000,
                                                      "result_count": 5000}})
    assert len(big) > 512 * 1024
    assert ro.result_count_of("tools/call", "find_business", {"result": {"content": [
        {"type": "text", "text": big}]}}) is None


def test_a_declaration_in_the_body_is_authoritative_over_the_header():
    meta = "io.modelcontextprotocol/protocolVersion"
    # the body names a version we do not speak: the request was judged on that, so the header is not consulted
    assert ro.declared_protocol_version("tools/list", {"_meta": {meta: "2027-01-01"}},
                                        {"mcp-protocol-version": "2025-06-18"}, KNOWN) is None
    assert ro.declared_protocol_version("tools/list", {"_meta": {meta: "2025-11-25"}},
                                        {"mcp-protocol-version": "2025-06-18"}, KNOWN) == "2025-11-25"
    assert ro.declared_protocol_version("tools/list", {}, {"mcp-protocol-version": "2025-06-18"}, KNOWN) == "2025-06-18"
    assert ro.declared_protocol_version("tools/list", {}, {}, KNOWN) is None


def test_initialize_records_what_the_server_negotiated_not_what_the_caller_asked_for():
    assert ro.declared_protocol_version("initialize", {"protocolVersion": "2030-01-01"}, {}, KNOWN,
                                        reply={"result": {"protocolVersion": "2025-11-25"}}) == "2025-11-25"
    assert ro.declared_protocol_version("initialize", {"protocolVersion": "2025-03-26"}, {}, KNOWN,
                                        reply={"result": {"protocolVersion": "evil"}}) is None
    assert ro.declared_protocol_version("initialize", {}, {"mcp-protocol-version": "2025-06-18"}, KNOWN,
                                        reply=None) == "2025-06-18", "no reply to read: the header is all there is"


def test_a_hostile_request_never_raises_and_never_invents_a_version():
    for hostile in (None, "x", 5, [], {"_meta": "x"}, {"_meta": []}):
        assert ro.declared_protocol_version("tools/list", hostile, hostile, KNOWN) is None
        assert ro.declared_protocol_version("initialize", hostile, hostile, KNOWN, reply=hostile) is None
