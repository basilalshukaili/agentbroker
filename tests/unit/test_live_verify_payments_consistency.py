"""scripts/live_verify_release.py: after a deploy, the payment claims must agree with each other.

The live check cannot see the container's switches from outside, but it can see every claim the server
makes about them, and those must be one story: if the descriptor says no rail is on, no other surface
may offer one; if it says x402 is on, /.well-known/x402 must exist. `payments_problems` is the pure
judgement behind the `payments` check, tested here against the 2026-10-03 live state (descriptor
"active"/["credits"] while every switch was off) and against consistent states in both directions.

TWO RULES THE FIRST VERSION BROKE (review of feat/x402-honesty-20261004, F1 and F6):

  * Evidence that cannot be read is a problem, never a pass. A 500 from /keys/request, a failed tools/list,
    an unreadable preview_cost or a /.well-known/x402 that answered 500 each used to turn into an empty
    string or None and match "nothing offers a rail", so a broken surface passed the gate the deploy relies
    on. Every piece of evidence now has to be positively present.
  * The tool descriptions are judged per tool, against the descriptor. The old fixture listed the quota tag
    "[free in quota, then $0.02/call]" as the honest all-off state, so the contradiction the live server had
    (a quota promised while the descriptor said it is not enforced) could not be seen.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import live_verify_release as L  # noqa: E402

NOT_CHARGED = "not charged while no payment rail is on"
OFF = {"status": "not_enabled", "rails": [], "premium_data_quota_enforced": False,
       "note": "No payment rail is switched on at this time"}


def _tools(**over):
    """tools/list as the server answers it with every switch off: the three premium data tools are free, the
    priced ones carry the schedule price AND say it is not charged."""
    t = {
        "find_business": "Search. [free, no key]",
        "screen_sanctions": "Screen a name. [free, no key]",
        "verify_company_record": "Verify. [free, no key]",
        "map_trade_restriction": "Map. [free, no key]",
        "capture_lead": f"Capture. [$0.05/per_call, {NOT_CHARGED}]",
        "send_message": f"Send. [from $0.02/call, variable, {NOT_CHARGED}]",
    }
    t.update(over)
    return t


def _state(**over):
    """Everything off, every surface agreeing: the honest 2026-10-04 state."""
    s = {
        "descriptor": dict(OFF),
        "x402_status": 404,
        "tools_status": 200,
        "tool_descriptions": _tools(),
        "auth_text": "auth_required ... Option 1 (free): get a verified free key",
        "keys_status": 200,
        "keys_text": "Email hello@hatchloop.dev for a key provisioned by hand",
        "screen_sanctions_cost_usd": 0.0,
    }
    s.update(over)
    return s


def _rail(*rails, quota=False):
    return {"status": "active" if rails else "not_enabled", "rails": list(rails),
            "premium_data_quota_enforced": quota}


def _problems(**over):
    return L.payments_problems(**_state(**over))


def test_the_honest_all_off_state_has_no_problems():
    assert _problems() == []


def test_the_2026_10_03_live_state_is_caught():
    """Descriptor 'active' with the credits rail while the container had every switch off: the credits
    rail is named, but no surface actually offers or charges credits, and metering reads as off."""
    problems = _problems(descriptor={"status": "active", "rails": ["credits"], "premium_data_quota_enforced": None})
    assert problems, "the defect this release fixes was not noticed"
    assert any("credits" in p for p in problems)


def test_status_must_agree_with_rails():
    assert _problems(descriptor={"status": "active", "rails": [], "premium_data_quota_enforced": False})
    assert _problems(descriptor={"status": "not_enabled", "rails": ["credits"], "premium_data_quota_enforced": False})


# ---------------------------------------------------------------- x402

def test_x402_is_offered_nowhere_when_it_is_not_a_rail():
    for field, value in (("tool_descriptions", _tools(send_message="Send. [or pay per call: x402, USDC on Base]")),
                         ("auth_text", "Option 1 (free) Option 2 (pay per call, no signup): attach an x402 payment"),
                         ("keys_text", "Pay per call with x402 (USDC on Base)"),
                         ("x402_status", 200)):
        problems = _problems(**{field: value})
        assert problems and any("x402" in p for p in problems), field


def _x402_on_state(**over):
    """x402 is the one live rail, metering off: the three data tools must NOT carry the mention."""
    tools = _tools(send_message=f"Send. [from $0.02/call, variable] [or pay per call: x402, USDC on Base]",
                   capture_lead="Capture. [$0.05/per_call] [or pay per call: x402, USDC on Base]")
    s = _state(descriptor=_rail("x402"), x402_status=200, tool_descriptions=tools,
               auth_text="Option 1 (free) Option 2 (pay per call, no signup): attach an x402 payment",
               keys_text="Pay per call with x402 (USDC on Base)")
    s.update(over)
    return s


def test_x402_as_a_rail_needs_its_discovery_document_and_its_text():
    assert L.payments_problems(**_x402_on_state()) == []
    assert L.payments_problems(**_x402_on_state(x402_status=404)), "rail on but /.well-known/x402 is missing"
    assert L.payments_problems(**_x402_on_state(auth_text="auth_required Option 1 (free)")), (
        "rail on but the error text does not offer it")


def test_x402_on_metering_off_the_data_tools_must_not_offer_the_rail():
    """Review F3: the data-tool bypass answers them free before the x402 branch, so the mention invites a
    payment that is never read. The check has to see it per tool."""
    bad = _tools(send_message="Send. [or pay per call: x402, USDC on Base]",
                 capture_lead="Capture. [or pay per call: x402, USDC on Base]",
                 screen_sanctions="Screen. [free, no key] [or pay per call: x402, USDC on Base]")
    problems = L.payments_problems(**_x402_on_state(tool_descriptions=bad))
    assert any("screen_sanctions" in p and "x402" in p for p in problems), problems


def test_x402_on_metering_on_the_data_tools_must_offer_it():
    quota = "Screen. [free in quota, then $0.02/call] [or pay per call: x402, USDC on Base]"
    tools = _tools(send_message="Send. [from $0.02/call, variable] [or pay per call: x402, USDC on Base]",
                   capture_lead="Capture. [$0.05/per_call] [or pay per call: x402, USDC on Base]",
                   screen_sanctions=quota, verify_company_record=quota, map_trade_restriction=quota)
    ok = _x402_on_state(descriptor=_rail("x402", quota=True), tool_descriptions=tools, screen_sanctions_cost_usd=0.02)
    assert L.payments_problems(**ok) == []
    tools2 = dict(tools, screen_sanctions="Screen. [free in quota, then $0.02/call]")
    assert any("screen_sanctions" in p for p in L.payments_problems(**dict(ok, tool_descriptions=tools2)))


# ---------------------------------------------------------------- credits

def test_credits_text_follows_the_credits_rail():
    off_but_offered = _problems(auth_text="Option 1 (free) Option 2 (credits): buy a credit package")
    assert any("credits" in p for p in off_but_offered)
    on = _rail("credits")
    plain = _tools(capture_lead="Capture. [$0.05/per_call]", send_message="Send. [from $0.02/call, variable]")
    base = dict(descriptor=on, tool_descriptions=plain)
    assert L.payments_problems(**_state(**base, auth_text="Option 1 (free) Option 2 (credits): buy a credit package")) == []
    assert L.payments_problems(**_state(**base)), "rail on but the error text never offers credits"


# ---------------------------------------------------------------- the quota and the data tools

def test_preview_cost_must_match_whether_the_quota_is_enforced():
    # metering off -> the three premium tools cost nothing, and the descriptor says so
    assert _problems(screen_sanctions_cost_usd=0.02)
    # metering on -> they carry the price, and the descriptor says so
    quota = "[free in quota, then $0.02/call, " + NOT_CHARGED + "]"
    on = dict(descriptor=_rail(quota=True),
              tool_descriptions=_tools(screen_sanctions="S. " + quota, verify_company_record="V. " + quota,
                                       map_trade_restriction="M. " + quota))
    assert _problems(**on, screen_sanctions_cost_usd=0.02) == []
    assert _problems(**on, screen_sanctions_cost_usd=0.0)


def test_a_quota_tag_while_the_quota_is_not_enforced_is_caught():
    """THE FINDING (F1). The old fixture called '[free in quota, then $0.02/call]' the honest all-off state."""
    bad = _tools(screen_sanctions="Screen. [free in quota, then $0.02/call]")
    problems = _problems(tool_descriptions=bad)
    assert any("screen_sanctions" in p and "quota" in p for p in problems), problems
    # and the other direction: the quota is enforced, the tool says it is simply free
    on = dict(descriptor=_rail(quota=True), screen_sanctions_cost_usd=0.02)
    assert any("screen_sanctions" in p and "quota" in p for p in _problems(**on)), "enforced but untagged"


def test_a_priced_tool_quoted_as_charged_while_no_rail_is_on_is_caught():
    problems = _problems(tool_descriptions=_tools(capture_lead="Capture. [$0.05/per_call]"))
    assert any("capture_lead" in p and "charged" in p for p in problems), problems
    # and the opposite: a rail is on but the tool still says it is not charged
    on = dict(descriptor=_rail("credits"), auth_text="Option 1 (free) Option 2 (credits): buy a credit package",
              tool_descriptions=_tools(capture_lead=f"Capture. [$0.05/per_call, {NOT_CHARGED}]",
                                       send_message="Send. [from $0.02/call, variable]"))
    assert any("capture_lead" in p and "charged" in p for p in _problems(**on)), _problems(**on)


def test_a_missing_field_is_a_problem_not_a_pass():
    bad = _state(descriptor={"status": "not_enabled", "rails": []})
    assert L.payments_problems(**bad), "premium_data_quota_enforced is part of the contract"
    assert _problems(descriptor={}), "an empty descriptor must not pass"


# ---------------------------------------------------------------- evidence that cannot be read is a problem

@pytest.mark.parametrize("field,value,needle", [
    ("x402_status", 500, "x402"),
    ("x402_status", 0, "x402"),
    ("tools_status", 500, "tools/list"),
    ("tool_descriptions", {}, "tools/list"),
    ("keys_status", 503, "/keys/request"),
    ("keys_text", "", "/keys/request"),
    ("auth_text", "", "auth_required"),
    ("auth_text", "some unrelated body", "auth_required"),
    ("screen_sanctions_cost_usd", None, "preview_cost"),
])
def test_unreadable_evidence_is_a_problem(field, value, needle):
    """THE FINDING (F6). Each of these used to evaluate to 'nothing offers a rail' and pass."""
    problems = _problems(**{field: value})
    assert problems, f"{field}={value!r} passed"
    assert any(needle in p for p in problems), problems


def test_a_rail_that_is_off_must_answer_404_not_just_not_200():
    assert any("x402" in p for p in _problems(x402_status=500))
    assert any("x402" in p for p in _problems(x402_status=302))
    assert _problems(x402_status=404) == []


def test_agreement_is_not_truth_so_the_expected_rails_can_be_asserted():
    """The 2026-10-03 state: every surface agreed on 'credits' while CREDITS_ENABLED was off. Only the
    operator's expectation (what was staged) can catch that."""
    plain = _tools(capture_lead="Capture. [$0.05/per_call]", send_message="Send. [from $0.02/call, variable]")
    credits_everywhere = _state(descriptor=_rail("credits"), tool_descriptions=plain,
                                auth_text="Option 1 (free) Option 2 (credits): buy a credit package")
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


