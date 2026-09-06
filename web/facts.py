"""Every number the public pages state about the product, derived not typed.

WHY THIS EXISTS. The founder caught it on 2026-09-06: the page headed "How you
pay" - which describes the CREDIT RAILS FOR THE WHOLE PLATFORM - contained the
sentence "15 of 23 tools are free without a key either way". That is a fact about
one product, typed by hand, on a page about the shared currency. It becomes false
the day a second server ships, and HatchLoop is being built to run fifty.

The numbers were correct. That is not the point. The defect is that a product
fact reached a platform surface by being typed, which is the same disease as the
six manifests that disagreed about our own version number that morning.

`agent_interface/mcp_server.py` already carried `_total_tool_count` with a
docstring admitting the author hardcoded "12 of the 23 tools" one commit after
building a CI gate against exactly that. The habit is stronger than the rule,
which is why the rule has to be a function.

THE TWO COUNTS ARE NOT THE SAME NUMBER and conflating them is its own bug:
THIRTEEN tools cost no credits; TWELVE need no key. `import_booking_url` is free
and still requires a free email-verified key. A careful buyer counted 13 from
tools/list against 12 on the pricing page, concluded our surfaces contradicted
each other, and was right that something was wrong even though both numbers were
defensible. So each function below says which question it answers.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _ops() -> list[dict]:
    from agent_interface.manifest_server import get_full_manifest
    return list(get_full_manifest().get("operations") or [])


def total_tools() -> int:
    """Every tool in tools/list."""
    return len(_ops())


def _basis(op: dict) -> str:
    return (op.get("cost_model") or {}).get("basis", "per_call")


def costs_nothing() -> int:
    """Tools that spend no credits. NOT the same as keyless - see the module docstring."""
    return sum(1 for o in _ops() if _basis(o) == "free")


def keyless() -> int:
    """Tools callable with NO KEY AT ALL: free of credits and not a gated write."""
    from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH
    return sum(1 for o in _ops()
               if _basis(o) == "free" and o.get("name") not in _WRITE_TOOLS_REQUIRING_AUTH)


def quota_free() -> int:
    """Premium data tools that are free within a daily quota, then billed."""
    return sum(1 for o in _ops() if _basis(o) == "freemium_daily_quota")


def usable_without_key() -> int:
    """What a stranger can call before signing up for anything."""
    return keyless() + quota_free()


def free_tier_sentence() -> str:
    """The one sentence every public surface should use for the free tier.

    One phrasing, one source. Three surfaces previously said it three ways.
    """
    return (f"{usable_without_key()} of the {total_tools()} tools work with no key "
            f"({keyless()} always free, {quota_free()} free within a daily quota)")


def needs_key() -> int:
    """Write tools that cannot be called at all without a free email-verified key.

    The first version of this subtracted the daily-quota tools, on the assumption
    they were part of the auth-required set. They are not - they have an anonymous
    allowance - and the subtraction published 5 where the truth is 8. Deriving a
    number is not the same as deriving it correctly, and the only reason it was
    caught is that the page it replaced listed all eight tools by name.
    """
    from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH
    return len(_WRITE_TOOLS_REQUIRING_AUTH)


# Tokens the page templates use instead of digits. `page()` in web/_partials.py
# substitutes them on every rendered page and description, so a template cannot
# forget - and scripts/check_no_typed_counts.py fails the build if a literal
# count is typed back in. Inside an f-string body write {{n_tools}}; the doubled
# braces survive formatting and arrive here as {n_tools}.
_TOKENS = {
    "{n_tools}": total_tools,
    "{n_keyless}": keyless,
    "{n_quota}": quota_free,
    "{n_no_key}": usable_without_key,
    "{n_needs_key}": needs_key,
    "{n_costs_nothing}": costs_nothing,
}


def substitute(text: str) -> str:
    if not text or "{n_" not in text:
        return text
    for token, fn in _TOKENS.items():
        if token in text:
            text = text.replace(token, str(fn()))
    return text
