"""Already running key and OAuth clients cannot retain paid access after the refresh bound."""
import asyncio
from types import SimpleNamespace
import pytest
from agent_interface import identity as ident
from agent_interface.oauth import tokens


@pytest.fixture
def peers(monkeypatch):
    clock, durable = [1791000000.0], []
    monkeypatch.setattr(ident.time, "time", lambda: clock[0])
    monkeypatch.setattr(ident, "_SIGNING_SECRET", "synthetic-auth-test-signing-secret")
    monkeypatch.setattr(ident, "_revoked_customer_ids", set())
    monkeypatch.setattr(ident, "_revocation_hydrated", False)
    monkeypatch.setattr(ident, "_revocation_next_try", 0.0)
    monkeypatch.setattr(ident, "is_jti_revoked", lambda _: False)
    monkeypatch.setenv("POLAR_FULFILLMENT_KEY", "synthetic-scoped-key")
    monkeypatch.setenv("SUPABASE_URL", "https://offline.invalid")
    def rpc(name, payload):
        assert name == "polar_order_events_revoked_customer_ids"
        return [{"customer_id": cid} for cid in durable][payload["p_offset"]:payload["p_offset"]+payload["p_limit"]]
    monkeypatch.setattr("storage.supabase_client.rpc_sync", rpc)
    key = ident.issue_subscription_token("offline-customer", "developer", "").token
    subject = tokens.Subject("sub_offline-customer", "offline-customer", True, "developer")
    oauth = tokens.mint_access_token(subject,resource="https://api.hatchloop.dev/mcp",scope="mcp:tools",
                                    client_id="offline-client",family_id="offline-family").token
    return clock, durable, key, oauth


def test_existing_key_and_oauth_bearer_expire_peer_revocation_cache(peers):
    clock, durable, key, oauth = peers
    assert ident.validate_token(key).valid and ident.validate_token(oauth).valid
    assert ident._revocation_hydrated and ident._revoked_customer_ids == set()
    # A different process commits refund; this worker's already loaded cache is unchanged.
    durable.append("offline-customer")
    clock[0] += ident._REVOCATION_RETRY_S + 1
    assert not ident.validate_token(key).valid
    assert not ident.validate_token(oauth).valid


def test_oauth_refresh_cannot_issue_paid_subject_after_peer_refund(peers):
    clock, durable, key, oauth = peers
    assert ident.validate_token(oauth).valid
    durable.append("offline-customer")
    clock[0] += ident._REVOCATION_RETRY_S + 1
    async def lookup(digest):
        return dict(account_id="sub_offline-customer",customer_id="offline-customer",plan="developer")
    assert asyncio.run(tokens.resolve_subject(SimpleNamespace(account_for_email=lookup), "a"*64)) is None


def test_expired_cache_outage_denies_paid_access_until_recovery(peers, monkeypatch):
    clock, durable, key, oauth = peers
    assert ident.validate_token(key).valid
    def outage(*args):
        raise RuntimeError("synthetic store outage")
    monkeypatch.setattr("storage.supabase_client.rpc_sync", outage)
    clock[0] += ident._REVOCATION_RETRY_S + 1
    assert not ident.validate_token(key).valid and not ident.validate_token(oauth).valid
    assert not ident._revocation_hydrated


def test_missing_scoped_key_cannot_restore_paid_access_during_outage(peers, monkeypatch):
    clock, durable, key, oauth = peers
    assert ident.validate_token(key).valid
    monkeypatch.delenv("POLAR_FULFILLMENT_KEY", raising=False)
    original_rpc = __import__("storage.supabase_client", fromlist=["rpc_sync"]).rpc_sync
    monkeypatch.setattr("storage.supabase_client.rpc_sync", lambda *args: None)
    clock[0] += ident._REVOCATION_RETRY_S + 1
    assert not ident.validate_token(key).valid and not ident.validate_token(oauth).valid
    monkeypatch.setattr("storage.supabase_client.rpc_sync", original_rpc)
    clock[0] += ident._REVOCATION_RETRY_S + 1
    assert ident.validate_token(key).valid and ident.validate_token(oauth).valid


def test_free_identity_does_not_require_paid_revocation_freshness(peers, monkeypatch):
    def outage(*args):
        raise RuntimeError("synthetic store outage")
    monkeypatch.setattr("storage.supabase_client.rpc_sync", outage)
    free = ident.issue_token(ident.TokenRequest(agent_id="free_offline",principal_id="free_offline")).token
    assert ident.validate_token(free).valid
