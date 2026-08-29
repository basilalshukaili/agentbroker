"""
Narrow doors onto the same engine.

WHY THIS EXISTS, measured 2026-08-29 rather than assumed:

  * The MCP registry search matches SERVER NAMES ONLY, not descriptions.
    Proven both directions: "broker" (in our name) finds us; "whatsapp" and
    "book" (both in our description) do not. Searched by capability - which is
    how a stranger agent searches - we are ABSENT from sanctions (10
    competitors listed), sms (14), compliance (31), book appointment, verify
    company, lead capture and trade restriction. We are findable only by
    someone who already knows our name, which is nobody.
  * The registry caps `description` at 100 characters. A twenty-tool server
    physically cannot describe itself. A narrow one can.
  * tools/list costs 44,369 bytes - about 11,000 tokens. An agent that wants
    only send_message needs 1,082 of them. 90% of what it loads is irrelevant,
    every single connection, and a small agent is right to refuse that.

One name, one description, one small tool list per capability. The DEPTH stays
where it is: the compliance gate, the correlation cascade, the idempotency
ledger and demand shaping all live in the origin, BELOW the tool surface, so
every door proxies to the same engine and every caller is protected identically.
Complicated engine, simple doors.

THE RULE THAT MAKES A DOOR REAL: a profile must REFUSE tools outside it. If a
narrow server still executes anything, it is a wide server wearing a small sign -
and an agent that arrived through the sanctions door could call send_message
having never read its description, its cost, or its compliance requirements.
"""
from __future__ import annotations

from typing import Optional

# Tools every door carries. An agent needs to be able to ask what a call cost,
# poll an async operation, and check we are alive - whichever door it came
# through. Keeping these everywhere costs ~1,200 tokens and removes a whole
# class of "the narrow server is useless on its own" complaint.
_ORIENTATION = ("get_status", "get_outcome", "preview_cost", "self_test")

PROFILES: dict[str, dict] = {
    "sanctions-screening": {
        "title": "Sanctions & company verification",
        # 100-char cap, and it is checked by the publisher. Leads with the words
        # an agent actually searches for.
        "description": "Sanctions screening (OFAC/EU/UN/UK) and company verification. Free, no key, no signup.",
        "tools": (
            "screen_sanctions",
            "verify_company_record",
            "map_trade_restriction",
            "check_compliance",
        ),
        # Why this one ships first: every tool in it is READ-ONLY and needs no
        # smb_id, so the door works standalone; and its competence is
        # externally falsifiable in one keyless call - an agent can check our
        # sanctions answer against OpenSanctions itself. No competitor in the
        # registry lets a stranger verify them for free before committing.
        "why_first": "read-only, keyless, and externally verifiable in one call",
    },
    "sms-whatsapp-messaging": {
        "title": "Message a business",
        "description": "Send WhatsApp, SMS, email or voice to real businesses. TCPA/GDPR/CASL gate built in.",
        "tools": (
            "send_message",
            "send_transactional_confirmation",
            "check_compliance",
            "get_conversation",
            # The find/verify pair travels with every write door: send_message
            # takes an smb_id, and without a way to obtain one the door is a
            # dead end.
            "find_business",
            "verify_business",
        ),
    },
    "appointment-booking": {
        "title": "Book with a business",
        "description": "Find real businesses and book appointments with them. 12 booking platforms supported.",
        "tools": (
            "schedule_appointment",
            "check_booking_link",
            "import_booking_url",
            "find_business",
            "verify_business",
        ),
    },
}


class ProfileError(ValueError):
    """An unknown profile, or a tool that does not belong to one."""


def exists(profile: Optional[str]) -> bool:
    return profile is None or profile in PROFILES


def tools_for(profile: Optional[str]) -> Optional[frozenset]:
    """The tools this door exposes, or None for the full server.

    None means "no filtering" - that is the monolith, which stays exactly as it
    is for agents doing complex multi-step work.
    """
    if profile is None:
        return None
    if profile not in PROFILES:
        raise ProfileError(
            f"unknown profile {profile!r}; known: {', '.join(sorted(PROFILES))}")
    return frozenset(PROFILES[profile]["tools"]) | frozenset(_ORIENTATION)


def allows(profile: Optional[str], tool: str) -> bool:
    """Whether this door may execute `tool`. The check that makes it a door."""
    allowed = tools_for(profile)
    return True if allowed is None else tool in allowed


def describe(profile: str) -> dict:
    if profile not in PROFILES:
        raise ProfileError(f"unknown profile {profile!r}")
    p = PROFILES[profile]
    return {
        "name": f"dev.hatchloop/{profile}",
        "title": p["title"],
        "description": p["description"],
        "tool_count": len(tools_for(profile) or ()),
        "endpoint": f"https://hatchloop.dev/mcp/{profile}",
    }
