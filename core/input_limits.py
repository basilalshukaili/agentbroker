"""Upper bounds on the text a caller can put in a request, one definition for every handler a public door reaches.

WHY THIS EXISTS. The sanctions matcher compares the caller's name against ~79,000 list entries, and the only thing
that bounded the name was the size of the HTTP body. Measured on the live Claude-facing door: a name of "Kim", 20,000
spaces and "Jong" held the single worker for 13.6 s, 40,000 spaces for 37 s, and a 50 ms ticker on the same event
loop was stalled for 16.6 s (review of the ChatGPT door, 2026-10-04, on the Claude-facing sanctions door). One unauthenticated request, no key. The matcher is fixed to do the per-name work once
and off the event loop (core/screen_sanctions.py), but the name must still be bounded: every other stage (the
transliteration layer, the database queries, the receipt echoing the name back) is also linear in its length.

The limits are generous for anything a person or a registry would write and tight for anything else:

  * a name: 300 characters. The longest legal entity names on the lists we hold are well under 200; an Arabic name
    with its Latin rendering beside it is still under 300;
  * a product description: 300 characters (it is echoed into one guidance sentence);
  * a country: 100 characters (an ISO code or a name; the longest official country name is 56);
  * a Legal Entity Identifier: 64 characters (the standard is exactly 20; the slack is for stray whitespace and
    for a caller who pastes a prefix, and a wrong one is answered by the registry, not by us);
  * an HS code: 32 characters (an HS code has at most 12 digits plus separators).

A request over a limit is REFUSED, never truncated: a name cut at 300 characters is a different name, and a screen
of a different name reads as a screen of the one the caller asked about.
"""
from __future__ import annotations

from typing import Any, Optional

MAX_NAME_CHARS = 300
MAX_PRODUCT_CHARS = 300
MAX_COUNTRY_CHARS = 100
MAX_LEI_CHARS = 64
MAX_HS_CODE_CHARS = 32


def too_long(label: str, value: Any, limit: int) -> Optional[str]:
    """The refusal sentence when `value` is a string longer than `limit`, otherwise None.

    Names the field and the limit and counts the characters it was given, and never repeats the text itself: it is
    the caller's, it may be enormous, and a refusal must not become an amplifier. Contains no wording about the
    service's commercial terms (it is shown verbatim on a door that carries none)."""
    if isinstance(value, str) and len(value) > limit:
        return (f"{label} is too long: {len(value)} characters were given and this tool accepts at most {limit}. "
                f"Nothing was run on this call. Send the shorter, complete {label} again.")
    return None
