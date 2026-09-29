"""Single-token revocation (revoke_token()/revoke_jti()) must be durable --
AUDIT-2026-09-28. Before this fix, revoke_token() added a jti to an
in-memory-only set and returned True; nothing wrote it anywhere durable, so
the leaked token it was meant to kill was valid again after the next
process restart or container redeploy, while the caller had been told the
revocation "succeeded". This mirrors test_revocation_hydration_is_complete.py
and test_identity_revocation.py's patching style for the customer-level
path, applied to the jti-level one.

AUDIT-2026-09-29: _hydrate_jti_revocations() used to read the revoked_jtis
TABLE directly via storage.supabase_client.select_rows_sync_strict(). In
production (anon key only) that call gets HTTP 401 -- permission denied --
on every attempt, which the old except-branch could only log as an
endlessly-retried WARNING, so hydration never latched and 0505c23's
durability fix was inert. It now reads through the revoked_jtis_list
SECURITY DEFINER RPC (sql/agentbroker/007_revocation_read_rpc.sql) via
storage.supabase_client.rpc_sync(); every fake here patches `rpc_sync`
instead of `select_rows_sync_strict`, and TestJtiHydrationPermissionDenied
below locks in the new REVOCATION_READ_DENIED behaviour for when the RPC
boundary itself is refused.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_interface import identity as I


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    I._revoked_jtis.clear()
    I._jti_revocation_hydrated = False
    I._jti_revocation_next_try = 0.0
    yield
    I._revoked_jtis.clear()
    I._jti_revocation_hydrated = False
    I._jti_revocation_next_try = 0.0


@pytest.fixture(autouse=True)
def _no_real_supabase_calls(monkeypatch):
    """Every test here patches storage.supabase_client.rpc_sync directly and
    must never fall through to a real network call -- clear the three env
    vars supabase_client.py reads so it can never resolve a real URL/key,
    hermetic even if a patch is missed or ordered wrong."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)


def _issue():
    return I.issue_token(I.TokenRequest(agent_id="agent_x", principal_id="cust_x"))


# ---------------------------------------------------------------------------
# revoke_token() / revoke_jti() honesty
# ---------------------------------------------------------------------------

class TestRevokeTokenReturnValueIsHonest:
    def test_revoke_token_reports_success_when_the_write_lands(self):
        token_resp = _issue()
        with patch(
            "storage.supabase_client.insert_row_strict",
            new_callable=AsyncMock, return_value={"jti": "whatever"},
        ):
            durable = run(I.revoke_token(token_resp.token))
        assert durable is True

    def test_revoke_token_reports_false_when_the_durable_write_fails(self):
        """The write failing must not be reported as success -- the caller
        (an operator killing a leaked token) has to be able to tell the
        revocation is NOT yet safe against a restart."""
        token_resp = _issue()
        with patch(
            "storage.supabase_client.insert_row_strict",
            new_callable=AsyncMock, side_effect=RuntimeError("supabase down"),
        ) as mock_insert:
            durable = run(I.revoke_token(token_resp.token))
        assert mock_insert.await_count == 1
        assert durable is False, (
            "the durable write failed and revoke_token() reported success")

    def test_in_memory_effect_applies_even_when_durable_write_fails(self):
        """Fire-and-forget durability is fine for a metric; it is not fine
        for a revocation. But the IN-MEMORY effect (this process rejects the
        token right now) must still apply even though persistence failed --
        matching revoke_customer()'s contract."""
        token_resp = _issue()
        with patch(
            "storage.supabase_client.insert_row_strict",
            new_callable=AsyncMock, side_effect=RuntimeError("supabase down"),
        ):
            durable = run(I.revoke_token(token_resp.token))
        assert durable is False
        assert I.validate_token(token_resp.token).valid is False

    def test_malformed_token_returns_false_and_touches_nothing(self):
        assert run(I.revoke_token("not-a-real-token")) is False
        assert len(I._revoked_jtis) == 0

    def test_revoke_token_and_validate_token_are_consistent_before_revocation(self):
        token_resp = _issue()
        assert I.validate_token(token_resp.token).valid is True


# ---------------------------------------------------------------------------
# Durability: a jti revoked on ANOTHER process (durable store only, never
# seen locally) must still be honoured here once hydration reads it. This is
# the exact "survives a restart" scenario AUDIT-2026-09-28 describes.
# ---------------------------------------------------------------------------

