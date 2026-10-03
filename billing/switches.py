"""billing/switches.py -- the three money switches, read ONE way, by the gates AND by what we advertise.

THE RULE THIS FILE EXISTS TO ENFORCE. Whatever we tell an agent about how it can pay
(the discovery descriptor, the agent card, tool descriptions, the auth_required text, the
key-request guidance, /.well-known/x402) is a pure function of the switch that decides
whether the gate runs. Never a constant, never a second copy of the test.

WHY. The same defect shipped three times in two months, in both directions:

  * the descriptor said `"rails": ["credits"]` and "crypto is built and switched off"
    while the x402 rail was ON and answering with complete payment offers;
  * the auth_required message told every anonymous caller of a key-required tool
    "Option 3: attach an x402 payment ... USDC on Base" while the rail was OFF
    (X402_ENABLED is not set in the VPS container), so a payment attached there was
    silently ignored;
  * /.well-known/mcp.json said `payments.status = "active"`, `rails = ["credits"]`
    while CREDITS_ENABLED and DATA_METERING_ENABLED were both "false" in the running
    container: no call was charged, no credit was granted for a purchase, and the
    premium-data daily quota did not exist.

A claim written as a constant cannot follow a runtime flag. Each of the three was a
literal that was true on the day somebody typed it.

HOW. This module is the only place that reads CREDITS_ENABLED and DATA_METERING_ENABLED.
The gates in agent_interface/mcp_server.py, billing/polar_webhook.py, core/preview_cost.py
and main.py call these functions, and so do the advertisers, so "the gate is on" and "we
say it is on" are the same expression. tests/unit/test_billing_switches_are_the_only_readers.py
fails if any module reads either variable directly again.

x402 is not re-implemented here: its gate (billing.x402_gate.enabled()) also needs a
receiver address and both CDP credentials, and that function stays the one definition.

TRUTHINESS is unchanged from every call site this replaced: "1", "true" or "yes",
compared case-insensitively, read at CALL time (not import time) so a test or an
operator changing the environment is seen on the next request.

DEPLOYS. A switch that is absent from the container is OFF, which is indistinguishable
from a switch somebody forgot. scripts/check_deploy_env.py (parent repo) therefore
stages all three names explicitly, with an explicit value, so a deploy can never
silently drop one.
"""
from __future__ import annotations

import os

# The environment variable names, exported so the deploy checker and the tests
# can refer to them without typing them a third time.
CREDITS_VAR = "CREDITS_ENABLED"
DATA_METERING_VAR = "DATA_METERING_ENABLED"
X402_VAR = "X402_ENABLED"
# A literal tuple on purpose: scripts/check_deploy_env.py (parent repo) compares it with the names it
# stages by reading this file's AST, and ast.literal_eval cannot follow a reference to a constant.
SWITCH_VARS = ("X402_ENABLED", "CREDITS_ENABLED", "DATA_METERING_ENABLED")

_TRUTHY = ("1", "true", "yes")


def _flag(name: str) -> bool:
    return os.getenv(name, "").lower() in _TRUTHY


def credits_enabled() -> bool:
    """Is the credits ledger charging calls and crediting purchases?

    Off: paid tools are not metered, and a Polar purchase mints a key but grants no
    credits (billing/polar_webhook.py skips the grant). Nothing is charged.
    """
    return _flag(CREDITS_VAR)


def data_metering_enabled() -> bool:
    """Is the daily free quota on the three premium data tools enforced?

    Off: verify_company_record, screen_sanctions and map_trade_restriction run free and
    unmetered for everyone (the bypass in agent_interface/mcp_server.py), and
    preview_cost reports $0.00 for them.
    """
    return _flag(DATA_METERING_VAR)


def x402_enabled() -> bool:
    """Is the pay-per-call USDC rail accepting payment? The gate's own answer."""
    try:
        from billing import x402_gate
        return bool(x402_gate.enabled())
    except Exception:  # noqa: BLE001 - a status read must never fail the page that shows it
        return False


def live_rails() -> list[str]:
    """The payment rails that are switched on right now, in a stable order."""
    rails: list[str] = []
    if credits_enabled():
        rails.append("credits")
    if x402_enabled():
        rails.append("x402")
    return rails


def payments_status() -> str:
    """"active" when at least one rail is on, otherwise "not_enabled"."""
    return "active" if live_rails() else "not_enabled"


# What a price means while nothing can charge it. One sentence, one place: the tool tags, the cost
# sentences in llms.txt / openai-tools / anthropic-tools and preview_cost all use it, so they cannot
# word it three ways. It is only ever added to a price that is not zero.
NOT_CHARGED = "not charged while no payment rail is on"


def charging_active() -> bool:
    """Can ANY call be charged right now? True when at least one payment rail is on.

    Off: every price on every surface is a schedule, not a charge. The dollar figures stay (they are
    what applies once a rail is switched on, and several tests and the x402 gate read them); what
    changes is that nothing says they are being charged.
    """
    return bool(live_rails())


def not_charged_note() -> str:
    """NOT_CHARGED while no rail is on, otherwise "". Safe to splice into any price sentence."""
    return "" if charging_active() else NOT_CHARGED
