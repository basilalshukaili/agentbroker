"""Polar legacy/Standard Webhooks compatibility; synthetic, offline fixtures only.

The basic Standard Webhooks published vector and missing-header/secret checks
remain in test_polar_webhook.py. These cases cover compatibility and adversarial
signature parsing without invoking event handlers, credentials, or delivery.
"""
import base64
import hmac
import socket

import pytest

from billing import polar_webhook


# Independently generated with .NET System.Security.Cryptography.HMACSHA256:
# new key = bytes 1..32; old key = UTF8("whsec_" + Base64(new key)).
# Both sign UTF8(message_id + "." + timestamp + "." + body).
# No application signing/verifier helper generated these fixtures.
_SECRET = "whsec_AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
_BODY = b'{"type":"order.paid","data":{"id":"order_OFFLINE_FIXTURE_ONLY","amount":900}}'
_MESSAGE_ID = "msg_offline_signature_regression"
_TIMESTAMP = 1790078400
_SIGNATURES = {
    "legacy": "cPAGkwJZQj32QEj7qS4vXdwa2SycjE8tfhko/Q985ls=",
    "standard": "KxBdiOQbAWGT3YDPCaovzko/j9/RdacbGa1sdK4gCoo=",
}


@pytest.fixture(autouse=True)
def offline_clock(monkeypatch):
    monkeypatch.setattr(polar_webhook.time, "time", lambda: _TIMESTAMP)

    def reject_network(*args, **kwargs):
        pytest.fail("Signature verification must not access the network")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)


@pytest.fixture(params=("legacy", "standard"))
def signed_headers(request):
    return {
        "webhook-id": _MESSAGE_ID,
        "webhook-timestamp": str(_TIMESTAMP),
        "webhook-signature": "v1," + _SIGNATURES[request.param],
    }


@pytest.mark.parametrize(
    ("clock_delta", "accepted"),
    [(0, True), (-300, True), (300, True), (-301, False), (301, False)],
)
def test_both_schemes_obey_timestamp_window(
    signed_headers, monkeypatch, clock_delta, accepted
):
    monkeypatch.setattr(polar_webhook.time, "time", lambda: _TIMESTAMP + clock_delta)
    assert (
        polar_webhook.verify_polar_signature(_BODY, signed_headers, _SECRET)
        is accepted
    )


@pytest.mark.parametrize("tamper", ("amount", "body_space", "message_id", "timestamp"))
def test_both_schemes_bind_exact_payload_id_and_timestamp(signed_headers, tamper):
    body = _BODY
    if tamper == "amount":
        body = _BODY.replace(b"900", b"901")
    elif tamper == "body_space":
        body += b" "
    elif tamper == "message_id":
        signed_headers["webhook-id"] = "msg_different"
    else:
        # Still inside the freshness window: must fail cryptographic binding.
        signed_headers["webhook-timestamp"] = str(_TIMESTAMP + 1)

    assert not polar_webhook.verify_polar_signature(body, signed_headers, _SECRET)


@pytest.mark.parametrize("version", ("v2", "v1a", ""))
def test_supported_hmac_under_unsupported_version_is_rejected(signed_headers, version):
    signature = signed_headers["webhook-signature"].split(",", 1)[1]
    signed_headers["webhook-signature"] = version + "," + signature
    assert not polar_webhook.verify_polar_signature(_BODY, signed_headers, _SECRET)


@pytest.mark.parametrize("malformed", ("v1,\u2603", "v1,", "no-comma", "v2,ignored"))
def test_bad_entry_cannot_mask_later_valid_rotation_signature(signed_headers, malformed):
    signed_headers["webhook-signature"] = (
        malformed + " " + signed_headers["webhook-signature"]
    )
    assert polar_webhook.verify_polar_signature(_BODY, signed_headers, _SECRET)


def test_nonascii_only_signature_returns_false(signed_headers):
    signed_headers["webhook-signature"] = "v1,\u2603"
    assert not polar_webhook.verify_polar_signature(_BODY, signed_headers, _SECRET)


def test_svix_header_aliases_support_both_schemes(signed_headers):
    aliases = {
        key.replace("webhook-", "svix-"): value
        for key, value in signed_headers.items()
    }
    assert polar_webhook.verify_polar_signature(_BODY, aliases, _SECRET)


@pytest.mark.parametrize("timestamp", ("not-an-integer", ""))
def test_malformed_timestamp_rejected_before_verification(signed_headers, timestamp):
    signed_headers["webhook-timestamp"] = timestamp
    assert not polar_webhook.verify_polar_signature(_BODY, signed_headers, _SECRET)


def test_legacy_signature_rejects_wrong_full_secret():
    headers = {
        "webhook-id": _MESSAGE_ID,
        "webhook-timestamp": str(_TIMESTAMP),
        "webhook-signature": "v1," + _SIGNATURES["legacy"],
    }
    assert not polar_webhook.verify_polar_signature(
        _BODY, headers, "whsec_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )


def test_whitespace_configuration_cannot_authenticate_with_empty_key():
    # Prior permissive base64 parsing turned blank configuration into b"".
    content = f"{_MESSAGE_ID}.{_TIMESTAMP}.".encode() + _BODY
    empty_key_signature = base64.b64encode(hmac.digest(b"", content, "sha256")).decode()
    headers = {
        "webhook-id": _MESSAGE_ID,
        "webhook-timestamp": str(_TIMESTAMP),
        "webhook-signature": "v1," + empty_key_signature,
    }
    assert not polar_webhook.verify_polar_signature(_BODY, headers, " \t\r\n")
