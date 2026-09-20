"""Which tools need a key, and every number computed from that one answer.

WHY THIS FILE EXISTS. On 2026-09-20 one rule changed - get_conversation,
get_status and get_outcome began refusing another caller's records - and SIX
published surfaces went stale in the same commit, because each of them worked
out "which tools need a key" for itself:

  * agent_interface/mcp_server.py tagged get_conversation "[free, no key]" in
    tools/list, because the cost tag consulted only the WRITE set.
  * agent_interface/well_known.py listed it in free_tools for the same reason -
    in the file whose own comment calls it "the file agents read to decide what
    they can do without signing up".
  * mcp_server's initialize instructions said "14 of the 23 tools need no key;
    the 8 write tools require a token". 14 + 8 = 22: one tool fell in neither
    bucket, and a reader assumes it is free.
  * web/pages.py stated 15 twice, in a stat tile and in the refund policy.
  * smithery.yaml, server.json and glama.json each published "15 of the 23
    (12 always-free + 3 within a quota)".
  * agent_interface/key_requests.py said "12 of our 20", stale on both numbers.

None of that was deployed, so it did no harm. What it would have done is worse
than a wrong number on a page: an agent that reads our published description
and plans a keyless session gets refused on its first call, and an autonomous
client cannot ask a human why.

well_known.py's own history records this exact drift being found and fixed once
already, for import_booking_url, and reintroduced one file over. Six
independent derivations of the same fact will drift again the next time the
rule moves - so there is now one.

WHAT BELONGS HERE: the tool-to-auth-class mapping, and only counts that follow
from it. Enforcement stays where it is enforced (core/ownership.py decides who
may read a row; mcp_server gates the writes). This module answers "what do we
tell the world", and every surface - tools/list, /.well-known/*, the website,
the registry manifests - reads it rather than deciding for itself.

THE THREE AUTH CLASSES, which partition the tool list exactly:

  keyless      Costs no credits, needs no key. A stranger can call it now.
  quota_free   Priced, but free to an anonymous caller up to a daily quota.
  needs_key    Refused outright without an X-Agent-Identity key - either
               because it writes/spends, or because what it returns belongs to
               one caller.

"Free" and "keyless" are NOT the same set, and conflating them is its own bug:
import_booking_url and get_conversation both cost zero credits and both refuse
an anonymous call. Every function below says which question it answers.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------
# The mapping. THIS IS THE DEFINITION; everything else in the repo aliases it.
# ---------------------------------------------------------------------------

# Tools that mutate state or charge upstream credits. The MCP dispatcher must
# gate these the same way /ops/* gates them - otherwise a developer-tier
# customer can bypass scope-checks by tunneling write calls through /mcp.
WRITE_TOOLS_REQUIRING_AUTH = frozenset({
    "send_message",
    "schedule_appointment",
    "send_transactional_confirmation",
    "capture_lead",
    "handle_inbound",
    "escalate_to_human",
    "import_booking_url",
    "call_business",
})

# Tools that cost nothing and still need a key, because what they return
# belongs to ONE caller. The enforcement lives in the handler (an anonymous
# get_conversation is refused before any lookup); this is the same fact in the
# form the public counters need, because counting get_conversation as keyless
# publishes a number that is false on every anonymous call.
#
# get_status and get_outcome are deliberately NOT here: a keyless call is
# still accepted and answered (not_found for an unknown id, forbidden for
# anyone else's), it just never returns another caller's content — including,
# since the unowned-read policy in core/status_outcome.py was tightened to
# fail closed, an unowned one. A tool belongs in this set only when a keyless
# call is refused outright, before any lookup - get_conversation does that;
# get_status/get_outcome do not.
#
# tests/unit/test_conversation_ownership.py calls every tool named here
# anonymously and requires a refusal, so the list cannot drift away from the
# code it describes.
IDENTITY_REQUIRED_READ_TOOLS = frozenset({"get_conversation"})

# The union is the only thing most callers want: "can a stranger call this?"
TOOLS_REQUIRING_KEY = WRITE_TOOLS_REQUIRING_AUTH | IDENTITY_REQUIRED_READ_TOOLS


def requires_key(tool_name: str) -> bool:
    """True when a call with no X-Agent-Identity key is refused outright.

    This is the question every advertised surface is actually asking, and the
    one the cost tag in tools/list got wrong by asking a narrower one.
    """
    return tool_name in TOOLS_REQUIRING_KEY


# ---------------------------------------------------------------------------
# Counts, derived from the mapping above and the live manifest
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _ops() -> list[dict]:
    """The operation list tools/list is built from. Never a typed number.

    Imported lazily: this module sits under core/ so the write paths and
    core/ownership.py can import it with no cycle, and agent_interface imports
    core, not the other way round.
    """
    try:
        from agent_interface.manifest_server import get_full_manifest
        return list(get_full_manifest().get("operations") or [])
    except Exception:                           # noqa: BLE001
        return []


def _basis(op: dict) -> str:
    return (op.get("cost_model") or {}).get("basis", "per_call")


def auth_class(tool_name: str) -> str:
    """"keyless" | "quota_free" | "needs_key" for one tool.

    The order matters: a tool that needs a key is never advertised as free to
    a stranger, whatever its price. That is the mistake this module removes.
    """
    if requires_key(tool_name):
        return "needs_key"
    op = next((o for o in _ops() if o.get("name") == tool_name), None)
    if op is not None and _basis(op) == "freemium_daily_quota":
        return "quota_free"
    return "keyless"


def total_tools() -> int:
    """Every tool in tools/list."""
    return len(_ops())


def costs_nothing() -> int:
    """Tools that spend no credits. NOT the same as keyless - see the docstring."""
    return sum(1 for o in _ops() if _basis(o) == "free")


def keyless() -> int:
    """Callable right now, by a stranger, with no key and no credits."""
    return sum(1 for o in _ops() if auth_class(o.get("name", "")) == "keyless"
               and _basis(o) == "free")


def quota_free() -> int:
    """Premium data tools that are free within a daily quota, then billed."""
    return sum(1 for o in _ops() if auth_class(o.get("name", "")) == "quota_free")


def needs_key() -> int:
    """Tools that cannot be called at all without a free key."""
    return len(TOOLS_REQUIRING_KEY)


def write_tools() -> int:
    """Tools that MUTATE STATE OR SPEND CREDITS - the priced write set.

    This is a NARROWER question than needs_key(), and the difference is the
    whole reason this module exists. needs_key() is the union: the writes PLUS
    the free reads that belong to one caller. Any sentence about the PRICE
    TABLE, the 100 write ops/day allowance, or "what spends anything" is asking
    THIS question, not that one.

    It exists because it was missing. On 2026-09-20 the pricing page and the
    checkout page both needed this number, were offered only needs_key(), and
    used it - so the page said "The 9 write tools require a free key (100 write
    ops/day)" directly above a grid of 8, and implied get_conversation, which
    costs nothing, spends credits and eats the write allowance. That is exactly
    the free/keyless conflation the docstring at the top of this file says the
    module exists to end, committed by the module itself. A missing token is
    not a slip; the author reaches for the nearest one.
    """
    return len(WRITE_TOOLS_REQUIRING_AUTH)


def usable_without_key() -> int:
    """What a stranger can call before signing up for anything."""
    return keyless() + quota_free()


def partition_is_sound() -> Optional[str]:
    """None when every tool falls in exactly one class, else what is wrong.

    A derivation can be wrong in a way that still looks tidy: the first
    `needs_key` written here subtracted the daily-quota tools, which are not in
    the auth set, and would have published 5 where the truth is 9. The gate in
    scripts/check_no_typed_counts.py and the registry test both call this, so a
    class that stops adding up fails a build rather than reaching a page.
    """
    total = total_tools()
    if total <= 0:
        return "the manifest read as zero operations - nothing is being counted"
    parts = keyless() + quota_free() + needs_key()
    if parts != total:
        return (f"the partition does not add up: keyless {keyless()} + quota "
                f"{quota_free()} + needs-key {needs_key()} = {parts}, but there "
                f"are {total} tools. Every tool must fall in exactly one class.")
    unknown = sorted(TOOLS_REQUIRING_KEY - {o.get("name") for o in _ops()})
    if unknown:
        return (f"{unknown} require a key but are not in the manifest - a rule "
                f"about a tool that does not exist protects nothing")
    return None


def free_tier_sentence() -> str:
    """The one sentence every public surface should use for the free tier.

    One phrasing, one source. Three surfaces previously said it three ways.
    """
    return (f"{usable_without_key()} of the {total_tools()} tools work with no key "
            f"({keyless()} always free, {quota_free()} free within a daily quota)")


def auth_note() -> str:
    """The free-tier sentence as the registry catalogues publish it.

    smithery.yaml, server.json and glama.json are data files that cannot
    import anything, so scripts/gen_manifests.py substitutes this in from
    registry/servers.yaml tokens and CI fails if a generated file drifts.
    """
    return (f"{usable_without_key()} of the {total_tools()} tools require no auth "
            f"({keyless()} always-free + {quota_free()} free within a daily quota)")


# Tokens the page templates and the registry source use instead of digits.
# `page()` in web/_partials.py substitutes them on every rendered page and
# description, and scripts/gen_manifests.py substitutes them into the registry
# manifests, so neither can forget - and scripts/check_no_typed_counts.py fails
# the build if a literal count is typed back in. Inside an f-string body write
# {{n_tools}}; the doubled braces survive formatting and arrive here as
# {n_tools}.
TOKENS = {
    "{n_tools}": total_tools,
    "{n_keyless}": keyless,
    "{n_quota}": quota_free,
    "{n_no_key}": usable_without_key,
    "{n_needs_key}": needs_key,
    "{n_write_tools}": write_tools,
    "{n_costs_nothing}": costs_nothing,
}


def substitute(text: str) -> str:
    if not text or "{n_" not in text:
        return text
    for token, fn in TOKENS.items():
        if token in text:
            text = text.replace(token, str(fn()))
    return text
