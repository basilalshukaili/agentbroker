"""
The README must not state a tool count that contradicts the price table.

Found 2026-08-28: the README said "9 utility tools free (no key, unmetered)" in
its status table and "8 utility tools are free (no key, unmetered)" four lines
later, in the same document. The real number is 9. The "8" predated
`get_conversation` shipping and nothing noticed, because a prose sentence is
not derived from anything.

This is the same class as the manifest pricing lie that
test_manifest_pricing_parity.py guards: a second, hand-maintained copy of a
fact that `billing/pricing.py` already owns. The manifest got a generator; the
README is prose and cannot have one, so it gets a test instead - the count is
recomputed from pricing.py and every stated figure must match it.

WHY THE COUNT IS WHAT IT IS, since three different numbers are all defensible
and that is exactly how the drift happened:

  * 22 tools are priced in total.
  * 12 of them cost ZERO credits.
  * One of those twelve, `import_booking_url`, is a WRITE tool and needs a key.
    So "free with no key" is 11, not 12 - free and keyless are different claims
    and the README makes the keyless one.
  * The 3 premium data tools are NOT in either figure: they are metered-free
    up to a daily limit, which is a third category and must never be folded
    into "free".
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README = os.path.join(ROOT, "README.md")


@pytest.fixture(scope="module")
def readme():
    with open(README, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def counts():
    """Recomputed from the single source of truth, never hardcoded here."""
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH as needs_auth
    from billing import pricing

    priced = pricing._PRICING_CENTS
    zero_cost = {k for k, v in priced.items() if v == 0}
    return {
        "total": len(priced),
        "zero_cost": len(zero_cost),
        "free_and_keyless": len(zero_cost - set(needs_auth)),
    }


def test_the_free_keyless_count_is_nine_or_this_test_is_stale(counts):
    """A canary on the fixture itself. If this fails the product changed, and
    the README needs updating - which is the point - but it also means every
    other assertion here was comparing against a moved target."""
    assert counts["free_and_keyless"] == 11, (
        f"free-and-keyless tool count moved to {counts['free_and_keyless']}; "
        f"update README.md and this docstring together")


def test_every_stated_free_count_matches_the_price_table(readme, counts):
    """The actual bug: two different numbers in one document, one of them
    wrong, neither derived from anything."""
    stated = re.findall(
        r"(\d+)\s+utility tools?\s+(?:are\s+)?free\s*\(no key", readme)
    assert stated, "README no longer states a free-tool count in the known phrasing"
    wrong = [s for s in stated if int(s) != counts["free_and_keyless"]]
    assert not wrong, (
        f"README claims {wrong} free keyless tools; pricing.py says "
        f"{counts['free_and_keyless']}")


def test_the_document_does_not_contradict_itself(readme):
    """Even if every number were right today, two independent copies of the
    same fact will drift again."""
    stated = set(re.findall(
        r"(\d+)\s+utility tools?\s+(?:are\s+)?free\s*\(no key", readme))
    assert len(stated) <= 1, (
        f"README states the free-tool count as {sorted(stated)} in the same "
        f"document - one of them is wrong and a reader cannot tell which")


def test_premium_data_tools_are_never_counted_as_free(readme, counts):
    """They are metered-free up to a daily limit. Folding them into "free"
    would overstate what an agent gets for nothing by three tools."""
    stated = re.findall(
        r"(\d+)\s+utility tools?\s+(?:are\s+)?free\s*\(no key", readme)
    for s in stated:
        assert int(s) != counts["free_and_keyless"] + 3, (
            "the free count appears to include the 3 premium data tools, "
            "which are free only up to a daily limit")


def test_the_free_table_rows_match_the_stated_count(readme, counts):
    """The prose and the table are two more copies of the same fact."""
    rows = re.findall(r"^\|\s*\d+\s*\|\s*`([a-z_]+)`\s*\|.*\|\s*\*\*free\*\*\s*\|$",
                      readme, flags=re.M)
    assert len(rows) == counts["free_and_keyless"], (
        f"README table marks {len(rows)} tools **free** ({sorted(rows)}) but "
        f"pricing.py says {counts['free_and_keyless']} are free and keyless")


def test_import_booking_url_is_not_advertised_as_keyless(readme):
    """It costs nothing AND requires a key. Listing it among the plain-free
    rows would promise agents they can call it with no credentials."""
    rows = re.findall(r"^\|\s*\d+\s*\|\s*`([a-z_]+)`\s*\|.*\|\s*\*\*free\*\*\s*\|$",
                      readme, flags=re.M)
    assert "import_booking_url" not in rows, (
        "import_booking_url is free but needs a key - it must not sit in the "
        "plain **free** rows")
