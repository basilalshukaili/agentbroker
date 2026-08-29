"""
A tool description is a promise. These pin the four that were not true.

Found 2026-08-29 by an external product review that used the live endpoint the
way a prospect would. Each of these read well and each was false:

  verify_business   "Performs a live capability probe against the business's
                     channel"  -> an in-memory dictionary lookup,
                     verification_method: "directory_lookup", latency_ms: 0.
  self_test         "verifies... each claimed operation is reachable"
                     -> runs 6 checks, for 20 tools, and probes none of them.
  send_transactional_confirmation
                    "Guaranteed delivery via redundant channels"
                     -> channels can return channel_not_configured.
  find_business     "only curated, verified, transactable businesses", with
                     Tokyo and London in the examples -> 36 rows, mostly
                     [DEMO] seeds with .example addresses, nothing in Tokyo,
                     and the human_message called a DEMO row "1 verified
                     businesses".

THE PATTERN IS ALWAYS THE SAME: the DATA was honest - is_demo flags, [DEMO]
name prefixes, verification_method saying directory_lookup, sandbox notices -
and the PROSE around it was not. This company's cardinal rule is never to
advertise a capability we do not have, and the descriptions are exactly where
that rule keeps getting broken, because prose is not executed and nothing
checks it.

These tests execute it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
    MANIFEST = json.load(fh)
OPS = {o["name"]: o for o in MANIFEST.get("operations", [])}


def desc(op: str) -> str:
    return OPS[op].get("description", "")


# --------------------------------------------------------------------------
# Words that assert a capability we do not have
# --------------------------------------------------------------------------

def test_verify_business_does_not_claim_to_contact_the_business():
    """It reads a directory. Claiming a live probe invites an agent to treat
    a stale record as a fresh confirmation that the business is trading."""
    d = desc("verify_business").lower()
    assert "live capability probe" not in d
    assert "directory lookup" in d, (
        "verify_business must say what it actually does")


def test_self_test_does_not_claim_to_probe_every_operation():
    """It runs 6 checks. There are 20 tools. An integrator reading "each
    claimed operation is reachable" would take a green self_test as proof that
    all 20 work."""
    d = desc("self_test").lower()
    assert "each claimed operation is reachable" not in d
    assert "does not probe each tool" in d


def test_nothing_promises_guaranteed_delivery():
    """We cannot guarantee delivery on any channel, and one of them can return
    channel_not_configured. "Guaranteed" is the single most expensive word
    available to a messaging product."""
    for name, op in OPS.items():
        assert "guaranteed delivery" not in op.get("description", "").lower(), (
            f"{name} promises guaranteed delivery")


def test_find_business_admits_the_network_is_small_and_partly_sample_data():
    """The directory is 36 rows and mostly [DEMO] seeds. Describing it as
    "only curated, verified, transactable businesses" is the overclaim; the
    is_demo flag beside it was always honest."""
    d = desc("find_business")
    assert "curated, verified, transactable" not in d
    assert "[DEMO]" in d and "is_demo" in d, (
        "find_business must tell the caller how to spot sample data")


@pytest.mark.parametrize("op", sorted(OPS))
def test_no_tool_advertises_a_superlative_it_has_not_measured(op):
    """A competitive claim in a description ends up quoted onto public
    listings. profiles.py carried "No competitor in the registry lets a
    stranger verify them for free" - which was simply false, and was caught
    only because someone went and looked."""
    d = desc(op).lower()
    for phrase in ("no competitor", "the only ", "best-in-class", "unmatched",
                   "fastest", "most accurate"):
        assert phrase not in d, f"{op} makes an unmeasured claim: {phrase!r}"


# --------------------------------------------------------------------------
# The message a caller reads, not just the description it may not read
# --------------------------------------------------------------------------

def test_sample_results_are_named_as_sample_in_the_human_message():
    """`find_business` returned "[DEMO] Cuts & Co." under the sentence "Found
    1 verified businesses." An agent reads the sentence."""
    src = open(os.path.join(ROOT, "core", "find_business.py"),
               encoding="utf-8").read()
    assert "verified businesses." not in src, (
        "find_business still calls its results 'verified businesses' - it "
        "cannot, while the directory is mostly sample data")
    assert "is_demo" in src, (
        "the human_message does not distinguish sample data from real results")


