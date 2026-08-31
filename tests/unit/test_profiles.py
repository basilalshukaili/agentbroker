"""
Narrow doors onto the same engine — and the check that makes them real.

WHY THEY EXIST (measured 2026-08-29): the MCP registry search matches server
NAMES ONLY, so searched by capability we were absent from sanctions, sms,
compliance, booking and every other term an agent actually types. The registry
also caps a description at 100 characters, which a 20-tool server cannot use
honestly. And tools/list costs ~11,000 tokens when an agent wanting one tool
needs ~1,000.

THE PROPERTY THAT MATTERS MOST HERE is not the filtering, it is the REFUSAL. A
profile that lists four tools and still executes twenty is a wide server wearing
a small sign, and an agent that arrived through the sanctions door could call
send_message having never read its description, its cost, or its compliance
requirements. These tests exist mostly to keep that true.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import profiles  # noqa: E402
from agent_interface.mcp_server import handle_mcp_request  # noqa: E402


def run(coro):
    """A FRESH loop per call.

    This first used `asyncio.get_event_loop().run_until_complete(...)`, copied
    from a neighbouring test file. Alone, all 28 tests here passed. In the full
    suite, 20 of them failed with RuntimeError - because some earlier test had
    already closed the shared loop, and this file inherited the corpse.

    A test that passes alone and fails in the suite is worse than one that just
    fails: it reads as "the code broke" and sends you looking in the wrong
    place. asyncio.run() builds and tears down its own loop, so nothing another
    test does can reach these.
    """
    return asyncio.run(coro)


def call(method, params, profile=None):
    return run(handle_mcp_request(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers={}, profile=profile))


# --------------------------------------------------------------------------
# THE REFUSAL — what makes a door a door
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["send_message", "call_business",
                                  "schedule_appointment", "capture_lead"])
def test_the_sanctions_door_refuses_tools_it_does_not_advertise(tool):
    r = call("tools/call", {"name": tool, "arguments": {}},
             profile="sanctions-screening")
    assert "error" in r, f"{tool} was executable through a door that does not list it"
    assert "not available on this endpoint" in str(r["error"]["message"])


def test_the_refusal_names_the_full_server():
    """An agent refused here must be told where the tool DOES live, or the
    narrow door costs us the call instead of routing it."""
    r = call("tools/call", {"name": "send_message", "arguments": {}},
             profile="sanctions-screening")
    assert "mcp/agent-broker" in str(r["error"]["message"])


def test_a_door_still_runs_its_own_tools():
    r = call("tools/call", {"name": "screen_sanctions", "arguments": {"name": "x"}},
             profile="sanctions-screening")
    assert "error" not in r


def test_the_full_server_is_unchanged():
    """profile=None must expose the full tool surface (currently 22:
    the 21 at the time profiles landed, plus lookup_us_contracts)."""
    r = call("tools/list", {})
    assert len(r["result"]["tools"]) == 22


# --------------------------------------------------------------------------
# A caller must not be able to widen its own door
# --------------------------------------------------------------------------

@pytest.mark.parametrize("injected", [None, "agent-broker", "sms-whatsapp-messaging"])
def test_a_caller_cannot_widen_its_door_by_sending_profile(injected):
    """The profile is injected server-side from the ROUTE. If it were read from
    the payload, the narrowing would be a suggestion."""
    r = call("tools/list", {"_profile": injected}, profile="sanctions-screening")
    names = {t["name"] for t in r["result"]["tools"]}
    assert "send_message" not in names
    assert names == profiles.tools_for("sanctions-screening")


def test_injecting_a_profile_cannot_unlock_a_tool():
    r = call("tools/call",
             {"name": "send_message", "arguments": {}, "_profile": None},
             profile="sanctions-screening")
    assert "error" in r


# --------------------------------------------------------------------------
# The doors are actually narrow, which was the point
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_every_door_is_materially_lighter_than_the_monolith(profile):
    full = json.dumps(call("tools/list", {})["result"]["tools"])
    door = json.dumps(call("tools/list", {}, profile=profile)["result"]["tools"])
    assert len(door) < len(full) * 0.6, (
        f"{profile} is {len(door)} bytes vs {len(full)} - not enough lighter to "
        f"be worth a separate door")


@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_every_door_carries_the_orientation_tools(profile):
    """Whichever door an agent came through it must be able to ask what a call
    cost, poll an async operation, and check we are alive."""
    names = {t["name"] for t in call("tools/list", {}, profile=profile)["result"]["tools"]}
    for t in ("get_status", "get_outcome", "preview_cost", "self_test"):
        assert t in names, f"{profile} cannot {t}"


@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_every_advertised_tool_actually_exists(profile):
    """A door that lists a tool the server does not have is a 404 waiting to
    happen, and the agent blames us."""
    real = {t["name"] for t in call("tools/list", {})["result"]["tools"]}
    for t in profiles.tools_for(profile):
        assert t in real, f"{profile} advertises {t}, which the server does not serve"


def test_write_doors_can_obtain_an_smb_id():
    """send_message and schedule_appointment take an smb_id. A door that offers
    them without find/verify is a dead end - the agent cannot get one."""
    for profile in ("sms-whatsapp-messaging", "appointment-booking"):
        names = profiles.tools_for(profile)
        assert "find_business" in names and "verify_business" in names, profile


# --------------------------------------------------------------------------
# The registry constraints these exist to satisfy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_each_description_fits_the_registry_cap(profile):
    """100 chars, verified against the published schema. A longer one is
    REJECTED at publish time, which silently leaves the old text live."""
    d = profiles.PROFILES[profile]["description"]
    assert len(d) <= 100, f"{profile}: {len(d)} chars"


@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_each_name_carries_the_words_an_agent_would_search(profile):
    """The whole reason these exist: registry search matches names. A profile
    named for our brand instead of its capability is invisible again."""
    assert "hatchloop" not in profile.lower()
    assert "broker" not in profile.lower()
    assert len(profile.split("-")) >= 2


def test_an_unknown_profile_is_refused_not_silently_widened():
    with pytest.raises(profiles.ProfileError):
        profiles.tools_for("does-not-exist")
