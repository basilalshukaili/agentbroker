"""
The restricted-content gate must not be defeated by a keyboard.

An external compliance review broke the whole classifier with character
substitution:

    "Join our casino! Place your bets and win big money"    -> gambling
    "Join our cas1no! Massive jackp0t, place your b3ts..."  -> CLEAN

Every pattern in `content_classifier.py` matches literal spelling, so typing a
1 for an i walked past all of them. That is the oldest spam-filter evasion
there is, and it matters more for us than for most: the compliance layer is the
thing this company claims as its moat, and "cas1no" got through it.

THE OTHER HALF OF THIS FILE IS THE FALSE-POSITIVE SIDE, and it is why the fix
is narrow. This product sends appointment reminders, order confirmations and
booking receipts - text full of digits. A de-obfuscator that turns "Clinic 5"
or "order #10553" into a banned word would block real customers' real
messages, which is the more expensive failure for a paying user. Both
directions are pinned below.

The fix is a heuristic and does not end the arms race - Unicode homoglyphs,
zero-width joiners and s p a c e d letters still get through. It closes the
cheapest attack, which is the one that was actually demonstrated against us.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from compliance.content_classifier import classify_content  # noqa: E402


def category(text: str) -> str:
    c = classify_content(text).category
    return c.value if hasattr(c, "value") else str(c)


# --------------------------------------------------------------------------
# MUST STILL BE CAUGHT when spelled around
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    # The exact string from the review.
    "Join our cas1no! Massive jackp0t, place your b3ts now and w!n big money",
    "Best 0nline cas1no bonus",
    "Weekly j4ckpot draw - enter now",
    "Try our new sp0rtsbook, free b3t on signup",
])
def test_substituted_spelling_is_still_blocked(text):
    assert category(text) != "clean", (
        f"{text!r} passed as clean - the gate is defeated by a keyboard")


def test_the_plain_spelling_still_works():
    """The raw text is still checked first. Normalisation is an ADDITIONAL
    pass, so a bug in it can never lose an exact match."""
    assert category("Join our casino! Place your bets and win big money") == "gambling"


@pytest.mark.parametrize("text", [
    "payday l0an no credit check",
    "buy w33d online",
])
def test_other_categories_see_through_it_too(text):
    """The gambling check was fixed first because that is what the review
    demonstrated - but every category matched literal spelling, so every
    category had the same hole."""
    assert category(text) != "clean"


# --------------------------------------------------------------------------
# MUST NOT start blocking ordinary transactional messages
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Your appointment is confirmed for 3pm",
    "Your table for 4 is booked at 7pm on the 15th",
    "Order #10553 ships Tuesday. Track at example.com/t/8801",
    "Reminder: your 3:30 with Dr Ali at Clinic 5",
    "Your booking reference is B4T7-99X. See you Friday.",
    "Invoice 4417 for 350 OMR is due on the 5th",
    "We are open 24/7 - call us on +968 9000 0000",
])
def test_ordinary_messages_with_digits_are_not_blocked(text):
    """This is the half that protects paying customers.

    Our traffic is appointment reminders and receipts - text that is mostly
    digits. A de-obfuscator that turns "Clinic 5" into a banned word would
    refuse real messages, which for a customer is worse than letting one spam
    message through.
    """
    assert category(text) == "clean", (
        f"{text!r} was blocked - an ordinary transactional message must send")