class TestJtiRevocationSurvivesAcrossProcesses:
    def test_a_durably_revoked_jti_is_honoured_after_hydration(self, monkeypatch):
        token_resp = _issue()
        # Extract this token's jti the same way validate_token() does, to
        # seed the fake durable store with EXACTLY the row a real durable
        # write for this token would have produced -- never assert against
        # a jti we invented ourselves.
        claims = I._verify(token_resp.token)
        jti = claims["jti"]

        # Simulate: this process never called revoke_token() for this
        # token (so _revoked_jtis is empty for it) -- it was revoked by a
        # PEER process, which durably wrote the row. Before hydration runs,
        # this process must not yet know.
        assert I.validate_token(token_resp.token).valid is True

        import storage.supabase_client as sb
        monkeypatch.setattr(
            sb, "rpc_sync",
            lambda fn, payload: [{"jti": jti, "revoked_at": "2026-09-29T00:00:00Z"}],
        )
        # The first (config-less) attempt above already consumed this
        # process's retry backoff window; force a fresh attempt now that
        # the store is "reachable" rather than waiting out 60s in real time.
        I._jti_revocation_next_try = 0.0

        result = I.validate_token(token_resp.token)
        assert result.valid is False
        assert "revoked" in (result.error or "").lower()

    def test_hydration_never_forgets_a_jti_this_process_already_knows(self, monkeypatch):
        """Once a jti is known-revoked (this process called revoke_token()),
        a LATER hydration failure must never make it test as valid again --
        'never let a store outage turn a revoked token into a valid one'."""
        token_resp = _issue()
        with patch(
            "storage.supabase_client.insert_row_strict",
            new_callable=AsyncMock, side_effect=RuntimeError("supabase down"),
        ):
            run(I.revoke_token(token_resp.token))
        assert I.validate_token(token_resp.token).valid is False

        # Force a fresh hydration attempt (bypassing the retry backoff) that
        # also fails -- the outage continuing must not un-revoke anything.
        I._jti_revocation_next_try = 0.0
        import storage.supabase_client as sb
        monkeypatch.setattr(
            sb, "rpc_sync",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("still down")),
        )
        result = I.validate_token(token_resp.token)
        assert result.valid is False, (
            "a jti already known-revoked in this process must survive a "
            "hydration outage, not be forgotten")


# ---------------------------------------------------------------------------
# Hydration pagination/latch discipline -- same shape as
# test_revocation_hydration_is_complete.py, applied to the jti table.
# ---------------------------------------------------------------------------

def _jti_rows(n: int, start: int = 0) -> list:
    return [{"jti": f"jti_{i}"} for i in range(start, start + n)]


def _fake_pager(total: int, monkeypatch):
    calls = []

    def _rpc(fn, payload):
        limit = payload["p_limit"]
        offset = payload["p_offset"]
        calls.append((limit, offset))
        return _jti_rows(max(0, min(limit, total - offset)), offset)

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "rpc_sync", _rpc)
    return calls


class TestJtiHydrationDiscipline:
    def test_every_revoked_jti_is_loaded_not_just_the_first_page(self, monkeypatch):
        _fake_pager(4300, monkeypatch)
        I._hydrate_jti_revocations()
        assert len(I._revoked_jtis) == 4300
        assert I.is_jti_revoked("jti_4299") is True

    def test_a_single_page_still_latches(self, monkeypatch):
        calls = _fake_pager(12, monkeypatch)
        I._hydrate_jti_revocations()
        assert I._jti_revocation_hydrated is True
        assert len(calls) == 1
        I._hydrate_jti_revocations()
        assert len(calls) == 1, "hydration ran twice despite the latch"

    def test_a_failed_read_does_not_latch(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("supabase down")

        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", _boom)
        I._hydrate_jti_revocations()
        assert I._jti_revocation_hydrated is False

    def test_an_empty_successful_read_latches_with_zero(self, monkeypatch):
        """A genuinely empty result (nothing revoked yet) is a SUCCESSFUL
        read and must latch -- this is not the bug; the bug this migration
        fixes is an empty result that comes from a PERMISSION failure being
        indistinguishable from this one. See TestJtiHydrationPermissionDenied
        for the case that must NOT latch."""
        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [])
        I._hydrate_jti_revocations()
        assert I._jti_revocation_hydrated is True
        assert len(I._revoked_jtis) == 0

    def test_hydration_calls_the_rpc_by_name_with_paging_params(self, monkeypatch):
        """Locks in the RPC boundary itself: the exact function name
        sql/agentbroker/007_revocation_read_rpc.sql defines, called with the
        p_limit/p_offset parameter names that function's signature uses."""
        seen = {}

        def _rpc(fn, payload):
            seen["fn"] = fn
            seen["payload"] = payload
            return [{"jti": "jti_seen", "revoked_at": "2026-09-29T00:00:00Z"}]

        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", _rpc)
        I._hydrate_jti_revocations()

        assert seen["fn"] == "revoked_jtis_list"
        assert seen["payload"] == {"p_limit": 1000, "p_offset": 0}
        assert I.is_jti_revoked("jti_seen") is True


