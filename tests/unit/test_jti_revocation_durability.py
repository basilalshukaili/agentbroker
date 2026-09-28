"""Single-token revocation (revoke_token()/revoke_jti()) must be durable --
AUDIT-2026-09-28. Before this fix, revoke_token() added a jti to an
in-memory-only set and returned True; nothing wrote it anywhere durable, so
the leaked token it was meant to kill was valid again after the next
process restart or container redeploy, while the caller had been told the
revocation "succeeded". This mirrors test_revocation_hydration_is_complete.py
and test_identity_revocation.py's patching style for the customer-level
path, applied to the jti-level one.
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
            sb, "select_rows_sync_strict",
            lambda table, **kw: [{"jti": jti, "reason": "manual"}],
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
            sb, "select_rows_sync_strict",
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

    def _sel(table, order=None, limit=1000, offset=0, **kw):
        calls.append((limit, offset))
        return _jti_rows(max(0, min(limit, total - offset)), offset)

    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_sync_strict", _sel)
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
        monkeypatch.setattr(sb, "select_rows_sync_strict", _boom)
        I._hydrate_jti_revocations()
        assert I._jti_revocation_hydrated is False
