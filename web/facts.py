"""Every number the public pages state about the product, derived not typed.

WHY THIS EXISTS. The founder caught it on 2026-09-06: the page headed "How you
pay" - which describes the CREDIT RAILS FOR THE WHOLE PLATFORM - contained the
sentence "15 of 23 tools are free without a key either way". That is a fact about
one product, typed by hand, on a page about the shared currency. It becomes false
the day a second server ships, and HatchLoop is being built to run fifty.

The numbers were correct. That is not the point. The defect is that a product
fact reached a platform surface by being typed, which is the same disease as the
six manifests that disagreed about our own version number that morning.

WHAT THIS FILE IS NOW. A thin view onto core/tool_auth.py, which owns the
tool-to-auth-class mapping and every count computed from it. It used to do its
own derivation from the manifest and the auth set, which was correct and still
not enough: five OTHER surfaces did their own, and when the identity rule
changed on 2026-09-20 this file was updated and they were not. The counts have
one home; this module keeps the names the website already imports.

THE TWO COUNTS ARE NOT THE SAME NUMBER and conflating them is its own bug:
thirteen tools cost no credits; eleven need no key. import_booking_url and
get_conversation are both free and both require a key. A careful buyer counted
one against the other, concluded our surfaces contradicted each other, and was
right that something was wrong even though both numbers were defensible. So
each function below says which question it answers.
"""
from __future__ import annotations

from core import tool_auth

# Each name below is the question the pages ask, answered in exactly one place.
total_tools = tool_auth.total_tools           # every tool in tools/list
costs_nothing = tool_auth.costs_nothing       # spends no credits (NOT keyless)
keyless = tool_auth.keyless                   # no key, no credits, no signup
quota_free = tool_auth.quota_free             # free to an anonymous caller up to a daily quota
usable_without_key = tool_auth.usable_without_key
needs_key = tool_auth.needs_key               # refused outright without a key
free_tier_sentence = tool_auth.free_tier_sentence

# Tokens the page templates use instead of digits. `page()` in web/_partials.py
# substitutes them on every rendered page and every meta description, so a
# template cannot forget - and scripts/check_no_typed_counts.py fails the build
# if a literal count is typed back in. Inside an f-string body write
# {{n_tools}}; the doubled braces survive formatting and arrive here as
# {n_tools}.
_TOKENS = tool_auth.TOKENS


def substitute(text: str) -> str:
    return tool_auth.substitute(text)
