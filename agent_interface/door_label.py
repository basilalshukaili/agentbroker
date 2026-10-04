"""Which door a request came through, as one of OUR labels - never as text the caller typed.

Verdict item A7 (docs/reviews/2026-10-03-mcp-focus-verdict.md): "without it we cannot see the first buyer". The
door a request used is the first thing anybody asks about it (did the Muse caller come in at the full server or
through the sanctions door? is anyone still knocking on a retired one?), and until now it lived in free text for
five of the doors and nowhere for the sixth.

The labels, which are what `usage_events.door` holds (migration 013):

  agent-broker       the full server: `/mcp`, and `/mcp/agent-broker`, which Caddy rewrites to `/mcp` before the
                     origin sees it and which the origin also answers itself. One door under two spellings.
  <capability door>  one of agent_interface/profiles.PROFILES, by its own name.
  retired:<slug>     one of the six retired servers (agent_interface/retired_doors.py), which answer with a
                     tombstone. The prefix is what keeps scorer traffic out of every "do people use us" figure:
                     a query can drop `retired:%` without knowing which servers were retired.
  unknown            a path under /mcp that is not a door at all (a probe, a typo, a key pasted into the URL).
                     The caller's text is NOT kept - only the fact that it was not a door.

and no label at all (None) for anything that is not an MCP door: a page, a webhook, a REST route.

THE DOOR COMES FROM THE ROUTE, never from the payload, for the same reason the profile does (a caller that could
name its own door would be writing its own analytics). The labels are validated against DOOR_PATTERN, and the
database function refuses anything that does not match it - the same pattern, written the same way in both places
(a test pins that they are the same string).

Pure functions: no I/O, no state.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from agent_interface import profiles, retired_doors

FULL_DOOR = "agent-broker"
UNKNOWN_DOOR = "unknown"
RETIRED_PREFIX = "retired:"

# Written to be identical in Python (re) and PostgreSQL (ARE): migration 013 validates p_door against this string.
DOOR_PATTERN = r"^(retired:)?[a-z0-9][a-z0-9._-]{0,62}$"
_DOOR = re.compile(DOOR_PATTERN)


def is_valid(label: Any) -> bool:
    """True for a string the database will accept as a door."""
    return isinstance(label, str) and _DOOR.fullmatch(label) is not None


def for_retired(slug: str) -> str:
    return f"{RETIRED_PREFIX}{slug}"


def for_profile(profile: Optional[str]) -> str:
    """The label for the `profile` a dispatcher request was routed with (None = the bare /mcp route)."""
    if profile is None or profile in profiles.FULL_SERVER_ALIASES:
        return FULL_DOOR
    if retired_doors.is_retired(profile):
        return for_retired(profile)
    if profile in profiles.PROFILES:
        return profile
    return UNKNOWN_DOOR


def for_path(path: Any) -> Optional[str]:
    """The label for a URL path, or None when the path is not under /mcp.

    `/mcp`, `/mcp/` and `/mcp/agent-broker` are the full server; `/mcp/<door>` and `/mcp/<retired>/mcp` name
    their door; anything else under /mcp is `unknown`."""
    if not isinstance(path, str):
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "mcp":
        return None
    if len(parts) == 1:
        return FULL_DOOR
    slug = parts[1]
    if len(parts) == 2:
        return for_profile(slug)
    if len(parts) == 3 and parts[2] == "mcp" and retired_doors.is_retired(slug):
        return for_retired(slug)
    return UNKNOWN_DOOR
