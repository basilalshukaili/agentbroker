"""A partial revocation load must never latch as complete.

is_customer_revoked() answers from an in-memory set hydrated once per process.
The loader read one page of 5000, logged an error if it got exactly 5000, and
then set the "hydrated" latch anyway - so every revocation past that boundary
was never loaded, never retried, and those refunded customers kept paid access
until the next redeploy.

The loud log line is what made it look handled. It is the same defect the
function's own comment describes ("latch only on SUCCESS"), one level down:
truncation is not success.
"""
from __future__ import annotations

import pytest

from agent_interface import identity as I


@pytest.fixture(autouse=True)
def _clean():
    I._revoked_customer_ids.clear()
    I._revocation_hydrated = False
    I._revocation_next_try = 0.0
    yield
    I._revoked_customer_ids.clear()
    I._revocation_hydrated = False
    I._revocation_next_try = 0.0


def _rows(n: int, start: int = 0) -> list:
    return [{"customer_id": f"cus_{i}", "status": "revoked"}
            for i in range(start, start + n)]


def _fake_pager(total: int, monkeypatch):
    """Serve `total` revocations through the paged interface."""
    calls = []

    def _sel(table, filters=None, order=None, limit=1000, offset=0, **kw):
        calls.append((limit, offset))
        return _rows(max(0, min(limit, total - offset)), offset)

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_sync_strict", _sel)
    return calls


def test_every_revocation_is_loaded_not_just_the_first_page(monkeypatch):
    """4,300 revocations across five pages. The old single read stopped at
    one page and called it done."""
    _fake_pager(4300, monkeypatch)
    I._hydrate_revocations()
    assert len(I._revoked_customer_ids) == 4300, (
        f"only {len(I._revoked_customer_ids)} of 4300 revocations loaded - the "
        f"rest are refunded customers who still validate")
    assert I.is_customer_revoked("cus_4299") is True, (
        "a revocation past the first page does not revoke")


def test_a_single_page_still_latches(monkeypatch):
    calls = _fake_pager(12, monkeypatch)
    I._hydrate_revocations()
    assert I._revocation_hydrated is True
    assert len(calls) == 1, "a short first page should not ask for a second"
    I._hydrate_revocations()
    assert len(calls) == 1, "hydration ran twice despite the latch"


def test_hitting_the_page_ceiling_does_not_latch(monkeypatch):
    """If there are genuinely more revocations than the loop will read, the
    honest state is 'not hydrated' so the backoff retries - not 'done' with a
    subset, which is what silently granted access before."""
    _fake_pager(10_000_000, monkeypatch)
    I._hydrate_revocations()
    assert I._revocation_hydrated is False, (
        "hydration latched on an admittedly incomplete read")
    # What it did manage to load still counts.
    assert I.is_customer_revoked("cus_0") is True


def test_a_failed_read_does_not_latch(monkeypatch):
    """The case the existing comment is about, kept so the rewrite cannot
    regress it."""
    def _boom(*a, **kw):
        raise RuntimeError("supabase down")

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_sync_strict", _boom)
    I._hydrate_revocations()
    assert I._revocation_hydrated is False
