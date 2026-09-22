"""
Shared "must we assume production?" decision for security guards that exist
to catch a dev-default secret before it ships live.

Board row 250. Three guards each independently keyed on
`os.getenv("ENVIRONMENT") == "production"`:
  - billing/receipt_signer.py   (raises)
  - agent_interface/identity.py (logs)
  - agent_interface/unsubscribe.py (logs)

That condition is False whenever ENVIRONMENT is unset, empty, misspelled, or
holds a value nobody anticipated ("staging", "testing", "Production" with a
stray space) — and the real production container ran with ENVIRONMENT UNSET
the entire time. All three guards silently evaluated False and never fired,
while the dev-default secrets they exist to catch stayed live in a PUBLIC
repository. Guard 3's own comment says as much: "every opt-out token is
forgeable by anyone who can read this line" — and it never logged a word,
because the condition it copied never fires on silence.

The recurring failure isn't "read ENVIRONMENT more carefully" — copying that
one-liner into three places is what let it drift into three subtly different
guards in the first place (one raises, two only log; see memory
lessons-learned-in-one-place-only). The fix is ONE place that answers the
question the same way for every guard, and answers "I don't know which
environment this is" with the dangerous assumption instead of the convenient
one.

Do NOT reuse channels/stub_policy.py's `_is_production()` here, and do not
make it reuse this. Since 2026-09-22 the two AGREE on the answer — that
helper also fails closed now, accepting only ENVIRONMENT="development" — but
they answer different questions ("is this secret forgeable?" vs "may we
fabricate a successful send?") and must stay independently changeable. If
these guards are ever loosened (admitting "staging", say), that must not
silently re-enable fabricated delivery receipts as a side effect. Sharing the
spelling of the answer is deliberate; sharing the decision is not.

CORRECTION (2026-09-22). An earlier version of this paragraph argued that
stub_policy SHOULD keep failing open, because its ALLOW_STUB_CHANNELS opt-in
was itself a sufficient second gate. That reasoning was circular and has been
retired: stub_policy's prod guard exists precisely BECAUSE that opt-in can
leak onto a production host — its own comment said "even if someone
accidentally sets ALLOW_STUB_CHANNELS on Render" — so the opt-in cannot also
be the reason the guard may be lax. The two flags also fail for one shared
reason (a host nobody configured carefully), so they were never the
independent layers that a defence-in-depth argument needs. The full argument
lives in channels/stub_policy.py's module docstring.
"""
from __future__ import annotations

import os

# The ONLY spelling that opts a run out of "assume production" for these
# guards. Anything else — unset, empty, "staging", "testing", a typo, mixed
# case with stray whitespace — is treated as production. Local development
# must say so explicitly; silence is not consent.
_EXPLICIT_NON_PRODUCTION = "development"


def is_production_for_security_guards() -> bool:
    """
    True unless ENVIRONMENT is exactly "development" (case/whitespace
    insensitive).

    This is intentionally stricter than config.py's own three-way
    development/staging/production split (config.ENVIRONMENT, used for things
    like DEBUG and REQUIRE_AUTH defaults elsewhere in the app). Those other
    defaults are conveniences; these guards are the last line of defense
    against a forgeable secret reaching a real customer, so "I could not tell
    which environment this is" has to mean "assume the dangerous one," not
    "assume the convenient one."
    """
    value = os.getenv("ENVIRONMENT", "").strip().lower()
    return value != _EXPLICIT_NON_PRODUCTION
