"""Stable fulfillment claims sign identically across retries, without changing ordinary minting."""
import json
import base64
import pytest

from agent_interface import identity


def claims(token):
    encoded = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_same_persistent_issuance_reproduces_exact_token(monkeypatch):
    monkeypatch.setattr(identity, "_SIGNING_SECRET", "synthetic-test-signing-secret")
    kwargs = dict(customer_id="offline-customer", plan="developer", customer_email="buyer@example.invalid",
                  issued_at=1791000000.0, token_id="a" * 32)
    first = identity.issue_subscription_token(**kwargs)
    monkeypatch.setattr(identity.time, "time", lambda: 1792000000.0)
    second = identity.issue_subscription_token(**kwargs)
    assert first == second
    assert claims(first.token)["jti"] == "a" * 32
    assert claims(first.token)["agent_id"] == "sub_offline-customer"
    assert first.expires_at == 1791000000.0 + 90 * 86400


def test_default_issuance_remains_fresh(monkeypatch):
    monkeypatch.setattr(identity, "_SIGNING_SECRET", "synthetic-test-signing-secret")
    first = identity.issue_subscription_token("offline-customer", "developer", "")
    second = identity.issue_subscription_token("offline-customer", "developer", "")
    assert claims(first.token)["jti"] != claims(second.token)["jti"]


def test_stable_identity_uses_existing_validation_and_revocation(monkeypatch):
    monkeypatch.setattr(identity, "_SIGNING_SECRET", "synthetic-test-signing-secret")
    monkeypatch.setattr(identity.time, "time", lambda: 1791000001.0)
    monkeypatch.setattr(identity, "is_jti_revoked", lambda _: False)
    monkeypatch.setattr(identity, "is_customer_revoked", lambda _: False)
    token = identity.issue_subscription_token("offline-customer", "developer", "",
                                             issued_at=1791000000.0, token_id="a" * 32).token
    assert identity.validate_token(token).valid
    monkeypatch.setattr(identity, "is_jti_revoked", lambda jti: jti == "a" * 32)
    assert not identity.validate_token(token).valid
    monkeypatch.setattr(identity, "is_jti_revoked", lambda _: False)
    monkeypatch.setattr(identity, "is_customer_revoked", lambda customer: customer == "offline-customer")
    assert not identity.validate_token(token).valid
    monkeypatch.setattr(identity, "is_customer_revoked", lambda _: False)
    monkeypatch.setattr(identity.time, "time", lambda: 1791000000.0 + 91 * 86400)
    assert not identity.validate_token(token).valid


@pytest.mark.parametrize("timestamp,token_id", [(None, "a" * 32), (1791000000.0, None),
                         (float("nan"), "a" * 32), (float("inf"), "a" * 32),
                         (True, "a" * 32), (-1, "a" * 32), (1791000000.0, "bad-id")])
def test_incomplete_or_invalid_stable_issuance_fails(timestamp, token_id):
    with pytest.raises(ValueError):
        identity.issue_subscription_token("offline-customer", "developer", "",
                                          issued_at=timestamp, token_id=token_id)
