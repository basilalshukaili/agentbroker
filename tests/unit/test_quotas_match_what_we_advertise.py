"""
The quota we enforce must equal the quota we publish.

WHAT HAPPENED. On 2026-08-26 a deliberate generosity pass raised the
premium-data quotas to 500/day with a free key and 100/day anonymous, and
propagated that to every public surface: the pricing page, the README,
llms-install.md, the skill repo, the directory listings and two open PRs.

It landed in `config.py`. `billing/data_quota.py` - the module that actually
enforces the limit - never read `config.py`. It re-read the same two
environment variables with its own hardcoded defaults, the pre-raise 50 and 20.
So unless someone also set those env vars on the host, production served a
FIFTH of the anonymous allowance we advertised and a TENTH of the keyed one.

It was found by reading the live quota ledger while investigating something
else: anonymous buckets capping at 20 while every public surface said 100.

Under-delivering against your own published numbers is worse than pricing
badly - a customer who counted on the published figure hits a wall we told
them was not there, and the honest-looking response says the limit they were
promised has been reached.

WHY A TEST AND NOT JUST A FIX. The fix is one import. What makes it stay fixed
is this file: the enforced numbers are asserted against the numbers in the
public copy, so raising one and forgetting the other fails the build. That is
the [[propagate-changes-everywhere]] rule, executed instead of remembered.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from billing import data_quota  # noqa: E402


def test_the_enforcer_uses_the_configured_limits():
    """The module that says no must read the same numbers as the one that
    documents yes."""
    assert data_quota._get_anon_limit() == config.ANON_DATA_QUOTA_PER_DAY
    assert data_quota._get_free_limit() == config.FREE_DATA_QUOTA_PER_DAY


def test_the_retired_lower_limits_are_gone():
    """Pin the specific regression.

    50 and 20 are the pre-2026-08-26 values. If either reappears as an
    enforced limit, the generosity pass has been silently reverted - which is
    exactly what had happened, invisibly, for three days.
    """
    assert data_quota._get_anon_limit() != 20, (
        "the anonymous premium-data quota is back to the retired 20/day while "
        "every public surface advertises 100")
    assert data_quota._get_free_limit() != 50, (
        "the free-key premium-data quota is back to the retired 50/day while "
        "every public surface advertises 500")


# The public surfaces that state these numbers. If a number changes, it must
# change in all of them - which is what this asserts, by reading them.
_SURFACES = [
    os.path.join(ROOT, "README.md"),
    os.path.join(ROOT, "llms-install.md"),
]


@pytest.mark.parametrize("path", _SURFACES, ids=[os.path.basename(p) for p in _SURFACES])
def test_public_copy_states_the_enforced_numbers(path):
    """Read the advertised figures back out of the copy and compare.

    Deliberately tolerant about WORDING and strict about the NUMBER: the
    sentence may be rewritten freely, but if it names a per-day figure next to
    'anonymous' it had better be the one we enforce.
    """
    if not os.path.exists(path):
        pytest.skip(f"{path} not present")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    anon = config.ANON_DATA_QUOTA_PER_DAY
    free = config.FREE_DATA_QUOTA_PER_DAY

    # e.g. "500/day with a free key, 100/day anonymous"
    for n, label in re.findall(r"(\d+)\s*/\s*day\s+(anonymous|with a free key)", text):
        n = int(n)
        expected = anon if label == "anonymous" else free
        assert n == expected, (
            f"{os.path.basename(path)} advertises {n}/day {label} but we "
            f"enforce {expected}. One of the two is a promise we are not "
            f"keeping.")


def test_this_file_read_a_surface_that_actually_states_the_numbers():
    """Guard the guard.

    The parametrised test above passes trivially if the regex matches nothing -
    a rewritten sentence would silently end coverage while staying green. At
    least one surface must still state a per-day figure.
    """
    found = 0
    for path in _SURFACES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            found += len(re.findall(r"(\d+)\s*/\s*day\s+(anonymous|with a free key)",
                                    fh.read()))
    assert found > 0, (
        "no public surface states a per-day quota in the expected form any "
        "more - the comparison above is checking nothing. Update the pattern "
        "to match the new wording rather than deleting this test.")
