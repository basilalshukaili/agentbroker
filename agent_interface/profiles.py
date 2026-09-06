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

# THE PRODUCT'S OWN NAME, ROUTED TO THE FULL SERVER.
#
# The founder, 2026-09-06: "i seen you making mistake of making the hatchloop mcp
# endpoint specifically points to agentbroker". He is right, and this is half the
# fix. The origin knew `/mcp` and five door names, so `/mcp/agent-broker` was a
# 404 there while the canonical host answered 200 - the same server reachable
# under two different shapes depending on which host you asked. Every listing we
# publish names the slug, so the slug must work everywhere.
#
# A NAME HERE IS NOT A DOOR. It maps to None, which means no filtering at all:
# the full 23 tools, exactly as `/mcp` serves them. Adding it to PROFILES would
# have made it a sixth door with a tool list somebody has to keep in step with
# the product, which is the drift the registry exists to end.
FULL_SERVER_ALIASES = frozenset({"agent-broker"})

PROFILES: dict[str, dict] = {
    "sanctions-screening": {
        "title": "Sanctions & company verification",
        # 100-char cap, and it is checked by the publisher. Leads with the words
        # an agent actually searches for.
        # NOT "UN". The UN consolidated list is as easy to fetch as the
        # others and is deliberately absent: it carries no open licence and no
        # commercial carve-out, unlike OFAC (public domain), the EU list (EC
        # open-data) and the UK list (OGL v3.0). We screen what we are licensed
        # to screen and say exactly that. See core/screen_sanctions.py.
        "description": "Sanctions screening (OFAC, EU, UK) and company verification. Free, no key, no signup.",
        "tools": (
            "screen_sanctions",
            "verify_company_record",
            "map_trade_restriction",
            "check_compliance",
        ),
        # Why this one ships first: every tool in it is READ-ONLY and needs no
        # smb_id, so the door works standalone; and its competence is
        # externally falsifiable in one keyless call - an agent can check our
        # sanctions answer against OpenSanctions itself.
        #
        # THIS USED TO CLAIM "no competitor in the registry lets a stranger
        # verify them for free before committing." That was FALSE and an
        # external review caught it: pipeworx-io's open-sanctions server is
        # free with no auth, and entia.systems gives 100 requests a month free.
        # Writing a competitive superlative into the source is how an
        # unverified boast ends up quoted onto a public listing - which is
        # exactly the drift this company keeps fixing in other people's copy.
        # The keyless read tier is a real advantage; being the ONLY one was
        # never checked and was not true.
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
        "description": "Find real businesses and book appointments. Books via Cal.com; imports 12 platforms.",
        "tools": (
            "schedule_appointment",
            "check_booking_link",
            "import_booking_url",
            "find_business",
            "verify_business",
        ),
    },
    # "compliance" is a 31-competitor registry search we were absent from
    # (measured 2026-08-29). Its own door owns that name. Every tool here is a
    # FREE, read-only pre-flight — the door works standalone with no key.
    "compliance-check": {
        "title": "Compliance pre-flight for AI agents",
        "description": "TCPA/GDPR/CASL compliance + sanctions & registry screening before you act. Free, no key.",
        "tools": (
            "check_compliance",
            "screen_sanctions",
            "verify_company_record",
            "map_trade_restriction",
        ),
    },
    # "verify company" is a distinct capability search; the door leads with the
    # official-registry data (GLEIF LEI, SEC EDGAR) that is the moat. Free reads.
    "company-verification": {
        "title": "Verify a company",
        "description": "Verify a company on official registries (GLEIF LEI, SEC EDGAR), screen sanctions. Free.",
        "tools": (
            "verify_company_record",
            "screen_sanctions",
            "lookup_us_contracts",
        ),
    },
}


class ProfileError(ValueError):
    """An unknown profile, or a tool that does not belong to one."""


def exists(profile: Optional[str]) -> bool:
    return (profile is None or profile in PROFILES
            or profile in FULL_SERVER_ALIASES)


def tools_for(profile: Optional[str]) -> Optional[frozenset]:
    """The tools this door exposes, or None for the full server.

    None means "no filtering" - that is the monolith, which stays exactly as it
    is for agents doing complex multi-step work.
    """
    if profile is None or profile in FULL_SERVER_ALIASES:
        return None
    if profile not in PROFILES:
        raise ProfileError(
            f"unknown profile {profile!r}; known: "
            f"{', '.join(sorted(set(PROFILES) | FULL_SERVER_ALIASES))}")
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
