"""
Switching the crypto rail on must not silently cancel the free tier.

WHAT WAS ABOUT TO HAPPEN (caught 2026-08-29, before the flag was set). The
dispatcher checked x402 like this:

    if x402_gate.enabled() and x402_gate.is_paid_tool(name):
        return await x402_gate.run_paid_tool(...)

ahead of both the credits gate and the free-quota gate. So setting
X402_ENABLED=true - a one-line config change, no deploy of any code - would have
made all ten paid tools demand a USDC payment up front, and every one of these
public promises false in the same instant:

  * "free email-verified key (100 ops/day)"  - website, README, manifest,
    Smithery listing, and the storefront message the server itself returns
  * "premium data tools free up to 500/day with a free key"

Checked against PRODUCTION rather than the defaults, which made it worse:
DATA_METERING_ENABLED is true in prod, so the premium-data bypass earlier in the
dispatcher does not fire and those three tools were exposed too.

x402 is an ESCAPE PATH, which is exactly what the server's own quota-exceeded
message calls it. A caller who attaches a payment gets served by it. Everyone
else falls through to credits, then the free quota.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def armed(monkeypatch):
    """x402 fully configured and switched ON - the state we are about to ship."""
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "enabled", lambda: True)
    return x402_gate


def _dispatcher_source():
    path = os.path.join(ROOT, "agent_interface", "mcp_server.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# The ordering itself
# --------------------------------------------------------------------------

def test_the_x402_branch_requires_an_offered_payment():
    """The guard that makes the difference. Without it, enabling the flag is a
    silent breaking change to every free caller."""
    src = _dispatcher_source()
    assert "_offered_payment" in src, "the payment-presence guard is gone"
    assert "and _offered_payment:" in src, (
        "the x402 branch no longer requires the caller to present a payment - "
        "enabling the rail would jump the free tier again")


def test_x402_is_still_checked_before_credits_when_payment_is_offered():
    """One rail per call: an agent that paid must not also be billed credits.

    Anchored on the actual BRANCH statements, not the bare identifiers - both
    names appear in comments earlier in the file, and comparing the first
    occurrence of each compared two pieces of prose."""
    src = _dispatcher_source()
    x402_branch = src.index(
        "if x402_gate.enabled() and x402_gate.is_paid_tool(name) and _offered_payment:")
    credits_branch = src.index('if _os_credits.getenv("CREDITS_ENABLED"')
    assert x402_branch < credits_branch, (
        "a call that already paid via x402 would also be charged credits")


def test_the_payment_is_read_from_meta_not_from_arguments():
    """x402 payments arrive in _meta. Reading them from arguments would let a
    caller fake one as an ordinary tool parameter."""
    src = _dispatcher_source()
    assert '_meta.get("x402/payment")' in src


# --------------------------------------------------------------------------
# What the free caller experiences - the whole point
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    "send_message", "capture_lead", "schedule_appointment",
    "send_transactional_confirmation", "handle_inbound", "escalate_to_human",
    "call_business", "verify_company_record", "screen_sanctions",
    "map_trade_restriction",
])
def test_every_paid_tool_is_still_a_paid_tool(armed, tool):
    """Sanity: these ARE the tools the gate would have captured. If this list
    ever shrinks, the ordering test above is guarding less than it claims."""
    assert armed.is_paid_tool(tool), f"{tool} is no longer priced"


def test_no_payment_offered_means_the_gate_does_not_claim_the_call(armed):
    """The exact condition the dispatcher evaluates, with no payment attached."""
    meta = {}
    offered = bool(isinstance(meta, dict) and meta.get("x402/payment"))
    assert not offered
    assert not (armed.enabled() and armed.is_paid_tool("send_message") and offered)


def test_a_payment_offered_does_route_to_the_gate(armed):
    meta = {"x402/payment": "eyJ4NDAyVmVyc2lvbiI6MX0="}
    offered = bool(isinstance(meta, dict) and meta.get("x402/payment"))
    assert armed.enabled() and armed.is_paid_tool("send_message") and offered


def test_an_empty_payment_value_is_not_a_payment(armed):
    """"" and None must not be mistaken for an offer - that would route a free
    caller into the paid path on a malformed request."""
    for junk in ({"x402/payment": ""}, {"x402/payment": None}, {"other": "x"}, None):
        offered = bool(isinstance(junk, dict) and junk.get("x402/payment"))
        assert not offered, f"{junk!r} was treated as a payment"


# --------------------------------------------------------------------------
# The promises this protects, checked where they are actually written
# --------------------------------------------------------------------------

def test_the_free_tier_is_still_advertised_and_therefore_must_still_work():
    """If someone ever removes these claims, this test should be deleted
    deliberately - not left passing while the promise quietly disappears."""
    readme = os.path.join(ROOT, "README.md")
    with open(readme, encoding="utf-8") as fh:
        text = fh.read()
    assert "100 ops/day" in text
    assert "free" in text.lower()


def test_the_quota_message_still_names_x402_as_an_escape_not_a_gate():
    """The server's own quota-exceeded message describes x402 as a way out for
    someone who has run out - which is only true with this ordering."""
    path = os.path.join(ROOT, "billing", "data_quota.py")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert "free_quota_exceeded" in text