# ---------------------------------------------------------------------------
# THE SURFACE AGENTS ACTUALLY READ
# ---------------------------------------------------------------------------
#
# Everything above checks manifest/manifest.json - the ORIGIN's descriptions.
# That file was honest and these tests passed, while the canonical endpoint
# every agent connects to served the opposite:
#
#     find_business    origin: says the network is small and partly sample data
#                      edge:   "curated, verified, transactable businesses"
#     verify_business  origin: "directory lookup"
#                      edge:   "Performs a live capability probe"
#
# hatchloop.dev serves initialize and tools/list from EMBEDDED SNAPSHOTS in the
# Cloudflare worker. They are generated copies, and a generated copy that
# nothing re-checks is just a second source of truth with a longer fuse. A
# buyer evaluating us read the stale one, because it is the only one on the
# marketing page.
#
# So the honesty rules are now applied to the snapshots too. If they drift,
# the build fails and `python scripts/refresh_edge_snapshots.py` is the fix.

_SNAPSHOT_FILES = [
    os.path.join(ROOT, "edge", "src", "snapshots", n)
    for n in ("mcp-tools-list.json", "agents.json", "openai-tools.json",
              "anthropic-tools.json", "manifest.json")
]


def _snapshot_descriptions():
    """(file, tool_name, description) for every tool in every snapshot."""
    out = []
    for path in _SNAPSHOT_FILES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            try:
                blob = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
        stack, seen = [blob], 0
        while stack and seen < 5000:
            node = stack.pop()
            seen += 1
            if isinstance(node, dict):
                nm, ds = node.get("name"), node.get("description")
                if isinstance(nm, str) and isinstance(ds, str):
                    out.append((os.path.basename(path), nm, ds))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return out


SNAPSHOT_DESCRIPTIONS = _snapshot_descriptions()


def test_the_snapshots_were_actually_read():
    """Guard the guard: an empty list would pass every test below."""
    assert len(SNAPSHOT_DESCRIPTIONS) >= 20, (
        f"only found {len(SNAPSHOT_DESCRIPTIONS)} snapshot description(s) - "
        f"the collector is broken, and the tests below prove nothing")


@pytest.mark.parametrize("banned,why", [
    ("curated, verified, transactable",
     "the supply network is small and largely [DEMO] sample data"),
    ("live capability probe",
     "verify_business does a directory lookup; it contacts nobody"),
    ("guaranteed delivery",
     "no channel we operate guarantees delivery"),
])
def test_snapshots_do_not_carry_retired_overclaims(banned, why):
    hits = [f"{f}:{n}" for f, n, d in SNAPSHOT_DESCRIPTIONS if banned in d]
    assert not hits, (
        f"{hits} still claim {banned!r} - {why}. The origin manifest was "
        f"corrected but the snapshot was not, so hatchloop.dev serves the old "
        f"text to every agent. Run scripts/refresh_edge_snapshots.py.")


def test_snapshot_sanctions_claim_matches_the_origin():
    """The conditional in the sanctions description must survive the copy.

    OpenSanctions is queried only when a key is configured. Production has no
    key, so an unconditional "queries OpenSanctions ... 40+ lists" tells an EU
    buyer their obligation was met by a screen that never touched an EU list.
    """
    for f, n, d in SNAPSHOT_DESCRIPTIONS:
        if n != "screen_sanctions":
            continue
        assert "ONLY when" in d or "only when" in d, (
            f"{f} describes OpenSanctions as unconditional. It is not: without "
            f"a configured key the screen is OFAC SDN alone.")
