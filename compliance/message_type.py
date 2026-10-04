"""Which message types exist, and how a caller's spelling of one is read - one definition for every surface.

THE DEFECT THIS MODULE CLOSES (second independent review of the DRR defects fix, 2026-10-04). The marketing
consent branch of the gate compared `message_type == "marketing"` exactly, and the two free previews
(`check_compliance` and `POST /compliance/check`) accepted any string. So "Marketing", "MARKETING",
" marketing" and any type the gate has never heard of ("promotional"; the HTTP field's own documented
"opt-in-confirm" and "customer-service", which were never real) skipped the consent check and came back
`legal: true` for an SMS with no consent on file. The real send path refuses every one of them, because
`MessageType` is an enum there - so the preview said "permitted" for a message the send would refuse.

The rule, applied by the preview tool, the HTTP route and the gate itself: strip and lower-case, and then the
value is one of the `MessageType` values or it is not a message type at all. The accepted spellings are derived
from the enum, so a sixth type is one edit in one place.
"""
from __future__ import annotations

from typing import Optional

from core.models import MessageType

VALID_MESSAGE_TYPES = tuple(m.value for m in MessageType)

_SHOWN_CHARS = 40


def canonical_message_type(value) -> Optional[str]:
    """The `MessageType` value a caller meant, or None when it names none.

    A `MessageType` member, or a string that is one after trimming and lower-casing. Nothing else: a number, a
    list, an empty string and "marketing blast" are all None."""
    if isinstance(value, MessageType):
        return value.value
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in VALID_MESSAGE_TYPES:
            return candidate
    return None


def shown(value) -> str:
    """A value as it may appear in OUR sentences: short, one line, never the caller's prose in full."""
    text = value if isinstance(value, str) else type(value).__name__
    text = " ".join(text.split())
    return text if len(text) <= _SHOWN_CHARS else text[:_SHOWN_CHARS] + "..."


def refusal_sentence(value) -> str:
    """The sentence every surface uses for an unknown type."""
    return (f"message_type must be one of {', '.join(VALID_MESSAGE_TYPES)} (upper or lower case), "
            f"got '{shown(value)}'. Nothing was checked or sent.")
