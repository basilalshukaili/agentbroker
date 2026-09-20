"""POST /keys/request must not claim success when no email left the process.

CONFIRMED (assignment #3, technical lead, 2026-09-21): production has no
Resend configured (RESEND_API_KEY unset - verified via
GET https://api.hatchloop.dev/healthz/external, resend: not_configured), and
`request_free_key` always returned 200 {"status": "verification_sent"}
regardless of whether `send_verification_email` (agent_interface/
key_request_logic.py:117) actually sent anything.
`send_verification_email` was documented "best-effort - never raises" and
returned None unconditionally, so the router had no way to tell "sent" from
"silently skipped" - a missing key produced a success-shaped response and
silence. Every new agent who signed up was told to go check an inbox that was
never going to receive anything.

Fix: send_verification_email now returns bool (True only on an actual 2xx
from Resend). request_free_key checks it and returns 503
{"error": "onboarding_unavailable", ...} instead of the success shape when
nothing was sent - honest refusal instead of a false positive.

NEVER SENDS A REAL EMAIL: httpx.AsyncClient is monkeypatched to a mock
transport (or never reached at all, when RESEND_API_KEY is unset) in every
test in this file, including the ones that simulate a successful send.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_interface import key_requests as KR
from agent_interface import key_request_logic as KRL


def _run(coro):
    return asyncio.run(coro)


def _post_request(email: str):
    return _run(KR.request_free_key(body=KR.KeyRequestBody(email=email)))


def _body(resp) -> dict:
    import json
    return json.loads(resp.body)


# ---------------------------------------------------------------------------
# Mock HTTP transport for send_verification_email -- never opens a socket.
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _MockHTTPClient:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        self.calls += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class _PoisonHTTPClient:
    """Fails the test outright if anything tries to open a connection."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        raise AssertionError(
            "send_verification_email must not call Resend when "
            "RESEND_API_KEY is unset -- this would have sent a real request")


# ---------------------------------------------------------------------------
# Layer 1: send_verification_email itself, against a mocked transport.
# Establishes the discriminating fact the router-level tests build on: the
# function now reports what actually happened instead of always returning
# None.
# ---------------------------------------------------------------------------

def test_missing_key_returns_false_and_never_calls_resend(monkeypatch):
    """THE EXACT PRODUCTION STATE TODAY: RESEND_API_KEY unset."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _PoisonHTTPClient())
    sent = _run(KRL.send_verification_email("someone@example.com", "https://x/verify?token=t"))
    assert sent is False, "must report failure, not None/truthy, when no key is configured"


def test_provider_rejection_returns_false(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key-not-real")
    import httpx
    mock = _MockHTTPClient(response=_Resp(status_code=500, text="internal error"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: mock)
    sent = _run(KRL.send_verification_email("someone@example.com", "https://x/verify?token=t"))
    assert sent is False
    assert mock.calls == 1, "must actually attempt the send before reporting failure"


def test_transport_exception_returns_false(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key-not-real")
    import httpx
    mock = _MockHTTPClient(raise_exc=RuntimeError("connection reset"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: mock)
    sent = _run(KRL.send_verification_email("someone@example.com", "https://x/verify?token=t"))
    assert sent is False


def test_successful_send_returns_true(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key-not-real")
    import httpx
    mock = _MockHTTPClient(response=_Resp(status_code=200))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: mock)
    sent = _run(KRL.send_verification_email("someone@example.com", "https://x/verify?token=t"))
    assert sent is True
    assert mock.calls == 1


# ---------------------------------------------------------------------------
# Layer 2: the router, POST /keys/request end to end.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _never_touch_supabase(monkeypatch):
    """store_pending is best-effort persistence, irrelevant to this bug and
    not something a unit test should depend on a live Supabase for."""
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(KR, "store_pending", _noop)


def test_invalid_email_is_still_400(monkeypatch):
    """POSITIVE CONTROL -- unrelated to this fix, passes on both the fixed
    and the pre-fix tree. Included to show the discriminating tests below
    aren't just noise: ordinary validation is untouched."""
    resp = _post_request("not-an-email")
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_email"


def test_send_failure_is_refused_honestly_not_faked_as_sent(monkeypatch):
    """THE BUG. Reproduces the exact reported defect: email delivery fails
    (any reason) and the caller must NOT be told 'verification_sent'."""
    async def _fails(email, verify_url):
        return False
    monkeypatch.setattr(KR, "send_verification_email", _fails)

    resp = _post_request("agent@example.com")

    assert resp.status_code != 200, (
        "a failed send must not return the same 200 as a real success")
    assert resp.status_code == 503
    body = _body(resp)
    assert body["error"] == "onboarding_unavailable"
    # Must say plainly that nothing was sent, not just fail silently on tone.
    assert "no" in body["detail"].lower() and "sent" in body["detail"].lower()
    assert "verification_sent" not in resp.body.decode()
    assert "check your inbox" not in resp.body.decode().lower()


def test_todays_actual_production_config_is_refused(monkeypatch):
    """No mocking of send_verification_email itself -- run the REAL function
    with RESEND_API_KEY unset, exactly as production is configured today
    (confirmed via /healthz/external: resend not_configured). Must not touch
    a real socket either way."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _PoisonHTTPClient())

    resp = _post_request("agent@example.com")

    assert resp.status_code == 503
    assert _body(resp)["error"] == "onboarding_unavailable"


def test_send_failure_response_does_not_depend_on_any_new_env_var(monkeypatch):
    """The fix must be correct with TODAY's production configuration, not
    conditioned on some new flag that is also unset. Explicitly clear every
    env var this code path touches and confirm the honest failure still
    fires -- nothing here should make the path 'succeed'."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _PoisonHTTPClient())

    resp = _post_request("agent@example.com")
    assert resp.status_code == 503


def test_send_success_still_returns_verification_sent(monkeypatch):
    """POSITIVE CONTROL -- also passes on the pre-fix tree (which ignored the
    return value and always returned this shape). Confirms the fix didn't
    break the genuine happy path, but is not itself proof of the fix."""
    async def _ok(email, verify_url):
        return True
    monkeypatch.setattr(KR, "send_verification_email", _ok)

    resp = _post_request("agent@example.com")

    assert resp.status_code == 200
    assert _body(resp)["status"] == "verification_sent"


def test_failure_message_points_to_a_real_alternative_not_keys_mint(monkeypatch):
    """The honest-failure message must tell the caller what to do instead --
    and must NOT point them at /keys/mint, which is intentionally disabled
    and must never be advertised as a working path (lead's ruling)."""
    async def _fails(email, verify_url):
        return False
    monkeypatch.setattr(KR, "send_verification_email", _fails)

    resp = _post_request("agent@example.com")
    detail = _body(resp)["detail"].lower()
    assert "keys/mint" not in detail
    assert "hello@hatchloop.dev" in detail