# ---------------------------------------------------------------- the wiring: check_payments with a stubbed network

class _Server:
    """Stands in for http() and rpc(): answers each URL the check reads, with overrides per surface."""

    def __init__(self, **over):
        self.over = over
        self.calls = []

    def http(self, method, url, **kw):
        self.calls.append((method, url))
        path = url.split("api.example", 1)[1]
        if path in self.over:
            return self.over[path]
        if path == "/.well-known/mcp.json":
            return 200, {}, json.dumps({"payments": OFF})
        if path == "/.well-known/x402":
            return 404, {}, "{}"
        if path == "/keys/request":
            return 200, {}, json.dumps({"no_email_available": {"alternative": "Email hello@hatchloop.dev"}})
        raise AssertionError(f"unexpected GET {path}")

    def rpc(self, url, method, params=None, **kw):
        self.calls.append((method, (params or {}).get("name", "")))
        key = method if method != "tools/call" else "tools/call:" + (params or {}).get("name", "")
        if key in self.over:
            return self.over[key]
        if method == "tools/list":
            tools = [{"name": n, "description": d} for n, d in _tools().items()]
            return 200, {}, {"result": {"tools": tools}}
        if key == "tools/call:send_message":
            body = {"error_code": "auth_required", "human_message": "Option 1 (free): get a key",
                    "how_to_resolve": {"free_key": "x"}}
            return 200, {}, {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}
        if key == "tools/call:preview_cost":
            body = {"estimated_cost_usd": 0.0}
            return 200, {}, {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}
        raise AssertionError(f"unexpected rpc {key}")


