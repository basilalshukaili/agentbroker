"""scripts/check_pricing.py reads generator SOURCE as a syntax tree, so a respelling cannot hide a literal rail.

THE DEFECT (third review of feat/x402-honesty-20261004, P3, reproduced by two reviewers). The rail-literal
rule was a set of line regexes, so the original defect passed under trivial respellings. Each of these was
verified as MISSED on the previous version of the checker:

    {'rails': ['credits']}              single-quoted dict key
    "status": f"active"                 an f-string with no fields
    status = "active"                   a plain assignment
    rails = list(("credits",))          a literal wrapped in a call
    rails = DEFAULT_RAILS               an alias of a constant defined elsewhere
    rails.append("credits")             built up after the fact
    r = ["credits"]; {"rails": r}       a variable fed into the rails key

The tree check refuses all of them, while the derived code that replaced the defect stays clean. Every case is
its own test, so deleting any one rule fails exactly the spelling it was written for.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

AB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(AB, "scripts"))

import check_pricing  # noqa: E402


def _problems(source: str) -> list[str]:
    return check_pricing.ast_rail_problems(source, "agent_interface/generated.py")


MISSED_BEFORE = [
    ("single-quoted dict key", "def d():\n    return {'rails': ['credits']}\n"),
    ("double-quoted dict key", 'def d():\n    return {"rails": ["credits"]}\n'),
    ("f-string status", 'def d():\n    return {"status": f"active"}\n'),
    ("plain status", 'def d():\n    status = "active"\n    return {"status": status}\n'),
    ("status key literal", "def d():\n    return {'status': 'active'}\n"),
    ("literal wrapped in a call", 'def d():\n    rails = list(("credits",))\n    return rails\n'),
    ("alias of a constant", 'DEFAULT_RAILS = ["credits"]\n\ndef d():\n    rails = DEFAULT_RAILS\n    return rails\n'),
    ("appended afterwards", 'def d():\n    rails = []\n    rails.append("credits")\n    return rails\n'),
    # derived, THEN a hand-written rail added: only the grow rule can see this one
    ("appended to a derived list", 'def d():\n    rails = switches.live_rails()\n    rails.append("x402")\n    return rails\n'),
    # a derived call with a hand-written rail inside the same expression is still a typed claim
    ("derived plus hand-written", 'def d():\n    rails = switches.live_rails() + ["x402"]\n    return rails\n'),
    ("derived plus hand-written, in the key", 'def d():\n    return {"rails": switches.live_rails() + ["x402"]}\n'),
    # only the assignment rule can see these two (nothing reads the variable)
    ("status assigned alone", 'def d():\n    payments_status = "active"\n    return None\n'),
    ("status assigned, other literal", 'def d():\n    status = "not_enabled"\n    return None\n'),
    ("extended afterwards", 'def d():\n    rails = []\n    rails.extend(["credits", "x402"])\n    return rails\n'),
    ("variable fed into the rails key", 'def d():\n    r = ["credits"]\n    return {"rails": r}\n'),
    ("concatenation", 'def d(x):\n    rails = ["credits"] + (["x402"] if x else [])\n    return rails\n'),
    ("augmented", 'def d():\n    rails = switches.live_rails()\n    rails += ["x402"]\n    return rails\n'),
    ("annotated", 'def d():\n    rails: list = ["credits"]\n    return rails\n'),
    ("attribute target", 'def d(self):\n    self.payment_rails = ("credits",)\n'),
    ("literal not_enabled status", 'def d():\n    return {"status": "not_enabled", "rails": []}\n'),
]

DERIVED = [
    ("the descriptor as written",
     "from billing import switches\n\ndef d():\n    rails = switches.live_rails()\n"
     "    return {'status': switches.payments_status(), 'rails': rails}\n"),
    ("rails passed through", "def d(rails):\n    return {'rails': rails}\n"),
    ("a different status", "def d():\n    return {'status': 'see_pricing'}\n"),
    ("status derived", "def d(s):\n    status = s.compute()\n    return {'status': status}\n"),
    ("unrelated lists", "def d():\n    names = ['credits', 'x402']\n    return {'accepted_words': names}\n"),
    ("derived then read", "def d():\n    from billing import switches\n    rails = switches.live_rails()\n"
                          "    x402_live = 'x402' in rails\n    return x402_live\n"),
    ("append of a derived value", "def d(r):\n    out = []\n    out.append(r.name)\n    return out\n"),
]


@pytest.mark.parametrize("label,source", MISSED_BEFORE, ids=[m[0] for m in MISSED_BEFORE])
def test_every_respelling_of_a_literal_rail_is_refused(label, source):
    assert _problems(source), f"not caught: {label}"


@pytest.mark.parametrize("label,source", DERIVED, ids=[m[0] for m in DERIVED])
def test_derived_code_is_not_flagged(label, source):
    assert _problems(source) == [], f"false positive: {label}: {_problems(source)}"


def test_a_file_that_does_not_parse_is_a_problem_not_a_pass():
    problems = _problems("def broken(:\n")
    assert problems and "parse" in problems[0].lower(), problems


def test_the_single_quoted_dict_key_is_also_a_line_regex_hit():
    """The cheap line rule keeps working for the non-Python generators and for a quick grep."""
    line = "        {'rails': ['credits'],"
    assert any(re.search(pat, line) for pat, _ in check_pricing._RAIL_ASSERTIONS)


def test_check_rail_claims_applies_the_tree_rule_to_every_python_generator(tmp_path, monkeypatch):
    gen = tmp_path / "agent_interface"
    gen.mkdir()
    (gen / "well_known.py").write_text("def d():\n    r = ['credits']\n    return {'rails': r}\n", encoding="utf-8")
    monkeypatch.setattr(check_pricing, "_AGENTBROKER_DIR", tmp_path)
    monkeypatch.setattr(check_pricing, "_GENERATORS", ["agent_interface/well_known.py"])
    problems = check_pricing.check_rail_claims()
    assert problems and "well_known.py" in problems[0], problems


def test_the_generators_in_this_tree_are_clean_under_the_tree_rule():
    assert check_pricing.check_rail_claims() == []
