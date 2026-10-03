"""scripts/live_verify_release.py: after a deploy, the payment claims must agree with each other.

The live check cannot see the container's switches from outside, but it can see every claim the server
makes about them, and those must be one story: if the descriptor says no rail is on, no other surface
may offer one; if it says x402 is on, /.well-known/x402 must exist. `payments_problems` is the pure
judgement behind the `payments` check, tested here against the 2026-10-03 live state (descriptor
"active"/["credits"] while every switch was off) and against consistent states in both directions.
"""
from __future__ import annotations

import os
import sys

AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import live_verify_release as L  # noqa: E402


def _state(**over):
    """Everything off, every surface agreeing: the honest 2026-10-04 state."""
    s = {
        "descriptor": {"status": "not_enabled", "rails": [], "premium_data_quota_enforced": False,
                       "note": "No payment rail is switched on at this time"},
        "x402_status": 404,
        "tools_text": "find_business [free, no key] screen_sanctions [free in quota, then $0.02/call]",
        "auth_text": "auth_required ... Option 1 (free): get a verified free key",
        "keys_text": "Email hello@hatchloop.dev for a key provisioned by hand",
        "screen_sanctions_cost_usd": 0.0,
    }
    s.update(over)
    return s


def test_the_honest_all_off_state_has_no_problems():
    assert L.payments_problems(**_state()) == []


def test_the_2026_10_03_live_state_is_caught():
    """Descriptor 'active' with the credits rail while the container had every switch off: the credits
    rail is named, but no surface actually offers or charges credits, and metering reads as off."""
    bad = _state(descriptor={"status": "active", "rails": ["credits"], "premium_data_quota_enforced": None})
    problems = L.payments_problems(**bad)
    assert problems, "the defect this release fixes was not noticed"
    assert any("credits" in p for p in problems)


def test_status_must_agree_with_rails():
    assert L.payments_problems(**_state(descriptor={"status": "active", "rails": [],
                                                    "premium_data_quota_enforced": False}))
    assert L.payments_problems(**_state(descriptor={"status": "not_enabled", "rails": ["credits"],
                                                    "premium_data_quota_enforced": False}))


def test_x402_is_offered_nowhere_when_it_is_not_a_rail():
    for field, value in (("tools_text", "send_message [or pay per call: x402, USDC on Base]"),
                         ("auth_text", "Option 2 (pay per call, no signup): attach an x402 payment"),
                         ("keys_text", "Pay per call with x402 (USDC on Base)"),
                         ("x402_status", 200)):
        problems = L.payments_problems(**_state(**{field: value}))
        assert problems and any("x402" in p for p in problems), field


def test_x402_as_a_rail_needs_its_discovery_document_and_its_text():
    on = {"status": "active", "rails": ["x402"], "premium_data_quota_enforced": False}
    ok = _state(descriptor=on, x402_status=200,
                tools_text="send_message [or pay per call: x402, USDC on Base]",
                auth_text="Option 2 (pay per call, no signup): attach an x402 payment",
                keys_text="Pay per call with x402 (USDC on Base)")
    assert L.payments_problems(**ok) == []
    assert L.payments_problems(**{**ok, "x402_status": 404}), "rail on but /.well-known/x402 is missing"
    assert L.payments_problems(**{**ok, "auth_text": "auth_required Option 1 (free)"}), (
        "rail on but the error text does not offer it")


def test_credits_text_follows_the_credits_rail():
    off_but_offered = _state(auth_text="Option 2 (credits): buy a credit package")
    assert any("credits" in p for p in L.payments_problems(**off_but_offered))
    on = {"status": "active", "rails": ["credits"], "premium_data_quota_enforced": False}
    assert L.payments_problems(**_state(descriptor=on, auth_text="Option 2 (credits): buy a credit package")) == []
    assert L.payments_problems(**_state(descriptor=on)), "rail on but the error text never offers credits"


def test_preview_cost_must_match_whether_the_quota_is_enforced():
    # metering off -> the three premium tools cost nothing, and the descriptor says so
    assert L.payments_problems(**_state(screen_sanctions_cost_usd=0.02))
    # metering on -> they carry the price, and the descriptor says so
    on = {"status": "not_enabled", "rails": [], "premium_data_quota_enforced": True}
    assert L.payments_problems(**_state(descriptor=on, screen_sanctions_cost_usd=0.02)) == []
    assert L.payments_problems(**_state(descriptor=on, screen_sanctions_cost_usd=0.0))


def test_a_missing_field_is_a_problem_not_a_pass():
    bad = _state(descriptor={"status": "not_enabled", "rails": []})
    assert L.payments_problems(**bad), "premium_data_quota_enforced is part of the contract"
    assert L.payments_problems(**_state(descriptor={})), "an empty descriptor must not pass"


def test_agreement_is_not_truth_so_the_expected_rails_can_be_asserted():
    """The 2026-10-03 state: every surface agreed on 'credits' while CREDITS_ENABLED was off. Only the
    operator's expectation (what was staged) can catch that."""
    credits_everywhere = _state(
        descriptor={"status": "active", "rails": ["credits"], "premium_data_quota_enforced": False},
        auth_text="Option 2 (credits): buy a credit package")
    assert L.payments_problems(**credits_everywhere) == [], "internally consistent"
    problems = L.payments_problems(**credits_everywhere, expect_rails=[])
    assert problems and any("expected" in p for p in problems)
    assert L.payments_problems(**credits_everywhere, expect_rails=["credits"]) == []
    assert L.payments_problems(**_state(), expect_rails=[]) == []


def test_the_expect_rails_option_is_wired_through_main(monkeypatch):
    seen = {}
    monkeypatch.setitem(L.CHECKS, "payments", lambda ctx: seen.update(ctx) or L.check(True))
    assert L.main(["--expect-commit", "x", "--only", "payments", "--expect-rails", ""]) == 0
    assert seen["expect_rails"] == []
    assert L.main(["--expect-commit", "x", "--only", "payments", "--expect-rails", "credits, x402"]) == 0
    assert seen["expect_rails"] == ["credits", "x402"]
    assert L.main(["--expect-commit", "x", "--only", "payments"]) == 0
    assert seen["expect_rails"] is None


def test_the_check_is_registered():
    assert "payments" in L.CHECKS
