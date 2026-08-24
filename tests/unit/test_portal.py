"""
Unit tests for Slice 5 + 6: portal token/session helpers + email builders.

All external calls (Supabase, Resend, Polar) are mocked.
No real network calls. No FastAPI dependency.

Coverage:
- Magic token: creation, verification, expiry, wrong secret
- Session cookie: creation, verification, expiry, tampered
- Single-use token enforcement (in-memory, via portal_logic)
- Key masking
- Email builder: welcome email has key + credits in body
- Email builder: low-balance email has balance + top-up link in body
- Low-balance dedupe: not sent twice within 24h
- Polar package resolution
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac_mod
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


_TEST_SECRET = "test-portal-secret-abc123"


# ---------------------------------------------------------------------------
# Magic token tests (tests portal_logic, no FastAPI)
# ---------------------------------------------------------------------------

class TestMagicToken:
    def test_make_and_verify_valid(self):
        from agent_interface.portal_logic import make_magic_token, verify_magic_token
        token, exp = make_magic_token("user@example.com", secret=_TEST_SECRET)
        assert exp > time.time()
        email = verify_magic_token(token, secret=_TEST_SECRET)
        assert email == "user@example.com"

    def test_tampered_body_fails(self):
        from agent_interface.portal_logic import make_magic_token, verify_magic_token
        token, _ = make_magic_token("user@example.com", secret=_TEST_SECRET)
        parts = token.split(".")
        corrupted = "AAAAAAAAAA" + "." + parts[1]
        assert verify_magic_token(corrupted, secret=_TEST_SECRET) is None

    def test_wrong_sig_fails(self):
        from agent_interface.portal_logic import make_magic_token, verify_magic_token
        token, _ = make_magic_token("user@example.com", secret=_TEST_SECRET)
        parts = token.split(".")
        wrong = parts[0] + ".deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        assert verify_magic_token(wrong, secret=_TEST_SECRET) is None

    def test_expired_token_fails(self):
        from agent_interface.portal_logic import verify_magic_token
        expires_at = time.time() - 1
        payload = f"user@example.com|{expires_at}"
        sig = _hmac_mod.new(_TEST_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        token = f"{b64}.{sig}"
        assert verify_magic_token(token, secret=_TEST_SECRET) is None

    def test_malformed_token_fails(self):
        from agent_interface.portal_logic import verify_magic_token
        assert verify_magic_token("", secret=_TEST_SECRET) is None
        assert verify_magic_token("nodot", secret=_TEST_SECRET) is None
        assert verify_magic_token("too.many.dots", secret=_TEST_SECRET) is None

    def test_email_preserved(self):
        from agent_interface.portal_logic import make_magic_token, verify_magic_token
        for email in ["admin@hatchloop.dev", "USER+TAG@EXAMPLE.COM", "a@b.co"]:
            token, _ = make_magic_token(email, secret=_TEST_SECRET)
            result = verify_magic_token(token, secret=_TEST_SECRET)
            assert result == email

    def test_wrong_secret_fails(self):
        from agent_interface.portal_logic import make_magic_token, verify_magic_token
        token, _ = make_magic_token("user@example.com", secret=_TEST_SECRET)
        assert verify_magic_token(token, secret="different-secret") is None


# ---------------------------------------------------------------------------
# Session cookie tests
# ---------------------------------------------------------------------------

class TestSessionCookie:
    def test_make_and_verify_valid(self):
        from agent_interface.portal_logic import make_session_cookie, verify_session_cookie
        cookie = make_session_cookie("user@example.com", secret=_TEST_SECRET)
        email = verify_session_cookie(cookie, secret=_TEST_SECRET)
        assert email == "user@example.com"

    def test_tampered_fails(self):
        from agent_interface.portal_logic import make_session_cookie, verify_session_cookie
        cookie = make_session_cookie("user@example.com", secret=_TEST_SECRET)
        parts = cookie.split(".")
        tampered = "AAAAAAAAAA." + parts[1]
        assert verify_session_cookie(tampered, secret=_TEST_SECRET) is None

    def test_empty_fails(self):
        from agent_interface.portal_logic import verify_session_cookie
        assert verify_session_cookie("", secret=_TEST_SECRET) is None
        assert verify_session_cookie(None, secret=_TEST_SECRET) is None

    def test_expired_session_fails(self):
        from agent_interface.portal_logic import verify_session_cookie
        payload = f"user@example.com|{time.time() - 1}"
        sig = _hmac_mod.new(_TEST_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        cookie = f"{b64}.{sig}"
        assert verify_session_cookie(cookie, secret=_TEST_SECRET) is None

    def test_different_email_different_cookie(self):
        from agent_interface.portal_logic import make_session_cookie
        c1 = make_session_cookie("alice@example.com", secret=_TEST_SECRET)
        c2 = make_session_cookie("bob@example.com", secret=_TEST_SECRET)
        assert c1 != c2

    def test_wrong_secret_fails(self):
        from agent_interface.portal_logic import make_session_cookie, verify_session_cookie
        cookie = make_session_cookie("user@example.com", secret=_TEST_SECRET)
        assert verify_session_cookie(cookie, secret="wrong-secret") is None


# ---------------------------------------------------------------------------
# Token consume key uniqueness
# ---------------------------------------------------------------------------

class TestTokenConsumeKey:
    def test_same_token_same_key(self):
        from agent_interface.portal_logic import token_consume_key
        t = "abc123"
        assert token_consume_key(t) == token_consume_key(t)

    def test_different_tokens_different_keys(self):
        from agent_interface.portal_logic import token_consume_key
        assert token_consume_key("token_a") != token_consume_key("token_b")

    def test_key_is_32_chars(self):
        from agent_interface.portal_logic import token_consume_key
        assert len(token_consume_key("any_token")) == 32


# ---------------------------------------------------------------------------
# Key masking
# ---------------------------------------------------------------------------

class TestKeyMasking:
    def test_long_key_masked(self):
        from agent_interface.portal_logic import mask_key
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature_here_abc"
        masked = mask_key(token)
        assert masked.startswith(token[:12])
        assert masked.endswith(token[-4:])
        assert "..." in masked
        assert len(masked) < len(token)

    def test_short_key_safe(self):
        from agent_interface.portal_logic import mask_key
        assert len(mask_key("abc")) > 0

    def test_16_char_key(self):
        from agent_interface.portal_logic import mask_key
        key16 = "a" * 16
        masked = mask_key(key16)
        assert "..." in masked

    def test_mask_hides_middle(self):
        from agent_interface.portal_logic import mask_key
        key = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        masked = mask_key(key)
        # Middle chars should not appear literally in the masked string
        assert "MNOPQRSTUV" not in masked


# ---------------------------------------------------------------------------
# WELCOME email builder
# ---------------------------------------------------------------------------

class TestWelcomeEmail:
    def test_welcome_email_contains_key(self):
        from billing.emails import send_welcome_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append({"to": to, "subject": subject, "html": html})
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_welcome_email(
                email="user@example.com",
                credits=1000,
                api_key="test-api-key-abc123",
            ))

        assert len(sent) == 1
        assert "test-api-key-abc123" in sent[0]["html"]
        assert "1,000 credits" in sent[0]["html"]
        assert sent[0]["to"] == "user@example.com"

    def test_welcome_subject_includes_credits(self):
        from billing.emails import send_welcome_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append({"subject": subject})
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_welcome_email("user@example.com", 3500, "key-xyz"))

        assert "3,500" in sent[0]["subject"]

    def test_welcome_email_no_resend_key_returns_false(self):
        from billing.emails import send_welcome_email
        with patch.dict(os.environ, {"RESEND_API_KEY": ""}):
            result = run(send_welcome_email("user@example.com", 1000, "key-abc"))
        assert result is False

    def test_welcome_email_contains_portal_link(self):
        from billing.emails import send_welcome_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append(html)
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_welcome_email("user@example.com", 1000, "key-abc"))

        assert "hatchloop.dev/portal" in sent[0]

    def test_welcome_email_contains_mcp_config(self):
        from billing.emails import send_welcome_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append(html)
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_welcome_email("user@example.com", 1000, "key-abc"))

        assert "mcpServers" in sent[0]
        assert "agent-broker" in sent[0]

    def test_welcome_amount_usd_shown(self):
        from billing.emails import send_welcome_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append(html)
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_welcome_email("user@example.com", 1000, "key-abc", amount_usd=9.0))

        assert "$9.00" in sent[0]


# ---------------------------------------------------------------------------
# LOW-BALANCE email builder
# ---------------------------------------------------------------------------

class TestLowBalanceEmail:
    def test_low_balance_email_contains_balance(self):
        from billing.emails import send_low_balance_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append({"subject": subject, "html": html})
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_low_balance_email("user@example.com", 230))

        assert len(sent) == 1
        assert "230" in sent[0]["subject"]
        assert "hatchloop.dev/portal" in sent[0]["html"]

    def test_low_balance_email_topup_link(self):
        from billing.emails import send_low_balance_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append(html)
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_low_balance_email("user@example.com", 100))

        assert "#topup" in sent[0]

    def test_low_balance_skips_on_no_resend_key(self):
        from billing.emails import send_low_balance_email
        with patch.dict(os.environ, {"RESEND_API_KEY": ""}):
            result = run(send_low_balance_email("user@example.com", 100))
        assert result is False

    def test_low_balance_reads_stay_free_mentioned(self):
        from billing.emails import send_low_balance_email
        sent = []

        async def fake_send(to, subject, html):
            sent.append(html)
            return True

        with patch("billing.emails._send", side_effect=fake_send):
            run(send_low_balance_email("user@example.com", 400))

        assert "free" in sent[0].lower()


# ---------------------------------------------------------------------------
# Low-balance nudge in credits.py
# ---------------------------------------------------------------------------

class TestLowBalanceNudge:
    def test_nudge_skips_above_threshold(self):
        """_maybe_low_balance_nudge does nothing when balance >= 500."""
        from billing.credits import _maybe_low_balance_nudge

        with patch("storage.supabase_client.select_rows", new_callable=AsyncMock) as mock_sel:
            run(_maybe_low_balance_nudge("sub_cust456", 600))
            mock_sel.assert_not_called()

    def test_nudge_skips_when_recently_notified(self):
        """_maybe_low_balance_nudge skips if notified in the last 24h."""
        from billing.credits import _maybe_low_balance_nudge
        from datetime import datetime, timezone, timedelta

        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_row = {"email": "user@example.com", "low_balance_notified_at": recent}

        with (
            patch("storage.supabase_client.select_rows", new_callable=AsyncMock, return_value=[mock_row]),
            patch("billing.emails.send_low_balance_email", new_callable=AsyncMock) as mock_send,
            patch.dict(os.environ, {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_KEY": "test-key",
            }),
        ):
            run(_maybe_low_balance_nudge("sub_cust789", 100))
            mock_send.assert_not_called()

    def test_nudge_fires_when_never_notified(self):
        """_maybe_low_balance_nudge sends when low and never notified."""
        from billing.credits import _maybe_low_balance_nudge

        mock_row = {"email": "user@example.com", "low_balance_notified_at": None}

        mock_http_client = MagicMock()
        mock_http_resp = MagicMock()
        mock_http_resp.status_code = 204
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.patch = AsyncMock(return_value=mock_http_resp)

        with (
            patch("storage.supabase_client.select_rows", new_callable=AsyncMock, return_value=[mock_row]),
            patch("billing.emails.send_low_balance_email", new_callable=AsyncMock, return_value=True) as mock_send,
            patch("httpx.AsyncClient", return_value=mock_http_client),
            patch.dict(os.environ, {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_KEY": "test-key",
                "RESEND_API_KEY": "re_test",
            }),
        ):
            run(_maybe_low_balance_nudge("sub_cust123", 250))
            mock_send.assert_called_once_with(email="user@example.com", balance=250)

    def test_nudge_skips_when_no_email(self):
        """_maybe_low_balance_nudge skips if account has no email."""
        from billing.credits import _maybe_low_balance_nudge

        mock_row = {"email": "", "low_balance_notified_at": None}

        with (
            patch("storage.supabase_client.select_rows", new_callable=AsyncMock, return_value=[mock_row]),
            patch("billing.emails.send_low_balance_email", new_callable=AsyncMock) as mock_send,
            patch.dict(os.environ, {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_KEY": "test-key",
            }),
        ):
            run(_maybe_low_balance_nudge("sub_cust_noemail", 100))
            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Polar package resolution
# ---------------------------------------------------------------------------

class TestPolarPackages:
    def test_not_configured_when_no_env(self):
        from agent_interface.portal_logic import product_id_for_package
        with patch.dict(os.environ, {"POLAR_PACKAGES": ""}):
            result = product_id_for_package("starter")
        assert result is None

    def test_starter_matched_by_credits(self):
        from agent_interface.portal_logic import product_id_for_package
        pkgs = '{"prod_start": 1000, "prod_grow": 3500, "prod_scale": 13000}'
        with patch.dict(os.environ, {"POLAR_PACKAGES": pkgs}):
            result = product_id_for_package("starter")
        assert result == ("prod_start", 1000)

    def test_growth_matched(self):
        from agent_interface.portal_logic import product_id_for_package
        pkgs = '{"prod_start": 1000, "prod_grow": 3500}'
        with patch.dict(os.environ, {"POLAR_PACKAGES": pkgs}):
            result = product_id_for_package("growth")
        assert result == ("prod_grow", 3500)

    def test_scale_matched(self):
        from agent_interface.portal_logic import product_id_for_package
        pkgs = '{"prod_start": 1000, "prod_grow": 3500, "prod_scale": 13000}'
        with patch.dict(os.environ, {"POLAR_PACKAGES": pkgs}):
            result = product_id_for_package("scale")
        assert result == ("prod_scale", 13000)

    def test_unknown_package_returns_none(self):
        from agent_interface.portal_logic import product_id_for_package
        pkgs = '{"prod_start": 1000}'
        with patch.dict(os.environ, {"POLAR_PACKAGES": pkgs}):
            result = product_id_for_package("enterprise")
        assert result is None

    def test_malformed_env_returns_none(self):
        from agent_interface.portal_logic import product_id_for_package
        with patch.dict(os.environ, {"POLAR_PACKAGES": "not-valid-json"}):
            result = product_id_for_package("starter")
        assert result is None
