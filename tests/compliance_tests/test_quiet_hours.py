"""
TCPA quiet hours - the rule we advertised and did not enforce.

Every public surface says "TCPA compliance built in". TCPA restricts telephone
solicitation to 8am-9pm in the RECIPIENT'S local time (47 CFR 64.1200(c)(1)),
with $500-$1,500 statutory damages per message. A repo-wide search of
compliance/ on 2026-08-26 found no hour, no timezone, no calling window - only
timestamps. demand_shaping.py listed quiet hours as a designed layer marked
"deferred, not dropped", and it had stayed deferred.

The tests that matter here are the boundaries and the two directions of
failure, not the happy path.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from compliance.quiet_hours import check, SOLICITATION_TYPES


def _utc(h, m=0, day=26):
    return datetime(2026, 8, day, h, m, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# What is gated at all
# ---------------------------------------------------------------------------

def test_transactional_is_never_gated():
    """A booking confirmation at 10pm is what the customer asked for. Blocking
    it would break the product to satisfy a rule that does not govern it."""
    for hour in range(0, 24):
        d = check("transactional", "US", "CA", _utc(hour))
        assert d.allowed, f"transactional blocked at {hour}:00 UTC"
        assert d.reason == "not_solicitation"


def test_marketing_and_follow_up_are_both_solicitation():
    assert "marketing" in SOLICITATION_TYPES
    assert "follow_up" in SOLICITATION_TYPES
    # and follow_up really is gated, not just listed
    assert not check("follow_up", "US", "CA", _utc(11)).allowed   # 03:00 PT


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def test_midday_is_allowed():
    d = check("marketing", "US", "CA", _utc(20))     # 12:00 PT
    assert d.allowed and d.local_time == "12:00"


def test_the_small_hours_are_blocked():
    d = check("marketing", "US", "CA", _utc(11))     # 03:00 PT
    assert not d.allowed
    assert d.reason == "outside_permitted_hours"
    assert d.local_time == "03:00"


def test_blocked_result_says_when_to_retry():
    """An agent needs to know how long to wait, not merely that it failed."""
    d = check("marketing", "US", "CA", _utc(11))     # 03:00 PT
    assert d.retry_after_s and d.retry_after_s > 0
    assert d.retry_after_s < 24 * 3600


def test_a_stricter_state_rule_wins():
    """Florida ends at 8pm, not the federal 9pm."""
    fl = check("marketing", "US", "FL", _utc(1, day=27))   # 20:00 ET
    assert not fl.allowed, "20:00 is outside Florida's 8am-8pm window"


def test_dst_margin_is_conservative_not_precise():
    """We do not model DST. Rather than pretend to precision, the window is
    narrowed an hour at each end - being an hour early is free, being an hour
    late is a statutory violation."""
    d = check("marketing", "US", "CA", _utc(16))     # 08:00 PT, federal start
    assert not d.allowed, "08:00 should fall inside the DST safety margin"
    assert check("marketing", "US", "CA", _utc(17)).allowed   # 09:00 PT


# ---------------------------------------------------------------------------
# The two failure directions
# ---------------------------------------------------------------------------

def test_unknown_local_time_blocks_marketing():
    """Marketing is never urgent, so 'we cannot tell what time it is there'
    resolves to 'wait', not 'send anyway'."""
    d = check("marketing", "ZZ", None, _utc(12))
    assert not d.allowed
    assert d.reason == "local_time_unknown"


def test_us_without_a_state_refuses_to_guess():
    """The US spans six zones. A country-level guess there is meaningless, and
    a meaningless guess in this direction is a violation."""
    d = check("marketing", "US", None, _utc(20))
    assert not d.allowed and d.reason == "local_time_unknown"


def test_unknown_local_time_does_NOT_block_transactional():
    """The conservative default must not leak into traffic it does not govern."""
    assert check("transactional", "ZZ", None, _utc(3)).allowed


def test_single_timezone_country_resolves():
    d = check("marketing", "OM", None, _utc(7))      # 11:00 Oman
    assert d.allowed and d.local_time == "11:00"


def test_never_raises_on_junk_input():
    for args in (("marketing", None, None), ("", "US", "CA"),
                 ("marketing", "", ""), (None, None, None)):
        check(*args)      # must not raise


# ---------------------------------------------------------------------------
# Wired into the gate every messaging path already passes through
# ---------------------------------------------------------------------------

def test_pre_check_blocks_a_3am_marketing_sms(monkeypatch):
    from core.models import ComplianceViolationError
    import compliance.pre_check as pc
    import compliance.quiet_hours as qh

    # freeze "now" at 03:00 PT
    monkeypatch.setattr(qh, "check",
                        lambda mt, cc=None, sc=None, now_utc=None:
                        qh.__dict__["_orig_check"](mt, cc, sc, _utc(11)))
    qh.__dict__.setdefault("_orig_check", qh.check)

    with pytest.raises(ComplianceViolationError) as ei:
        pc.pre_check(recipient_id="+14155551234", channel="sms",
                     message_type="marketing", content="Special offer today",
                     country_code="US", state_code="CA", preview=True)
    assert ei.value.rule in ("TCPA_quiet_hours", "TCPA_marketing_consent")


def test_quiet_hours_never_blocks_transactional_in_the_gate():
    """Asserts the RIGHT thing: that quiet hours did not fire.

    A transactional US SMS still meets the 10DLC campaign-registration gate,
    which is a separate and legitimate check. Requiring "no exception at all"
    would make this test about 10DLC rather than about quiet hours - it would
    fail for a reason it is not testing, which is how a test stops meaning
    anything.
    """
    from core.models import ComplianceViolationError
    import compliance.pre_check as pc
    try:
        pc.pre_check(recipient_id="+14155551234", channel="sms",
                     message_type="transactional",
                     content="Your booking at 3pm is confirmed.",
                     country_code="US", state_code="CA", preview=True)
    except ComplianceViolationError as exc:
        assert exc.rule != "TCPA_quiet_hours", (
            "transactional must never be blocked by quiet hours")
