"""A tool's published contract names every answer its handler gives for an id it does not hold.

THE DEFECT (independent gate, P1, on d4f34c1). Commit 1a876ab made verify_business, capture_lead and
schedule_appointment answer an unknown or `osm:` smb_id with `out_of_supply_network` (a client error, not retriable).
manifest/manifest.json is the one source of tools/list, mcp_tools.json, the /manifest and llms-full.txt documents and
the edge snapshots, and it still said the opposite: verify_business's readiness summary read "An osm:... id returned by
find_business answers supply_unreachable", it is served to every caller in tools/list as `_meta["hatchloop/readiness"]`,
and the three tools' `failure_modes` named `supply_unreachable` (or nothing) but never `out_of_supply_network`. A caller
or a registry reading the published contract would have planned for an outage code the tool no longer returned, and
`supply_unreachable` is the one code reliability/retry_policy retries. The fix to the handler was correct and the
release note's "tools/list is byte-identical" was the symptom, not a safety property.

WHAT THIS PINS, so the contract cannot drift from the handler again:

  * the handlers are asked, for real, what they answer to an unknown id and to an `osm:` id; the answer is the oracle;
  * every published surface that carries a tool's failure modes (the manifest, the generated mcp_tools.json, the
    llms-full.txt rendering, the committed edge snapshots) names that code for that tool;
  * every surface that carries a readiness sentence (tools/list on the full server and on each door, mcp_tools.json,
    the edge snapshots) that speaks of an unknown or `osm:` id names that same code and not the outage code;
  * a tool with an `smb_id` parameter that is NOT in the table below fails the build until someone decides whether it
    resolves ids against the directory, so a fourth tool cannot join the first three unpublished.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent_interface import mcp_server as ms
from agent_interface import profiles
from agent_interface import well_known as wk
from agent_interface.manifest_server import get_full_manifest
from core.capture_lead import handle_capture_lead
from core.models import (AppointmentAction, CaptureLeadRequest, ProspectData, ScheduleAppointmentRequest,
                         VerifyBusinessRequest)
from core.schedule_appointment import handle_schedule_appointment
from core.verify_business import handle_verify_business

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / "edge" / "src" / "snapshots"
OUTAGE = "supply_unreachable"
UNKNOWN_IDS = ("smb_GHOST", "osm:node/1001")


def _run(coro):
    return asyncio.run(coro)


def _verify(smb_id):
    return _run(handle_verify_business(VerifyBusinessRequest(smb_id=smb_id)))


def _capture(smb_id):
    return _run(handle_capture_lead(CaptureLeadRequest(smb_id=smb_id, prospect=ProspectData(name="Ghost"),
                                                       source="t")))


def _schedule(smb_id):
    return _run(handle_schedule_appointment(ScheduleAppointmentRequest(smb_id=smb_id,
                                                                       action=AppointmentAction.BOOK)))


# tool -> a function that calls its real handler with an id. These three resolve `smb_id` against the supply directory.
RESOLVES_IDS = {"verify_business": _verify, "capture_lead": _capture, "schedule_appointment": _schedule}
# Tools that take an `smb_id` and do NOT answer an unknown one from the directory lookup, with the reason; reviewed
# 2026-10-09. escalate_to_human and handle_inbound only record what they are given (core/escalate_to_human.py,
# core/handle_inbound.py never read the directory); call_business treats an unresolvable id as "no callable phone
# number" (bad_input, or channel_unavailable first while voice is off) and has its own contract.
DOES_NOT_RESOLVE_IDS = {"escalate_to_human", "handle_inbound", "call_business"}

OPS = {op["name"]: op for op in get_full_manifest()["operations"]}


def _published_codes(op: dict) -> set:
    """The reason codes a manifest operation's failure_modes name (strings, or {reason_code|code, ...} objects)."""
    out = set()
    for fm in op.get("failure_modes") or []:
        if isinstance(fm, dict):
            out.add(fm.get("reason_code") or fm.get("code"))
        else:
            out.add(fm)
    return out


def _answer(tool: str, smb_id: str) -> str:
    return RESOLVES_IDS[tool](smb_id).reason_code


# ---------------------------------------------------------------------------------------------------------------------
# the oracle: what the handlers really answer
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(RESOLVES_IDS))
@pytest.mark.parametrize("smb_id", UNKNOWN_IDS)
def test_the_handlers_answer_out_of_supply_network_for_an_id_they_do_not_hold(tool, smb_id):
    """If this ever changes, the published tests below change with it - they read the answer, not a constant."""
    assert _answer(tool, smb_id) == "out_of_supply_network"


def test_every_tool_with_an_smb_id_parameter_is_accounted_for():
    with_smb_id = {n for n, op in OPS.items() if "smb_id" in (op.get("input_schema") or {}).get("properties", {})}
    assert with_smb_id == set(RESOLVES_IDS) | DOES_NOT_RESOLVE_IDS, (
        "a tool with an smb_id parameter is not in test_published_failure_modes_match_handlers.py: decide whether "
        "it answers an unknown id from the supply directory (add it to RESOLVES_IDS with a call) or not (add it to "
        "DOES_NOT_RESOLVE_IDS with the reason)")


