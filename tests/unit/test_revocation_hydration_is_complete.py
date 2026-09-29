"""A partial revocation load must never latch as complete.

is_customer_revoked() answers from an in-memory set hydrated once per process.
The loader read one page of 5000, logged an error if it got exactly 5000, and
then set the "hydrated" latch anyway - so every revocation past that boundary
was never loaded, never retried, and those refunded customers kept paid access
until the next redeploy.

The loud log line is what made it look handled. It is the same defect the
function's own comment describes ("latch only on SUCCESS"), one level down:
truncation is not success.

AUDIT-2026-09-29: _hydrate_revocations() used to read polar_order_events
directly via storage.supabase_client.select_rows_sync_strict(). In
production (anon key only), RLS is enabled with zero policies and anon still
holds the table SELECT grant, so that call got HTTP 200 with an EMPTY ARRAY
on every page -- not an error -- so the "latch only on success" rule
actually fired and latched hydration complete with ZERO revocations loaded,
forever, even with real revoked rows in the table. It now reads through the
polar_order_events_revoked_customer_ids SECURITY DEFINER RPC
(sql/agentbroker/007_revocation_read_rpc.sql) via
storage.supabase_client.rpc_sync(); every fake here patches `rpc_sync`
instead of `select_rows_sync_strict`, and TestRevocationPermissionDenied
below locks in the new REVOCATION_READ_DENIED behaviour for when the RPC
boundary itself is refused (as opposed to genuinely empty).
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

    def _rpc(fn, payload):
        limit = payload["p_limit"]
        offset = payload["p_offset"]
        calls.append((limit, offset))
        return [{"customer_id": r["customer_id"]}
                for r in _rows(max(0, min(limit, total - offset)), offset)]

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc_sync", _rpc)
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
    monkeypatch.setattr(sb, "rpc_sync", _boom)
    I._hydrate_revocations()
    assert I._revocation_hydrated is False


def test_an_empty_successful_read_latches_with_zero(monkeypatch):
    """A genuinely empty result (nobody revoked yet) is a SUCCESSFUL read and
    must latch -- that is not the bug. The bug this migration fixes is an
    empty-but-200 response coming from an RLS/grant problem being
    indistinguishable from this one; see TestRevocationPermissionDenied for
    the case that must NOT latch."""
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [])
    I._hydrate_revocations()
    assert I._revocation_hydrated is True
    assert len(I._revoked_customer_ids) == 0


def test_hydration_calls_the_rpc_by_name_with_paging_params(monkeypatch):
    """Locks in the RPC boundary itself: the exact function name
    sql/agentbroker/007_revocation_read_rpc.sql defines, called with the
    p_limit/p_offset parameter names that function's signature uses."""
    seen = {}

    def _rpc(fn, payload):
        seen["fn"] = fn
        seen["payload"] = payload
        return [{"customer_id": "cus_seen"}]

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc_sync", _rpc)
    I._hydrate_revocations()

    assert seen["fn"] == "polar_order_events_revoked_customer_ids"
    assert seen["payload"] == {"p_limit": 1000, "p_offset": 0}
    assert I.is_customer_revoked("cus_seen") is True


def test_a_durably_revoked_customer_token_is_rejected_after_hydration(monkeypatch):
    """End-to-end: a customer revoked on a PEER process (durable store only,
    never seen locally -- this process never called revoke_customer() for
    them) must have their token rejected here once hydration reads it via
    the RPC. This is the exact 'survives a restart' scenario
    sql/agentbroker/007_revocation_read_rpc.sql exists for."""
    token_resp = I.issue_subscription_token(
        customer_id="cus_peer_revoked_1", plan="developer",
        customer_email="x@example.com")
    assert I.validate_token(token_resp.token).valid is True

    import storage.supabase_client as sb
    monkeypatch.setattr(
        sb, "rpc_sync",
        lambda fn, payload: [{"customer_id": "cus_peer_revoked_1"}])
    # The sanity call above already ran (and failed) an unconfigured
    # hydration attempt, consuming this process's retry backoff window;
    # force a fresh attempt now that the store is "reachable" rather than
    # waiting out 60s in real time (mirrors
    # test_jti_revocation_durability.py's identical scenario).
    I._revocation_next_try = 0.0

    result = I.validate_token(token_resp.token)
    assert result.valid is False
    assert "revoked" in (result.error or "").lower()


class TestRevocationPermissionDenied:
    """AUDIT-2026-09-29: a 401/403/missing-function RPC failure is not a
    transient outage -- it will not self-heal on the backoff retry, it means
    a human must apply 007 or fix a grant. It must log the stable
    REVOCATION_READ_DENIED marker distinctly from the ordinary
    revocation_hydrate_failed WARNING, and it must NEVER latch (an
    unreadable answer must never be read as 'no customers are revoked')."""

    def test_a_permission_denied_error_does_not_latch_and_logs_the_marker(
        self, monkeypatch, caplog,
    ):
        import storage.supabase_client as sb

        def _denied(fn, payload):
            raise RuntimeError(
                f"rpc_sync({fn!r}) failed: HTTP 401 "
                f'body={{"code":"42501","message":"permission denied"}}')

        monkeypatch.setattr(sb, "rpc_sync", _denied)
        with caplog.at_level("ERROR", logger="smb_broker.identity"):
            I._hydrate_revocations()

        assert I._revocation_hydrated is False, (
            "a permission-denied RPC call must never be read as 'no "
            "customers are revoked'")
        assert any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        ), "a 401 RPC failure must log the stable REVOCATION_READ_DENIED marker"

    def test_a_missing_function_error_does_not_latch_and_logs_the_marker(
        self, monkeypatch, caplog,
    ):
        """404 == PGRST202: the function itself does not exist yet (007 not
        applied). Same bucket as a permission denial."""
        import storage.supabase_client as sb

        def _missing(fn, payload):
            raise RuntimeError(f"rpc_sync({fn!r}) failed: HTTP 404 body={{}}")

        monkeypatch.setattr(sb, "rpc_sync", _missing)
        with caplog.at_level("ERROR", logger="smb_broker.identity"):
            I._hydrate_revocations()

        assert I._revocation_hydrated is False
        assert any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        )

    def test_a_transient_outage_logs_the_ordinary_warning_not_the_marker(
        self, monkeypatch, caplog,
    ):
        """A transport failure/5xx must keep the pre-existing WARNING
        behaviour and must NOT claim REVOCATION_READ_DENIED."""
        import storage.supabase_client as sb

        def _outage(fn, payload):
            raise RuntimeError(f"rpc_sync({fn!r}) transport error: connection refused")

        monkeypatch.setattr(sb, "rpc_sync", _outage)
        with caplog.at_level("WARNING", logger="smb_broker.identity"):
            I._hydrate_revocations()

        assert I._revocation_hydrated is False
        assert not any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        )
        assert any(
            "revocation_hydrate_failed" in r.getMessage() for r in caplog.records
        )