@pytest.fixture
def server(monkeypatch):
    def make(**over):
        s = _Server(**over)
        monkeypatch.setattr(L, "http", s.http)
        monkeypatch.setattr(L, "rpc", s.rpc)
        return s
    return make


def _check(**ctx):
    return L.check_payments({"base": "https://api.example", **ctx})


def test_check_payments_passes_against_an_honest_stubbed_server(server):
    server()
    res = _check(expect_rails=[])
    assert res["ok"] is True and res["problems"] == [], res
    assert res["premium_data_quota_enforced"] is False and res["x402_discovery_status"] == 404


def test_check_payments_reads_every_surface_it_judges(server):
    s = server()
    _check()
    asked = " ".join(str(c[1]) for c in s.calls)
    for need in ("/.well-known/mcp.json", "/.well-known/x402", "/keys/request", "send_message", "preview_cost"):
        assert need in asked, need
    assert "tools/list" in [c[0] for c in s.calls]


@pytest.mark.parametrize("over,needle", [
    ({"tools/list": (500, {}, None)}, "tools/list"),
    ({"/keys/request": (500, {}, "boom")}, "/keys/request"),
    ({"/.well-known/x402": (500, {}, "boom")}, "x402"),
    ({"tools/call:preview_cost": (200, {}, {"result": {"content": [{"type": "text", "text": "not json"}]}})},
     "preview_cost"),
    ({"tools/call:send_message": (500, {}, None)}, "auth_required"),
])
def test_a_broken_surface_fails_the_check_instead_of_passing_it(server, over, needle):
    server(**over)
    res = _check()
    assert res["ok"] is False, res
    assert any(needle in p for p in res["problems"]), res["problems"]


