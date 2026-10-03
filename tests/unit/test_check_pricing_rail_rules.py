"""scripts/check_pricing.py must refuse every spelling of a hard-coded payment rail.

The 2026-08-29 incident (crypto rail ON, discovery said it did not exist) produced a rule that matched
only the JSON spelling `"rails": [...]`. The generator then wrote the credits rail as
`rails = ["credits"] + (...)` and the status as `"status": "active"`; no rule matched, and the credits
rail stayed a constant until it was false (2026-10-04: CREDITS_ENABLED and DATA_METERING_ENABLED were
"false" in the running container). Each rule is shown to fire on a line of the defect it was added for,
and not to fire on the derived code that replaced it.
"""
from __future__ import annotations

import os
import re
import sys

AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import check_pricing  # noqa: E402


def _hits(line: str) -> list[str]:
    return [why for pat, why in check_pricing._RAIL_ASSERTIONS if re.search(pat, line)]


# The first four are verbatim from agent_interface/well_known.py and agent_interface/mcp_server.py at
# 4e46f8e (and `"rails": ["credits"]` is the spelling the checker was first written for); the last is
# the phrase the third rule has always policed.
HARD_CODED = [
    '    rails = ["credits"] + (["x402"] if x402_live else [])',
    '        "status": "active",',
    '        "rails": ["credits"],',
    '            f"Option 2 (credits): buy a credit package (Starter $9/1,000 credits, Growth $29/3,500, "',
    '            f"Option 3 (pay per call, no signup): attach an x402 payment in "',
    "        \"note\": \"All tools are currently free to call\",",
    # review F8: the two spellings the first version of the rules let through
    '        "To get access: Option 1 (free): get a key. Option 2 (credits): buy a package. "',
    "        'status': 'active',",
]

# The derived code that replaced them.
DERIVED = [
    "    rails = switches.live_rails()",
    '        "status": switches.payments_status(),',
    '        "rails": rails,',
    '        _options_text = "".join(f"Option {i} {text}" for i, text in enumerate(_options, 1))',
    '            f"(pay per call, no signup): attach an x402 payment in "',
    '                f"(credits): buy a credit package (Starter $9/1,000 credits, Growth $29/3,500, "',
]


def test_every_hardcoded_spelling_is_refused():
    for line in HARD_CODED:
        assert _hits(line), f"not caught: {line.strip()}"


def test_the_derived_code_is_not_flagged():
    for line in DERIVED:
        assert _hits(line) == [], f"false positive: {line.strip()} -> {_hits(line)}"


def test_the_generators_in_this_tree_are_clean():
    assert check_pricing.check_rail_claims() == []


def test_the_key_request_guidance_is_a_checked_generator():
    assert "agent_interface/key_requests.py" in check_pricing._GENERATORS
    assert "agent_interface/well_known.py" in check_pricing._GENERATORS
    assert "agent_interface/mcp_server.py" in check_pricing._GENERATORS


def test_every_file_that_writes_a_payment_sentence_is_a_checked_generator():
    """Review F5: the free-key page, the past-quota messages and the consent page offered credits while the
    gate was off and none of them was policed."""
    for rel in ("agent_interface/key_request_logic.py", "billing/data_quota.py",
                "agent_interface/oauth/pages.py", "web/pages.py", "core/preview_cost.py"):
        assert rel in check_pricing._GENERATORS, rel


# Static copy cannot follow a switch, so it may not offer credits or promise a quota at all.
STATIC_BAD = [
    "a free email-verified key gives 100 ops/day, or buy credits at https://hatchloop.dev/pricing",
    "Top up credits at https://hatchloop.dev/pricing",
    "14 of the 23 tools require no auth (11 always-free + 3 free within a daily quota).",
]
STATIC_OK = [
    "a free email-verified key gives 100 ops/day.",
    "Which payment rails and daily quotas are switched on right now is published live in the payments block",
]


def _static_hits(line: str) -> list[str]:
    return [why for pat, why in check_pricing._STATIC_COPY_BANNED if re.search(pat, line)]


def test_static_copy_that_sells_credits_or_promises_a_quota_is_refused():
    for line in STATIC_BAD:
        assert _static_hits(line), f"not caught: {line}"
    for line in STATIC_OK:
        assert _static_hits(line) == [], f"false positive: {line}"


def test_the_static_registry_files_are_checked_and_clean():
    for rel in ("smithery.yaml", "glama.json", "server.json", "registry/servers.yaml"):
        assert rel in check_pricing._STATIC_COPY, rel
    assert check_pricing.check_static_copy() == []


def test_a_static_file_that_regresses_is_reported(tmp_path, monkeypatch):
    bad = tmp_path / "smithery.yaml"
    bad.write_text("auth: a free key, or buy credits at https://hatchloop.dev/pricing\n", encoding="utf-8")
    monkeypatch.setattr(check_pricing, "_AGENTBROKER_DIR", tmp_path)
    monkeypatch.setattr(check_pricing, "_STATIC_COPY", ["smithery.yaml"])
    problems = check_pricing.check_static_copy()
    assert problems and "smithery.yaml:1" in problems[0]


def test_the_free_to_call_reason_does_not_assert_a_date_credits_went_live():
    """The old reason hard-coded 'credits have been live since 2026-08-24' into a tool that fails builds;
    that sentence is itself a constant claim about a switch."""
    reasons = " ".join(why for _pat, why in check_pricing._RAIL_ASSERTIONS)
    assert "2026-08-24" not in reasons