# ---------------------------------------------------------------------------------------------------------------------
# the published contract
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(RESOLVES_IDS))
def test_the_manifest_names_the_code_the_handler_answers(tool):
    answered = {_answer(tool, i) for i in UNKNOWN_IDS}
    missing = answered - _published_codes(OPS[tool])
    assert not missing, (f"{tool}'s handler answers {sorted(answered)} for an unknown or osm: id but its published "
                         f"failure_modes ({sorted(_published_codes(OPS[tool]))}) omit {sorted(missing)}")


def test_the_outage_code_is_not_published_where_it_can_no_longer_occur():
    """verify_business is a directory lookup that contacts nobody and capture_lead a write to our own funnel: neither
    can fail with `supply_unreachable`, 'the target SMB could not be reached via any available channel'. Publishing it
    would tell a caller to retry something that cannot succeed on retry. schedule_appointment keeps it: the background
    booking worker (reliability/async_runner.py) still answers it when a business leaves the directory between the
    request and the run."""
    assert OUTAGE not in _published_codes(OPS["verify_business"])
    assert OUTAGE not in _published_codes(OPS["capture_lead"])
    assert OUTAGE in _published_codes(OPS["schedule_appointment"])


def test_the_readiness_sentences_that_speak_of_these_ids_name_the_code_the_handler_answers():
    for tool in ("verify_business", "capture_lead"):
        summary = OPS[tool]["readiness"]["summary"]
        assert "osm:" in summary, f"{tool}'s readiness sentence is expected to speak of osm: ids"
        for smb_id in UNKNOWN_IDS:
            assert _answer(tool, smb_id) in summary, summary
        assert OUTAGE not in summary, summary


def test_the_generated_catalogue_and_the_full_text_say_the_same(tmp_path):
    """mcp_tools.json (registry submissions are built from it) and llms-full.txt (every LLM crawler reads it) are
    generated from the manifest; a stale copy of either is the same defect on another surface."""
    catalogue = {t["name"]: t for t in json.loads((ROOT / "manifest" / "mcp_tools.json").read_text(encoding="utf-8"))}
    for tool in RESOLVES_IDS:
        summary = (catalogue[tool].get("_meta") or {}).get("hatchloop/readiness", {}).get("summary", "")
        if "osm:" in summary:
            assert _answer(tool, "osm:node/1001") in summary and OUTAGE not in summary, (tool, summary)
    full = wk.get_llms_full_txt()
    for tool in RESOLVES_IDS:
        section = full.split(f"## Operation: {tool}\n", 1)[1].split("### Failure Modes\n", 1)[1].split("\n## ", 1)[0]
        for code in {_answer(tool, i) for i in UNKNOWN_IDS}:
            assert f"- {code}" in section, f"llms-full.txt's Failure Modes for {tool} omit {code}"


def _tool_entries(tools) -> dict:
    return {t["name"]: t for t in tools}


def _list(profile):
    return _tool_entries(_run(ms._h_tools_list({"_profile": profile} if profile else {}))["tools"])


@pytest.mark.parametrize("profile", [None] + sorted(p for p in profiles.PROFILES if p != "chatgpt"))
def test_tools_list_on_every_surface_carries_the_current_readiness_sentence(profile):
    """tools/list is where a caller meets the sentence: full server and each door that serves the tool."""
    served = _list(profile)
    checked = 0
    for tool in ("verify_business", "capture_lead"):
        if tool not in served:
            continue
        summary = (served[tool].get("_meta") or {}).get("hatchloop/readiness", {}).get("summary", "")
        assert "out_of_supply_network" in summary and OUTAGE not in summary, (profile, tool, summary)
        checked += 1
    if profile is None:
        assert checked == 2


def test_the_committed_edge_snapshots_do_not_keep_the_old_contract():
    """The edge worker answers tools/list, /manifest and llms-full.txt from snapshots compiled into its bundle. They are
    origin-derived and are re-captured from the origin after a deploy (scripts/refresh_edge_snapshots.py); until then
    the tree must not carry text that contradicts the handlers."""
    tools = {t["name"]: t for t in json.loads((SNAP / "mcp-tools-list.json").read_text(encoding="utf-8"))["result"]["tools"]}
    manifest = {o["name"]: o for o in json.loads((SNAP / "manifest.json").read_text(encoding="utf-8"))["operations"]}
    for tool in RESOLVES_IDS:
        assert "out_of_supply_network" in _published_codes(manifest[tool]), f"edge manifest snapshot, {tool}"
        summary = (tools[tool].get("_meta") or {}).get("hatchloop/readiness", {}).get("summary", "")
        if "osm:" in summary:
            assert "out_of_supply_network" in summary and OUTAGE not in summary, (tool, summary)
    text = (SNAP / "llms-full.txt").read_text(encoding="utf-8")
    assert not re.search(r"osm:\.\.\. id returned by find_business answers supply_unreachable", text)


def test_no_documentation_still_says_an_osm_id_is_an_outage():
    """The prose that was updated with the handler (api/errors.md, the integration guide, architecture notes) stays so."""
    doc = (ROOT / "api" / "errors.md").read_text(encoding="utf-8")
    outage = next(line for line in doc.splitlines() if line.startswith("| `supply_unreachable`"))
    assert "out_of_supply_network" in outage
    for rel in ("docs/AGENT_INTEGRATION_GUIDE.md", "docs/architecture.md", "manifest/manifest.json"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"osm:[^\n]{0,80}supply_unreachable|supply_unreachable[^\n]{0,80}osm:", text), rel
