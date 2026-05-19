"""
PII redaction helpers for structured logs.

Every place we log a customer identifier should run it through `mask_pii` so
operators can still triage incidents without the logs themselves becoming a
PII exfiltration vector. Patterns intentionally keep enough characters to
correlate a single user across log lines but not enough to reconstruct the
identifier from the log alone.

Conventions:
  - Email "foo@bar.com" -> "f***@b***.com"
  - Phone "+14045550100" -> "+1404***0100"   (country+area kept, mid masked,
    last four kept for cross-line correlation)
  - JWT-like token (>20 chars) -> "...<last 12>" (mirrors what
    billing/telegram_revenue_alerts.py already does for token suffixes)

All helpers are pure, total functions: empty/None input returns a fixed
sentinel ("<empty>") and never raises.
"""
from __future__ import annotations

import re

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _mask_email(value: str) -> str:
    """foo@bar.com -> f***@b***.com (keeps first char of local, first of
    domain, full TLD). Falls back to "***" if the input isn't a plausible
    email after splitting."""
    local, _, domain = value.partition("@")
    if not local or "." not in domain:
        return "***"
    sub, _, tld = domain.rpartition(".")
    if not sub:
        return "***"
    return f"{local[0]}***@{sub[0]}***.{tld}"


def _mask_phone(value: str) -> str:
    """E.164 phone -> "+CC<area>***<last4>". For US +14045550100 -> "+1404***0100".
    Country code is taken to be 1-3 digits (matches +1 for US/CA and the
    longest E.164 country codes like +998). Falls back to "***" on a value
    that doesn't parse as E.164."""
    if not _E164_RE.match(value):
        return "***"
    # Heuristic: assume country code is 1 digit for "+1...", 2 digits for
    # "+44...", 3 digits otherwise. Combined with the next 3 area-code-like
    # digits and the last 4 digits, this keeps enough for grep-correlation
    # without disclosing the full number.
    digits = value[1:]
    if digits.startswith("1"):
        cc_len = 1
    elif digits[0] in "23456789" and len(digits) >= 10:
        # rough split — many countries have 2-digit CC
        cc_len = 2
    else:
        cc_len = 3
    cc = digits[:cc_len]
    area = digits[cc_len:cc_len + 3]
    last4 = digits[-4:]
    return f"+{cc}{area}***{last4}"


def _mask_token(value: str) -> str:
    """Long opaque tokens (JWTs, API keys) collapse to "...<last 12>".
    Mirrors the suffix convention already used in
    billing/telegram_revenue_alerts.py for Agent-Identity tokens."""
    if len(value) <= 12:
        return "***"
    return f"...{value[-12:]}"


def mask_pii(value: str | None) -> str:
    """Best-effort mask: dispatch by simple shape detection.

    The helper is intentionally forgiving — anything that doesn't look like
    an email, phone, or long token returns a generic "***" so a misuse
    never leaks the raw value.
    """
    if value is None:
        return "<empty>"
    s = str(value).strip()
    if not s:
        return "<empty>"
    if _E164_RE.match(s):
        return _mask_phone(s)
    if _EMAIL_RE.match(s):
        return _mask_email(s)
    if len(s) > 20:
        return _mask_token(s)
    return "***"


# Convenience aliases preserve call-site intent (callers know which class of
# identifier they hold) and avoid one extra pattern-match round.
def mask_email(value: str | None) -> str:
    if not value:
        return "<empty>"
    return _mask_email(value)


def mask_phone(value: str | None) -> str:
    if not value:
        return "<empty>"
    return _mask_phone(value)


def mask_token(value: str | None) -> str:
    if not value:
        return "<empty>"
    return _mask_token(value)
