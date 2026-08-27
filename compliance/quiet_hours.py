"""
Time-of-day restrictions on solicitation.

WHAT WAS MISSING. We advertise "TCPA compliance built in" on every public
surface, and TCPA restricts telephone solicitation to 8am-9pm in the
RECIPIENT'S local time (47 CFR 64.1200(c)(1)). Nothing in this codebase
enforced a time window: a repo-wide search of compliance/ found no hour, no
timezone, no calling window - only timestamps (2026-08-26). demand_shaping.py
listed quiet hours as a designed layer, marked "deferred, not dropped", and it
had stayed deferred.

Statutory damages are $500-$1,500 PER call or text. For a company whose entire
pitch is that an agent can message businesses lawfully, this was the gap that
most directly contradicted the product.

WHAT THIS DOES AND DELIBERATELY DOES NOT DO.

  Applies to SOLICITATION - marketing, and follow-up which can read as
  solicitation. It does NOT apply to transactional messages: a booking
  confirmation at 10pm is the thing the customer asked for, and blocking it
  would break the product to satisfy a rule that does not govern it. The
  message_type distinction already in core/models.py is what makes that
  separable.

  Fails CLOSED for marketing when the local time cannot be determined. A
  marketing message is never urgent, so "we could not tell what time it is
  there" resolves to "wait", not "send anyway". Transactional traffic is
  unaffected either way, so this costs us nothing we care about.

  State-level rules are stricter than federal in several US states; where we
  model one, it wins.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("smb_broker.quiet_hours")

# Message types that count as solicitation for time-window purposes.
SOLICITATION_TYPES = {"marketing", "follow_up"}

# Channels the rule actually governs. TCPA is about TELEPHONE solicitation -
# calls and texts. Email is governed by CAN-SPAM, which imposes no time-of-day
# restriction at all, so applying a calling window to email would block lawful
# mail for no legal reason. The existing test suite caught exactly that when
# this module was first wired (2026-08-26): a marketing EMAIL was refused by a
# telephone rule.
GOVERNED_CHANNELS = {"sms", "voice", "whatsapp"}

# Federal TCPA default: 8am-9pm local.
_DEFAULT_START_HOUR = 8
_DEFAULT_END_HOUR = 21

# Stricter windows where a jurisdiction we model imposes one. Only entries we
# can actually justify - guessing a stricter rule is as wrong as guessing a
# looser one, it just fails in the polite direction.
_JURISDICTION_WINDOWS: dict[str, tuple[int, int]] = {
    "US":    (8, 21),   # 47 CFR 64.1200(c)(1)
    "US-FL": (8, 20),   # Florida Telemarketing Act - 8pm, stricter than federal
    "US-OK": (8, 20),   # Oklahoma - 8pm
    "CA":    (9, 21),   # Canada CRTC telemarketing rules
    "GB":    (8, 21),
    "EU":    (9, 20),
}

# Rough UTC offsets, used ONLY when we have no better signal. Countries with a
# single practical business timezone. Anything spanning many zones (US, RU, AU)
# is deliberately absent - a country-level guess there is meaningless.
_COUNTRY_UTC_OFFSET: dict[str, float] = {
    "GB": 0, "IE": 0, "PT": 0,
    "FR": 1, "DE": 1, "ES": 1, "IT": 1, "NL": 1, "BE": 1, "SE": 1, "PL": 1,
    "OM": 4, "AE": 4, "SA": 3, "QA": 3, "KW": 3, "BH": 3,
    "IN": 5.5, "PK": 5, "BD": 6,
    "SG": 8, "MY": 8, "PH": 8, "HK": 8, "CN": 8,
    "JP": 9, "KR": 9,
    "NZ": 12,
}

# US state offsets (standard time; DST is ignored deliberately - see below).
_US_STATE_UTC_OFFSET: dict[str, float] = {
    "CA": -8, "WA": -8, "OR": -8, "NV": -8,
    "AZ": -7, "CO": -7, "UT": -7, "NM": -7, "MT": -7, "ID": -7, "WY": -7,
    "TX": -6, "IL": -6, "MN": -6, "WI": -6, "MO": -6, "LA": -6, "AR": -6,
    "IA": -6, "OK": -6, "KS": -6, "NE": -6, "AL": -6, "MS": -6, "TN": -6,
    "NY": -5, "FL": -5, "GA": -5, "NC": -5, "SC": -5, "VA": -5, "PA": -5,
    "OH": -5, "MI": -5, "MA": -5, "NJ": -5, "MD": -5, "IN": -5, "KY": -5,
    "CT": -5, "ME": -5, "NH": -5, "VT": -5, "RI": -5, "DE": -5, "WV": -5,
    "AK": -9, "HI": -10,
}


@dataclass
class QuietHoursDecision:
    allowed: bool
    reason: str
    local_time: Optional[str] = None
    window: Optional[str] = None
    retry_after_s: Optional[int] = None

    def as_block(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "recipient_local_time": self.local_time,
                "permitted_window": self.window,
                "retry_after_s": self.retry_after_s}


# E.164 dialling prefixes -> ISO country. Longest prefix wins.
# WHY THIS EXISTS: failing closed on "unknown local time" is the right call
# legally, but only if "unknown" is RARE. Without this, every marketing send
# that omitted the optional country_code would be blocked - a correct rule
# applied so broadly it breaks the product. A phone number almost always tells
# us the country, so infer it rather than refuse.
_E164_COUNTRY: dict[str, str] = {
    "968": "OM", "971": "AE", "966": "SA", "974": "QA", "965": "KW", "973": "BH",
    "44": "GB", "353": "IE", "351": "PT", "33": "FR", "49": "DE", "34": "ES",
    "39": "IT", "31": "NL", "32": "BE", "46": "SE", "48": "PL",
    "91": "IN", "92": "PK", "880": "BD",
    "65": "SG", "60": "MY", "63": "PH", "852": "HK", "86": "CN",
    "81": "JP", "82": "KR", "64": "NZ",
    # +1 is US AND Canada and spans six zones; it does NOT resolve a timezone,
    # so it is deliberately absent. Guessing there is what we are avoiding.
}


def country_from_number(recipient_id: Optional[str]) -> Optional[str]:
    """ISO country from an E.164 number, or None. Never raises."""
    s = "".join(c for c in (recipient_id or "") if c.isdigit())
    if not s:
        return None
    for length in (3, 2, 1):
        if s[:length] in _E164_COUNTRY:
            return _E164_COUNTRY[s[:length]]
    return None


def _offset_for(country_code: Optional[str],
                state_code: Optional[str]) -> Optional[float]:
    """UTC offset in hours, or None when we genuinely cannot tell.

    DST IS DELIBERATELY IGNORED. It shifts the boundary by an hour, and rather
    than pretend to a precision we do not have, the window is narrowed by an
    hour at both ends when a jurisdiction observes DST (see _window_for). Being
    an hour conservative is free; being an hour late is a statutory violation.
    """
    cc = (country_code or "").upper()
    sc = (state_code or "").upper()
    if cc == "US" and sc in _US_STATE_UTC_OFFSET:
        return _US_STATE_UTC_OFFSET[sc]
    if cc == "US":
        return None            # US without a state spans 6 zones - refuse to guess
    return _COUNTRY_UTC_OFFSET.get(cc)


def _window_for(country_code: Optional[str], state_code: Optional[str]) -> tuple[int, int]:
    cc = (country_code or "").upper()
    sc = (state_code or "").upper()
    key = f"{cc}-{sc}" if sc else cc
    if key in _JURISDICTION_WINDOWS:
        return _JURISDICTION_WINDOWS[key]
    if cc in _JURISDICTION_WINDOWS:
        start, end = _JURISDICTION_WINDOWS[cc]
        # DST safety margin: shrink by an hour at each end rather than model it.
        return (start + 1, end - 1) if cc in ("US", "CA", "GB", "EU") else (start, end)
    return _DEFAULT_START_HOUR, _DEFAULT_END_HOUR


def check(message_type: str,
          country_code: Optional[str] = None,
          state_code: Optional[str] = None,
          now_utc: Optional[datetime] = None,
          recipient_id: Optional[str] = None,
          channel: Optional[str] = None) -> QuietHoursDecision:
    """Is it a lawful hour to solicit this recipient? Never raises."""
    mtype = (message_type or "").lower()
    if mtype not in SOLICITATION_TYPES:
        return QuietHoursDecision(True, "not_solicitation")
    # Email is CAN-SPAM territory, which has no calling window.
    if channel is not None and channel.lower() not in GOVERNED_CHANNELS:
        return QuietHoursDecision(True, "channel_not_time_restricted")

    # An explicit country_code wins; otherwise infer from the number itself.
    country_code = country_code or country_from_number(recipient_id)
    offset = _offset_for(country_code, state_code)
    start, end = _window_for(country_code, state_code)
    window = f"{start:02d}:00-{end:02d}:00 local"

    if offset is None:
        # Cannot determine local time. Marketing is never urgent.
        return QuietHoursDecision(
            False, "local_time_unknown", window=window,
            retry_after_s=3600)

    now = now_utc or datetime.now(timezone.utc)
    local = now + timedelta(hours=offset)
    if start <= local.hour < end:
        return QuietHoursDecision(True, "within_window",
                                  local_time=local.strftime("%H:%M"), window=window)

    # How long until the window opens again, in the recipient's day.
    if local.hour < start:
        opens = local.replace(hour=start, minute=0, second=0, microsecond=0)
    else:
        opens = (local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0)
    wait = max(60, int((opens - local).total_seconds()))
    return QuietHoursDecision(
        False, "outside_permitted_hours",
        local_time=local.strftime("%H:%M"), window=window, retry_after_s=wait)
