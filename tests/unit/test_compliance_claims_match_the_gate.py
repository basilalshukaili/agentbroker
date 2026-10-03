"""What we publish about the compliance gate must be true of the gate.

THE DEFECT (found by the independent review of the D2 fix, 2026-10-04). The send_message tool description, its
compliance constraints, the cookbook an agent reads, the storefront pages and the registry-submission drafts all
said the gate enforces "TCPA, GDPR, CASL, PDPL across 26 jurisdictions", and one constraint said Gulf
recipients are "covered by PDPL-style rules per jurisdiction; the gate routes by country_code". No rule in the
gate implements the Omani, Saudi, Emirati, Qatari, Kuwaiti or Bahraini data-protection law. Every Gulf state is
judged by the service's own conservative opt-in default (compliance/jurisdiction_rules.py: `statutes` is empty
for them), and the answers say so ("No OM-specific consent statute is implemented ... not a citation of OM law").
A description that says the opposite of the answer a caller then receives is the worst kind of wrong: it is read
BEFORE the call, by the agent deciding whether to rely on us for a regulated send.

This house rule is "never advertise a capability we do not have", and prose is where it keeps being broken
because nothing executes it. This test executes it: the word PDPL may appear in text we publish only where it
is a duty of the USER (the terms of service tell a customer to comply with the law of every jurisdiction their
agent reaches), never as something the gate does.

Out of scope on purpose: edge/src/snapshots and edge/dist*, which are GENERATED from the live origin by
scripts/refresh_edge_snapshots.py after a deploy (that script's --check is their drift gate).
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SKIP_DIRS = {".git", "tests", "docs", "edge", "node_modules", "state", "reports", "__pycache__", ".pytest_cache",
             ".ci", ".github", "migrations"}
EXTENSIONS = (".py", ".md", ".json", ".yaml", ".yml", ".txt", ".html")

# Sentences in which PDPL is named as a duty of the customer, not as a rule the gate applies.
ALLOWED_CONTEXTS = (
    "Comply with applicable telecommunications and privacy law in every jurisdiction your agent reaches "
    "(TCPA, GDPR, CASL, PDPL",
    "violates applicable telecommunications, privacy, or consumer-protection law (including TCPA, CAN-SPAM, "
    "GDPR, CASL, PDPL)",
)


def _tracked_text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(EXTENSIONS):
                yield os.path.join(base, name)


def _occurrences():
    for path in _tracked_text_files():
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        flat = re.sub(r"\s+", " ", text)
        for m in re.finditer(r"PDPL", flat):
            window = flat[max(0, m.start() - 160): m.end() + 160]
            yield os.path.relpath(path, ROOT), window


def test_pdpl_is_never_published_as_something_the_gate_applies():
    offenders = []
    for rel, window in _occurrences():
        if not any(re.sub(r"\s+", " ", ok) in window for ok in ALLOWED_CONTEXTS):
            offenders.append(f"{rel}: ...{window}...")
    assert not offenders, "PDPL claimed as a gate rule in:\n" + "\n".join(o[:330] for o in offenders)


def test_the_allowed_contexts_still_exist_so_the_allowance_is_not_stale():
    flat = re.sub(r"\s+", " ", open(os.path.join(ROOT, "web", "pages.py"), encoding="utf-8").read())
    for ok in ALLOWED_CONTEXTS:
        assert re.sub(r"\s+", " ", ok) in flat, ok


def _manifest():
    with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
        return {o["name"]: o for o in json.load(fh)["operations"]}


def test_the_send_message_description_names_the_rules_the_gate_has_and_says_the_rest_is_a_default():
    desc = _manifest()["send_message"]["description"]
    for law in ("TCPA", "GDPR", "CASL"):
        assert law in desc
    assert "PDPL" not in desc
    assert "opt-in default" in desc, "the description must say what every other country is judged by"


def test_the_gulf_constraint_says_no_gulf_statute_is_modeled_and_that_the_number_selects_the_rules():
    constraints = _manifest()["send_message"]["compliance_constraints"]
    gulf = [c for c in constraints if "UAE" in c and "OM" in c]
    assert len(gulf) == 1, constraints
    text = gulf[0]
    assert "PDPL" not in text and "routes by country_code" not in text
    assert "no" in text.lower() and "statute" in text.lower() and "opt-in default" in text
    assert "number" in text.lower(), "the country is read from the recipient's number now"


def test_the_gate_really_does_model_no_gulf_statute():
    """The claim the text above makes, checked against the rules it describes."""
    from compliance.jurisdiction_rules import describe_rule_set
    for country in ("AE", "SA", "OM", "QA", "KW", "BH"):
        d = describe_rule_set(country)
        assert d["basis"] == "conservative_default" and d["statutes_modeled"] == [], country
    for country, statute in (("US", "TCPA"), ("DE", "GDPR"), ("GB", "GDPR"), ("CA", "CASL")):
        assert statute in describe_rule_set(country)["statutes_modeled"], country


def test_the_generated_tool_catalogue_carries_the_corrected_text():
    with open(os.path.join(ROOT, "manifest", "mcp_tools.json"), encoding="utf-8") as fh:
        tools = {t["name"]: t for t in json.load(fh)}
    assert "PDPL" not in tools["send_message"]["description"]
    assert "opt-in default" in tools["send_message"]["description"]


def test_the_cookbook_an_agent_reads_does_not_claim_pdpl():
    from agent_interface import mcp_server as ms
    import asyncio
    resp = asyncio.run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, {}, None))
    uris = [r["uri"] for r in resp["result"]["resources"]]
    for uri in uris:
        body = asyncio.run(ms.handle_mcp_request(
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}}, {}, None))
        assert "PDPL" not in json.dumps(body), uri


def test_the_check_compliance_output_schema_documents_the_undecided_answer():
    out = _manifest()["check_compliance"]["output_schema"]["properties"]
    assert "undecided" in out["rule_set"]["properties"]["basis"]["enum"]
    assert "rule jurisdiction_conflict" in out["jurisdiction_conflict"]["description"]
    assert out["rule_set"]["properties"]["code"]["type"] == ["string", "null"]
