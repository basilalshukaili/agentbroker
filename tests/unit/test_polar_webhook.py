"""Polar webhook signature verification — proven against the official
Standard Webhooks test vector (https://www.standardwebhooks.com), which Polar
implements. This is payment-critical: a wrong verifier either rejects real
payments or accepts forged ones."""
from billing.polar_webhook import verify_polar_signature, _extract_plan, _extract_email

# Official Standard Webhooks test vector.
_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
_BODY = b'{"test": 2432232314}'
_HEADERS = {
    "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
    "webhook-timestamp": "1614265330",
    "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
}


def test_valid_signature_passes():
    assert verify_polar_signature(_BODY, _HEADERS, _SECRET, enforce_timestamp=False) is True


def test_tampered_body_fails():
    assert verify_polar_signature(b'{"test": 9999}', _HEADERS, _SECRET, enforce_timestamp=False) is False


def test_wrong_secret_fails():
    assert verify_polar_signature(
        _BODY, _HEADERS, "whsec_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", enforce_timestamp=False
    ) is False


def test_missing_headers_fails():
    assert verify_polar_signature(_BODY, {}, _SECRET, enforce_timestamp=False) is False


def test_empty_secret_fails():
    assert verify_polar_signature(_BODY, _HEADERS, "", enforce_timestamp=False) is False


def test_stale_timestamp_rejected_when_enforced():
    # The vector's timestamp is from 2021 — enforcing freshness must reject it.
    assert verify_polar_signature(_BODY, _HEADERS, _SECRET, enforce_timestamp=True) is False


def test_plan_extraction_from_metadata():
    assert _extract_plan({"metadata": {"plan": "Business"}}) == "business"
    assert _extract_plan({"product": {"name": "Agent Broker Enterprise"}}) == "enterprise"
    assert _extract_plan({}) == "developer"  # safe default


def test_email_extraction():
    assert _extract_email({"customer": {"email": "dev@example.com"}}) == "dev@example.com"
    assert _extract_email({"customer_email": "x@y.com"}) == "x@y.com"
    assert _extract_email({}) is None
