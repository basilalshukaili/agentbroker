"""Synthetic portal/HTTP proofs: disabled mutations cannot leak or promote keys."""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from agent_interface import identity, portal
from agent_interface.portal_logic import make_session_cookie


@pytest.fixture
def protected(monkeypatch):
    # A synthetic secret and signed free identity; no operator config is read.
    monkeypatch.setenv("PORTAL_SESSION_SECRET", "synthetic-portal-session")
    monkeypatch.setattr(identity, "_SIGNING_SECRET", "synthetic-identity-signing")
    token = identity.issue_token(identity.TokenRequest(
        agent_id="free_fixture", principal_id="free_fixture",
        principal_type="human", budget_cap_usd=0.0, ttl_seconds=90 * 86400,
    )).token
    account = {
        "account_id": "free_fixture", "email": "fixture@example.invalid",
        "plan": "free", "balance_credits": 0, "key_token": token,
        # Historical bug: metadata contains agent_id, not the signed UUID jti.
        "key_jti": "free_fixture",
    }
    read = AsyncMock(return_value=account)
    monkeypatch.setattr(portal, "_get_account", read)
    forbidden = []
    for name in ("issue_token", "issue_subscription_token"):
        spy = Mock(side_effect=AssertionError("portal issuance forbidden"))
        monkeypatch.setattr(identity, name, spy)
        forbidden.append(spy)
    for name in ("revoke_token", "revoke_jti"):
        spy = AsyncMock(side_effect=AssertionError("portal revocation forbidden"))
        monkeypatch.setattr(identity, name, spy)
        forbidden.append(spy)
    import storage.supabase_client as sb
    for name in ("insert_row", "insert_row_strict", "update_row", "upsert_row", "rpc"):
        spy = AsyncMock(side_effect=AssertionError("portal mutation forbidden"))
        monkeypatch.setattr(sb, name, spy)
        forbidden.append(spy)
    patch = AsyncMock(side_effect=AssertionError("account PATCH forbidden"))
    monkeypatch.setattr(httpx.AsyncClient, "patch", patch)
    forbidden.append(patch)
    yield account, read, forbidden
    for spy in forbidden:
        spy.assert_not_called()


def _app():
    app = FastAPI()
    app.include_router(portal.router)
    return app


async def _post(app, path, cookie=True):
    headers = {}
    if cookie:
        headers["Cookie"] = "hl_portal=" + make_session_cookie("fixture@example.invalid")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid",
    ) as client:
        return await client.post("/portal/key/" + path, headers=headers)


@pytest.mark.parametrize("path", ["generate", "reveal", "regenerate"])
def test_http_authentication_precedes_every_key_action(protected, path):
    account, read, _ = protected
    response = asyncio.run(_post(_app(), path, cookie=False))
    assert response.status_code == 401
    assert account["key_token"] not in response.text
    read.assert_not_called()


def test_http_reveal_returns_only_the_authenticated_stored_key(protected):
    account, read, _ = protected
    response = asyncio.run(_post(_app(), "reveal"))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "key": account["key_token"]}
    read.assert_awaited_once_with("fixture@example.invalid")


@pytest.mark.parametrize("plan", ["free", "developer", "business", None, "unknown"])
@pytest.mark.parametrize("path", ["generate", "reveal", "regenerate"])
def test_missing_key_never_mints_or_exposes_an_unpersisted_key(protected, path, plan):
    account, _, _ = protected
    account.update(key_token=None, plan=plan)
    before = copy.deepcopy(account)
    response = asyncio.run(_post(_app(), path))
    assert response.status_code == 503
    assert response.json()["reason"] == "key_mutation_unavailable"
    assert "key" not in response.json() and "token" not in response.json()
    assert account == before


def test_generate_existing_key_is_a_read_only_noop(protected):
    account, _, _ = protected
    before = copy.deepcopy(account)
    response = asyncio.run(_post(_app(), "generate"))
    assert response.json() == {"ok": True, "already": True}
    assert account == before


def test_rotation_ignores_broken_metadata_and_does_not_promote_free_key(protected):
    account, read, _ = protected
    before = copy.deepcopy(account)
    claims = identity._verify(account["key_token"])
    assert claims["jti"] != account["key_jti"]
    response = asyncio.run(_post(_app(), "regenerate"))
    assert response.status_code == 503
    read.assert_not_called()  # fail closed even if storage is down
    assert account == before
    assert claims["agent_id"] == "free_fixture"
    assert claims["scope"]["budget_cap_usd"] == 0.0


@pytest.mark.parametrize("path", ["generate", "reveal", "regenerate"])
def test_no_account_never_creates_a_row(protected, path):
    _, read, _ = protected
    read.return_value = None
    response = asyncio.run(_post(_app(), path))
    assert response.json()["ok"] is False
    assert "key" not in response.json()
    assert response.status_code == (200 if path == "reveal" else 503)


def test_parallel_apps_and_retries_cannot_mutate_or_leak_replacement(protected):
    account, _, _ = protected
    before = copy.deepcopy(account)
    apps = [_app(), _app()]

    async def requests():
        return await asyncio.gather(*[
            _post(apps[i % 2], "regenerate") for i in range(40)
        ])

    responses = asyncio.run(requests())
    assert all(response.status_code == 503 for response in responses)
    assert all("key" not in response.json() for response in responses)
    assert account == before


def test_generate_cannot_be_enabled_by_environment(protected, monkeypatch):
    account, _, _ = protected
    account["key_token"] = None
    monkeypatch.setenv("PORTAL_KEY_MUTATION_ENABLED", "1")
    response = asyncio.run(_post(_app(), "generate"))
    assert response.status_code == 503


def test_real_account_lookup_uses_only_session_email(monkeypatch):
    import storage.supabase_client as sb
    read = AsyncMock(return_value=[{"key_token": "synthetic-stored-value"}])
    monkeypatch.setattr(sb, "select_rows", read)
    account = asyncio.run(portal._get_account("fixture@example.invalid"))
    assert account == {"key_token": "synthetic-stored-value"}
    read.assert_awaited_once_with(
        "credit_accounts", filters={"email": "fixture@example.invalid"}, limit=1,
    )
