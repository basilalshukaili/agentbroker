"""SEC enrichment must survive a missing full stop.

verify_company_record returned Apple's real GLEIF record with sec_cik and
ticker BOTH NULL, while listing sec.gov/files/company_tickers.json in
sources_queried - a receipt stating we asked SEC about Apple and SEC had
nothing to say. Apple is obviously in that file.

The match was `entry["title"].lower() == name.lower()` and SEC stores
"Apple Inc." with a trailing period. So the enrichment worked only for a
caller who typed the name character-for-character as SEC stores it.

Found by an outside reviewer using the product as a stranger - which is
exactly the kind of defect that no amount of reading our own code surfaces,
because we all type the name the way the code expects.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.verify_company_record as v  # noqa: E402


def _search(name):
    return asyncio.run(v._edgar_search(name))


@pytest.mark.parametrize("typed", ["Apple Inc", "Apple Inc.", "APPLE INC"])
def test_apple_is_found_however_the_caller_punctuates_it(typed):
    try:
        rec = _search(typed)
    except v.RegistryUnavailable:
        pytest.skip("SEC EDGAR unreachable from this environment")
    assert rec, f"{typed!r} found nothing at SEC"
    assert rec.get("ticker") == "AAPL"
    assert str(rec.get("cik")) == "320193"


def test_a_bare_first_word_is_not_guessed():
    """The same file holds "Apple Hospitality REIT, Inc." and "Pineapple
    Financial Inc.". Attaching one of their CIKs to a company-verification
    receipt because someone typed "Apple" would be worse than finding
    nothing, so the match stays exact-after-normalisation."""
    try:
        rec = _search("Apple")
    except v.RegistryUnavailable:
        pytest.skip("SEC EDGAR unreachable from this environment")
    assert rec is None, (
        f"a bare first word matched {rec!r} - that is a wrong CIK on a "
        f"compliance receipt")
