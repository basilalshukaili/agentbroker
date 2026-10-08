"""Offline transport fixtures prove actual-JTI, duplicate and lost-ack retry behavior.

This proves the Python/PostgREST contract, not PostgreSQL roles or peer freshness.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_interface import identity as I
from storage import supabase_client as sb


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.fixture.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "synthetic-service-key")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.setattr(I, "_SIGNING_SECRET", "synthetic-identity-signing")
    monkeypatch.setattr(I, "_revoked_jtis", set())
    monkeypatch.setattr(I, "_jti_revocation_hydrated", False)
    monkeypatch.setattr(I, "_jti_revocation_next_try", 0.0)


def _token():
    return I.issue_token(I.TokenRequest(
        agent_id="free_fixture", principal_id="free_fixture", budget_cap_usd=0.0,
    )).token


@pytest.mark.parametrize("lost_ack", [False, True])
def test_repeated_revoke_confirms_actual_signed_jti_without_overwriting(monkeypatch, lost_ack):
    durable = {}
    calls = []
    original_client = httpx.AsyncClient

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            row = json.loads(request.content)
            jti = row["jti"]
            if jti in durable:
                return httpx.Response(409, json={"code": "23505"})
            durable[jti] = row
            if lost_ack:
                raise httpx.ReadTimeout("synthetic lost acknowledgement")
            return httpx.Response(201, json=[row])
        assert request.method == "GET"
        assert request.url.params["limit"] == "1"
        jti = request.url.params["jti"].removeprefix("eq.")
        return httpx.Response(200, json=[durable[jti]] if jti in durable else [])

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    token = _token()
    actual = I._verify(token)["jti"]
    assert actual != "free_fixture"
    assert asyncio.run(I.revoke_token(token, reason="first")) is True
    before = dict(durable[actual])
    assert asyncio.run(I.revoke_token(token, reason="retry")) is True
    assert durable == {actual: before}
    assert calls == (["POST", "GET", "POST", "GET"] if lost_ack else ["POST", "POST", "GET"])
    # Restart read-half proof. RPC is synthetic; it does not prove peer refresh.
    I._revoked_jtis.clear()
    monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: list(durable.values()))
    assert I.is_jti_revoked(actual) is True


@pytest.mark.parametrize("written", [None, {}, {"jti": "wrong"}, [], True])
def test_nonmatching_write_ack_requires_exact_durable_readback(monkeypatch, written):
    monkeypatch.setattr(sb, "insert_row_strict", AsyncMock(return_value=written))
    monkeypatch.setattr(sb, "select_rows_strict", AsyncMock(return_value=[]))
    token = _token()
    assert asyncio.run(I.revoke_token(token)) is False
    assert I._verify(token)["jti"] in I._revoked_jtis


@pytest.mark.parametrize("rows", [[], [{"jti": "wrong"}], [None], {}, [{"jti": "x"}, {"jti": "y"}]])
def test_conflict_or_outage_is_not_success_without_exact_row(monkeypatch, rows):
    monkeypatch.setattr(sb, "insert_row_strict", AsyncMock(side_effect=RuntimeError("synthetic conflict")))
    monkeypatch.setattr(sb, "select_rows_strict", AsyncMock(return_value=rows))
    assert asyncio.run(I.revoke_token(_token())) is False


def test_unavailable_confirmation_fails_closed_but_keeps_local_revocation(monkeypatch):
    monkeypatch.setattr(sb, "insert_row_strict", AsyncMock(side_effect=RuntimeError("synthetic timeout")))
    monkeypatch.setattr(sb, "select_rows_strict", AsyncMock(side_effect=RuntimeError("synthetic denied read")))
    token = _token()
    assert asyncio.run(I.revoke_token(token)) is False
    assert I.is_jti_revoked(I._verify(token)["jti"]) is True


def test_forged_token_never_revokes_an_identifier(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(sb, "insert_row_strict", write)
    token = _token()
    assert asyncio.run(I.revoke_token(token + "forged")) is False
    write.assert_not_called()
    assert I._revoked_jtis == set()
