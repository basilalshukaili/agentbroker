"""
Fail open, but never silently.

check_budget FAILS OPEN when the conversations ledger is unreachable, and that
is the right call - a throttle that breaks delivery is worse than the flood it
prevents. The defect was the SILENCE: `_recent_threads` returned `[]` both when
a business was genuinely quiet AND when the read failed, so the caller could not
tell them apart. A ledger outage switched the entire protection layer off and
looked exactly like a calm day (independent review 2026-08-26, confirmed and
fixed 2026-08-27).

These tests pin the distinction, because it is the whole fix.
"""
from __future__ import annotations

import asyncio

import pytest

from core import demand_shaping as ds


@pytest.fixture(autouse=True)
def clear_counter():
    ds._FAIL_OPEN.clear()
    yield
    ds._FAIL_OPEN.clear()


def _boom(*a, **k):
    async def _f(*_a, **_k):
        raise RuntimeError("supabase unreachable")
    return _f()


def test_quiet_business_and_broken_ledger_are_different(monkeypatch):
    """The core of it: both used to look like an empty list."""
    import storage.supabase_client as sb

    # genuinely quiet
    async def _empty(*a, **k):
        return []
    monkeypatch.setattr(sb, "select_rows", _empty)
    quiet = asyncio.run(ds.check_budget("biz_1"))
    assert quiet.allowed and not quiet.degraded
    assert quiet.reason_code == "within_budget"
    assert ds.fail_open_count() == 0

    # ledger down
    monkeypatch.setattr(sb, "select_rows", lambda *a, **k: _boom())
    blind = asyncio.run(ds.check_budget("biz_1"))
    assert blind.allowed, "must STILL fail open - delivery beats throttling"
    assert blind.degraded is True
    assert blind.reason_code == "shaping_degraded"
    assert ds.fail_open_count() == 1


def test_the_receipt_admits_it_decided_blind(monkeypatch):
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows", lambda *a, **k: _boom())
    block = asyncio.run(ds.check_budget("biz_1")).as_receipt_block()
    assert block["degraded"] is True
    assert "unreachable" in block["degraded_reason"]


def test_a_healthy_check_does_not_carry_the_degraded_flag(monkeypatch):
    """The flag must mean something - it cannot be present on every receipt."""
    import storage.supabase_client as sb

    async def _empty(*a, **k):
        return []
    monkeypatch.setattr(sb, "select_rows", _empty)
    assert "degraded" not in asyncio.run(ds.check_budget("biz_1")).as_receipt_block()


def test_counter_is_windowed(monkeypatch):
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows", lambda *a, **k: _boom())
    asyncio.run(ds.check_budget("biz_1"))
    assert ds.fail_open_count(3600) == 1
    # an event older than the window is not counted
    ds._FAIL_OPEN[0] -= 7200
    assert ds.fail_open_count(3600) == 0


def test_counter_is_bounded(monkeypatch):
    """A signal, not a log - it must not grow without limit under a long outage."""
    for _ in range(ds._FAIL_OPEN_MAX + 50):
        ds._record_fail_open()
    assert len(ds._FAIL_OPEN) <= ds._FAIL_OPEN_MAX


def test_no_business_id_is_not_a_fail_open(monkeypatch):
    """Skipping the check because there is nothing to check is not degradation."""
    d = asyncio.run(ds.check_budget(None))
    assert d.allowed and not d.degraded
    assert ds.fail_open_count() == 0


def test_health_check_escalates_with_volume(monkeypatch):
    """One blind check is a blip; forty means the layer is off."""
    import sys, os, importlib
    # system_health.py lives in the WORKSPACE root (…/ai company/scripts), which
    # is one level above the agentbroker package - not inside it.
    here = os.path.abspath(__file__)
    workspace = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    scripts = os.path.join(workspace, "scripts")
    if not os.path.isdir(scripts):
        import pytest as _pt
        _pt.skip("workspace scripts/ not present in this checkout")
    sys.path.insert(0, scripts)
    sh = importlib.import_module("system_health")

    ds._FAIL_OPEN.clear()
    assert sh.check_shaping_degraded()["status"] == "ok"

    for _ in range(3):
        ds._record_fail_open()
    assert sh.check_shaping_degraded()["status"] == "warn"

    for _ in range(20):
        ds._record_fail_open()
    r = sh.check_shaping_degraded()
    assert r["status"] == "fail"
    assert "effectively OFF" in r["detail"]
