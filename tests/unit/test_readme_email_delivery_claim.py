"""The README must not state, as the current fact, that key email delivery is unconfigured.

THE DEFECT (third review of feat/x402-honesty-20261004, P2). README.md said, in a status row and again in the
blockquote under it, that no email provider is configured on production, that POST /keys/request therefore
answers 503 onboarding_unavailable, and that `GET /healthz/external` reports `resend: not_configured`. Measured
2026-10-04: that endpoint reports `resend: ok` (the key is a full-access key) and `twilio: not_configured`; the
container has RESEND_API_KEY set and the sender domain is verified at Resend (docs/OAUTH-CONNECT.md, read-only
checks). A public README asserting the opposite of the running system is the failure this whole branch exists to
remove. A test cannot read production, so it pins the one thing it can: the false sentences do not come back, and
the row points at the live source instead of freezing a state.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()

# Phrasings of the stale claim. Each is a statement that delivery is NOT set up, as a present fact.
STALE = [
    r"(?i)no email provider is configured",
    r"(?i)email delivery[^.|]{0,60}is not configured on production",
    r"(?i)resend:\s*not_configured",
    r"(?i)free-key email delivery\s*\|\s*\*\*blocked",
]


def test_the_readme_does_not_say_key_email_is_unconfigured():
    hits = [p for p in STALE if re.search(p, README)]
    assert hits == [], f"README states a stale fact about email delivery: {hits}"


def test_the_email_row_points_at_the_live_health_check():
    row = next(line for line in README.splitlines() if line.startswith("| Free-key email delivery"))
    assert "/healthz/external" in row, "the live state of the provider is that endpoint; the row must say so"
    assert "onboarding_unavailable" in row, "the honest 503 is still documented, as a conditional"
    assert "only when" in row, "the 503 is conditional on a failed send, not the standing state"