def test_a_dead_descriptor_fails_the_check(server):
    server(**{"/.well-known/mcp.json": (503, {}, "down")})
    assert _check()["ok"] is False


def test_check_payments_catches_the_live_defect_end_to_end(server):
    """The 48e8b62 behaviour as the stub: the quota tag the descriptor contradicts, and a priced tool
    quoted as charged. This is what the deploy gate has to refuse."""
    tools = [{"name": n, "description": d} for n, d in _tools(
        screen_sanctions="Screen. [free in quota, then $0.02/call]",
        capture_lead="Capture. [$0.05/per_call]").items()]
    server(**{"tools/list": (200, {}, {"result": {"tools": tools}})})
    res = _check(expect_rails=[])
    assert res["ok"] is False
    joined = " | ".join(res["problems"])
    assert "screen_sanctions" in joined and "capture_lead" in joined


def test_the_report_says_whether_the_committed_edge_snapshot_is_in_step(server):
    """Review F9: edge/src/snapshots/mcp.json freezes the payments block. The edge worker is not in the live
    path today, but if it is ever deployed without a fresh KV overlay it serves that block. The check reports
    whether the live descriptor and the committed snapshot agree on status, rails and the quota flag, so a
    switch flipped without regenerating the snapshot is visible in the receipt (it does not fail the deploy:
    the origin, not the snapshot, is what callers reach)."""
    server()
    assert _check(expect_rails=[])["edge_snapshot_payments_in_step"] is True
    server(**{"/.well-known/mcp.json": (200, {}, json.dumps({"payments": _rail("credits", quota=True)}))})
    res = _check()
    assert res["edge_snapshot_payments_in_step"] is False
    assert res["ok"] is False or not any("snapshot" in p for p in res["problems"]), "informational, not a gate"