class TestJtiHydrationPermissionDenied:
    """AUDIT-2026-09-29: a 401/403/missing-function RPC failure is not a
    transient outage -- it will not self-heal on the backoff retry, it means
    a human must apply 007 or fix a grant. It must log the stable
    REVOCATION_READ_DENIED marker distinctly from the ordinary
    jti_revocation_hydrate_failed WARNING, and it must NEVER latch (an
    unreadable answer must never be read as 'zero jtis are revoked')."""

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
            I._hydrate_jti_revocations()

        assert I._jti_revocation_hydrated is False, (
            "a permission-denied RPC call must never be read as 'zero jtis "
            "are revoked'")
        assert any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        ), "a 401 RPC failure must log the stable REVOCATION_READ_DENIED marker"

    def test_a_missing_function_error_does_not_latch_and_logs_the_marker(
        self, monkeypatch, caplog,
    ):
        """404 == PGRST202: the function itself does not exist yet (007 not
        applied). Same bucket as a permission denial -- a human must act,
        the backoff retry alone will not fix it."""
        import storage.supabase_client as sb

        def _missing(fn, payload):
            raise RuntimeError(f"rpc_sync({fn!r}) failed: HTTP 404 body={{}}")

        monkeypatch.setattr(sb, "rpc_sync", _missing)
        with caplog.at_level("ERROR", logger="smb_broker.identity"):
            I._hydrate_jti_revocations()

        assert I._jti_revocation_hydrated is False
        assert any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        )

    def test_a_transient_outage_logs_the_ordinary_warning_not_the_marker(
        self, monkeypatch, caplog,
    ):
        """A transport failure/5xx is not a permission question -- it must
        keep the pre-existing WARNING behaviour and must NOT claim
        REVOCATION_READ_DENIED, which would wrongly send an operator to fix
        a grant that was never the problem."""
        import storage.supabase_client as sb

        def _outage(fn, payload):
            raise RuntimeError(f"rpc_sync({fn!r}) transport error: connection refused")

        monkeypatch.setattr(sb, "rpc_sync", _outage)
        with caplog.at_level("WARNING", logger="smb_broker.identity"):
            I._hydrate_jti_revocations()

        assert I._jti_revocation_hydrated is False
        assert not any(
            "REVOCATION_READ_DENIED" in r.getMessage() for r in caplog.records
        )
        assert any(
            "jti_revocation_hydrate_failed" in r.getMessage() for r in caplog.records
        )


class TestJtiHydrationRejectsMalformedRows:
    """Adversarial-review finding, 9e7b800: the isinstance(_chunk, list)
    check validates only the PAGE, never its ROWS, and the finalizing
    `row.get("jti")` loop that turns `rows` into `_revoked_jtis` sits
    OUTSIDE the try/except. A page shaped like ["cus_x"] or [None] is a
    list, so it sails through the check and gets extended into `rows`; the
    unguarded loop then calls `.get()` on a str/None and raises
    AttributeError straight out of is_jti_revoked() into validate_token() --
    a 500 on the live auth path, not the documented fail-open WARNING.
    Reproduced here as a direct call to _hydrate_jti_revocations() (isolates
    this function's bug) and as validate_token() (proves the live auth path
    itself does not raise)."""

    def test_a_string_array_page_does_not_raise_and_does_not_latch(
        self, monkeypatch, caplog,
    ):
        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: ["cus_x"])

        with caplog.at_level("WARNING", logger="smb_broker.identity"):
            I._hydrate_jti_revocations()  # must not raise AttributeError

        assert I._jti_revocation_hydrated is False, (
            "a page of non-dict rows must never latch hydration as complete")
        assert any(
            "jti_revocation_hydrate_failed" in r.getMessage()
            for r in caplog.records
        ), "a malformed page must log the ordinary hydrate-failed WARNING"
        # End-to-end: a lookup during the same bad page must not raise either.
        assert I.is_jti_revoked("some-jti") is False

    def test_a_none_page_does_not_raise_and_does_not_latch(
        self, monkeypatch, caplog,
    ):
        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [None])

        token_resp = _issue()
        with caplog.at_level("WARNING", logger="smb_broker.identity"):
            result = I.validate_token(token_resp.token)  # must not raise

        assert result.valid is True, (
            "a malformed hydration page must not raise, and must not turn "
            "a valid token invalid")
        assert I._jti_revocation_hydrated is False
        assert any(
            "jti_revocation_hydrate_failed" in r.getMessage()
            for r in caplog.records
        )


class TestJtiHydrationRequiresTheKey:
    """Adversarial review of 79a7573: a page of dicts that LACK "jti" (e.g. the
    RPC's output column renamed) passed the dict check and latched hydration
    as a complete read of zero revocations - the exact silent-empty failure
    007 exists to fix. A row must carry the key; a null value is still a
    legitimate row."""

    def test_rows_missing_the_key_do_not_latch_and_warn(self, monkeypatch, caplog):
        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [{"foo": "x"}])

        with caplog.at_level("WARNING", logger="smb_broker.identity"):
            I._hydrate_jti_revocations()

        assert I._jti_revocation_hydrated is False
        assert any("jti_revocation_hydrate_failed" in r.getMessage()
                   for r in caplog.records)

    def test_a_null_jti_value_still_latches(self, monkeypatch):
        import storage.supabase_client as sb
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [{"jti": None}])

        I._hydrate_jti_revocations()

        assert I._jti_revocation_hydrated is True
