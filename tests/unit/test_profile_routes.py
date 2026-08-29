"""
Are the capability doors actually reachable, and are they billed like doors?

`test_profiles.py` is a good file. It has 28 tests, it pins the refusal
boundary hard, and every one of them passed while the doors were UNREACHABLE IN
PRODUCTION - `profiles.py` and the filtering inside `handle_mcp_request` were
complete, and the only route was `POST /mcp`, which passes no profile. The
locks were tested. Nothing had cut the keyholes.

That is the fifth producer-with-no-caller in this codebase, so these tests ask
the other question: does a ROUTE exist, does it pass the profile, and does the
rest of the system treat that route as the same billed surface?

THE SECOND HALF MATTERS AS MUCH AS THE FIRST. The rate limiter and the
telemetry counter both matched the exact string "/mcp". A new door would have
been un-limited and uncounted - the same tools, the same providers, the same
cost to us, invisible. A feature that quietly disables metering is worse than a
feature that does not ship.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import profiles  # noqa: E402

MAIN = open(os.path.join(ROOT, "main.py"), encoding="utf-8", errors="replace").read()


# --------------------------------------------------------------------------
# THE KEYHOLE — a route has to exist
# --------------------------------------------------------------------------

def test_a_route_exists_for_capability_profiles():
    """The link that was missing. Without this, every profile is dead code."""
    assert re.search(r'@app\.post\("/mcp/\{profile\}"', MAIN), \
        "there is no /mcp/{profile} route - the doors cannot be reached"


def test_the_route_passes_the_profile_to_the_handler():
    """A route that exists but drops the profile serves the FULL tool list
    through a narrow-looking URL, which is worse than no door: an agent that
    arrived through the sanctions endpoint could call send_message."""
    body = MAIN[MAIN.index('@app.post("/mcp/{profile}"'):]
    body = body[:body.index("\n@app.")] if "\n@app." in body else body
    assert "profile=profile" in body, \
        "the route does not pass the profile through - it serves the full server"


def test_the_profile_comes_from_the_route_not_the_payload():
    """If the profile were read from the request body, a caller could widen its
    own door and the narrowing would be advisory."""
    body = MAIN[MAIN.index('@app.post("/mcp/{profile}"'):]
    body = body[:body.index("\n@app.")] if "\n@app." in body else body
    assert 'payload.get("profile")' not in body
    assert 'payload["profile"]' not in body


def test_an_unknown_door_is_refused_before_the_engine_runs():
    """An unknown profile must not fall through to the full server. Refusing
    AFTER dispatch would mean a typo silently opened everything."""
    body = MAIN[MAIN.index('@app.post("/mcp/{profile}"'):]
    body = body[:body.index("\n@app.")] if "\n@app." in body else body
    # `await handle_mcp_request`, not the bare name. The route's docstring
    # discusses handle_mcp_request by name, and matching that put the "call
    # site" hundreds of characters before the validation - the test failed on
    # correct code because it was reading the prose that explains the code.
    # Second time today; the lesson is to anchor on syntax only the real thing
    # has.
    i_check = body.index("tools_for(profile)")
    i_call = body.index("await handle_mcp_request")
    assert i_check < i_call, "the profile is validated after the request is handled"
    assert "404" in body


# --------------------------------------------------------------------------
# METERING — the door is narrower, the engine is not
# --------------------------------------------------------------------------

def test_the_doors_are_rate_limited_like_the_main_endpoint():
    """`/mcp/sanctions-screening` runs the same tools against the same paid
    providers. Limiting only the exact string "/mcp" makes every door a
    bypass."""
    fn = MAIN[MAIN.index("def _rl_path_should_limit"):]
    fn = fn[:fn.index("\n@app.")] if "\n@app." in fn else fn[:2000]
    assert 'startswith("/mcp/")' in fn, \
        "capability doors are not rate limited - each one is a free lane"


def test_the_doors_are_counted_in_telemetry():
    """Uncounted traffic is traffic we cannot see, on the surfaces we are
    actively trying to get discovered through - so it would look like the doors
    were not working precisely when they were."""
    fn = MAIN[MAIN.index("async def _telemetry_counter_middleware"):]
    fn = fn[:fn.index("\n@app.")] if "\n@app." in fn else fn[:1500]
    assert 'startswith("/mcp/")' in fn, \
        "requests through capability doors are not counted"


# --------------------------------------------------------------------------
# The doors we actually advertise
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", sorted(profiles.PROFILES))
def test_every_declared_profile_is_routable(profile):
    """The route is `/mcp/{profile}` and the lookup is `profiles.tools_for`, so
    anything in PROFILES resolves. This fails loudly if a name is added that the
    validator rejects - e.g. one containing a slash, which would silently 404."""
    assert profiles.tools_for(profile)
    assert "/" not in profile, f"{profile} contains a slash and cannot be a path segment"


def test_the_error_names_the_alternatives():
    """An agent probing capability names should be told what DOES exist rather
    than left guessing - the whole point of these doors is discovery."""
    body = MAIN[MAIN.index('@app.post("/mcp/{profile}"'):]
    body = body[:body.index("\n@app.")] if "\n@app." in body else body
    assert "available" in body and "full_server" in body
