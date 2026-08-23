"""
Unit tests — Twilio dual-auth + sender resolution (2026-08-24).

Proves:
  (A) API-Key mode: Client is constructed as Client(api_key_sid, api_key_secret, account_sid)
      when TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET / TWILIO_ACCOUNT_SID are all set.
  (B) Legacy mode: Client is constructed as Client(account_sid, auth_token) when only
      TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN are set (and no API-Key vars).
  (C) No-creds -> honest channel_not_configured failure (success=False, nothing sent).
  (D) Creds present but no sender (neither TWILIO_MESSAGING_SERVICE_SID nor
      TWILIO_FROM_NUMBER) -> honest channel_not_configured/no_sender failure.

Design notes:
  - Twilio is not installed in the local dev environment.  A mock `twilio` package is
    injected into sys.modules at module load time so that `from twilio.rest import Client`
    inside TwilioSMSAdapter._build_client() resolves without error.  All actual Client
    calls are intercepted per-test via patch("twilio.rest.Client").
  - compliance.pre_check.pre_check is bound at adapter-module-import time, so all send()
    calls patch "channels.sms_email.twilio_sms.pre_check" to a no-op.  This isolates
    auth/sender routing from 10DLC / consent checks.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Inject a stub 'twilio' package so import works without the real library.
# ---------------------------------------------------------------------------
if "twilio" not in sys.modules:
    _fake_twilio = MagicMock(name="twilio")
    _fake_twilio_rest = MagicMock(name="twilio.rest")
    _fake_twilio.rest = _fake_twilio_rest
    sys.modules["twilio"] = _fake_twilio
    sys.modules["twilio.rest"] = _fake_twilio_rest

from channels.adapter_interface import ChannelRequest  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_request():
    return ChannelRequest(
        recipient_id="+14155551234",
        channel="sms",
        message_type="transactional",
        content="Hello from AgentBroker",
        agent_id="test_agent",
        trace_id="tr_test_dual_auth_001",
    )


def _mock_message(sid="SMtest123", status="sent"):
    msg = MagicMock()
    msg.sid = sid
    msg.status = status
    return msg


def _adapter_for_env(env: dict):
    """Return a fresh TwilioSMSAdapter instantiated under the given env vars."""
    with patch.dict(os.environ, env, clear=False):
        from channels.sms_email import twilio_sms as m
        importlib.reload(m)
        return m.TwilioSMSAdapter()


def _send(adapter, env: dict):
    """Call adapter.send() with pre_check mocked out and env applied."""
    with patch.dict(os.environ, env, clear=False), \
         patch("channels.sms_email.twilio_sms.pre_check", return_value=None):
        return run(adapter.send(_make_request()))


# ---------------------------------------------------------------------------
# (A) API-Key mode
# ---------------------------------------------------------------------------

class TestAPIKeyMode:
    """API-Key auth: Client must be called as Client(api_key_sid, api_key_secret, account_sid)."""

    _env = {
        "TWILIO_API_KEY_SID": "SKtest_key_sid_0000000000000000",
        "TWILIO_API_KEY_SECRET": "test_key_secret_value",
        "TWILIO_ACCOUNT_SID": "ACtest_account_sid_0000000000000",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_MESSAGING_SERVICE_SID": "MGtest_service_sid",
        "TWILIO_FROM_NUMBER": "",
    }

    def test_api_key_mode_detected(self):
        assert _adapter_for_env(self._env)._auth_mode() == "api_key"

    def test_client_constructed_with_3_positional_args(self):
        """Client MUST be called as Client(api_key_sid, api_key_secret, account_sid)."""
        adapter = _adapter_for_env(self._env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, self._env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance) as MockClient:
            result = run(adapter.send(_make_request()))

        MockClient.assert_called_once_with(
            "SKtest_key_sid_0000000000000000",
            "test_key_secret_value",
            "ACtest_account_sid_0000000000000",
        )
        assert result.success is True
        assert result.provider_message_id == "SMtest123"

    def test_api_key_mode_uses_messaging_service_sid(self):
        """messaging_service_sid is passed to messages.create (not from_)."""
        adapter = _adapter_for_env(self._env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, self._env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        kwargs = mock_instance.messages.create.call_args[1]
        assert kwargs.get("messaging_service_sid") == "MGtest_service_sid"
        assert "from_" not in kwargs

    def test_api_key_mode_falls_back_to_from_number(self):
        """When no messaging_service_sid, from_ is used even in API-Key mode."""
        env = dict(self._env)
        env["TWILIO_MESSAGING_SERVICE_SID"] = ""
        env["TWILIO_FROM_NUMBER"] = "+15005550006"

        adapter = _adapter_for_env(env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        kwargs = mock_instance.messages.create.call_args[1]
        assert kwargs.get("from_") == "+15005550006"
        assert "messaging_service_sid" not in kwargs

    def test_api_key_health_check_is_true(self):
        assert run(_adapter_for_env(self._env).health_check()) is True


# ---------------------------------------------------------------------------
# (B) Legacy mode
# ---------------------------------------------------------------------------

class TestLegacyMode:
    """Legacy auth: Client must be called as Client(account_sid, auth_token)."""

    _env = {
        "TWILIO_API_KEY_SID": "",
        "TWILIO_API_KEY_SECRET": "",
        "TWILIO_ACCOUNT_SID": "AClegacy_account_sid_000000000000",
        "TWILIO_AUTH_TOKEN": "legacy_auth_token_value",
        "TWILIO_MESSAGING_SERVICE_SID": "MGlegacy_service_sid",
        "TWILIO_FROM_NUMBER": "",
    }

    def test_legacy_mode_detected(self):
        assert _adapter_for_env(self._env)._auth_mode() == "legacy"

    def test_client_constructed_with_2_positional_args(self):
        """Client MUST be called as Client(account_sid, auth_token) in legacy mode."""
        adapter = _adapter_for_env(self._env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, self._env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance) as MockClient:
            result = run(adapter.send(_make_request()))

        MockClient.assert_called_once_with(
            "AClegacy_account_sid_000000000000",
            "legacy_auth_token_value",
        )
        assert result.success is True

    def test_legacy_mode_uses_messaging_service_sid(self):
        """Legacy mode also passes messaging_service_sid when set."""
        adapter = _adapter_for_env(self._env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, self._env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        kwargs = mock_instance.messages.create.call_args[1]
        assert kwargs.get("messaging_service_sid") == "MGlegacy_service_sid"
        assert "from_" not in kwargs

    def test_legacy_mode_falls_back_to_from_number(self):
        """When messaging_service_sid is absent, from_ is used in legacy mode."""
        env = dict(self._env)
        env["TWILIO_MESSAGING_SERVICE_SID"] = ""
        env["TWILIO_FROM_NUMBER"] = "+15005550006"

        adapter = _adapter_for_env(env)
        mock_instance = MagicMock()
        mock_instance.messages.create.return_value = _mock_message()

        with patch.dict(os.environ, env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        kwargs = mock_instance.messages.create.call_args[1]
        assert kwargs.get("from_") == "+15005550006"
        assert "messaging_service_sid" not in kwargs

    def test_legacy_health_check_is_true(self):
        assert run(_adapter_for_env(self._env).health_check()) is True

    def test_api_key_sid_only_does_not_trigger_api_key_mode(self):
        """API-Key mode requires ALL THREE vars; partial set falls through to legacy."""
        env = dict(self._env)
        env["TWILIO_API_KEY_SID"] = "SKpartial_only_no_secret"
        # TWILIO_API_KEY_SECRET is still "" -> must NOT enter api_key mode
        adapter = _adapter_for_env(env)
        assert adapter._auth_mode() == "legacy"


# ---------------------------------------------------------------------------
# (C) No credentials -> honest failure
# ---------------------------------------------------------------------------

class TestNoCreds:
    """With no auth credentials the adapter must return an honest failure; never fake success."""

    _env = {
        "TWILIO_API_KEY_SID": "",
        "TWILIO_API_KEY_SECRET": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_MESSAGING_SERVICE_SID": "",
        "TWILIO_FROM_NUMBER": "",
        "ALLOW_STUB_CHANNELS": "",
        "RENDER": "true",   # forces prod mode so stubs are unconditionally off
    }

    def test_auth_mode_is_none(self):
        assert _adapter_for_env(self._env)._auth_mode() == "none"

    def test_health_check_is_false(self):
        assert run(_adapter_for_env(self._env).health_check()) is False

    def test_returns_failure_not_success(self):
        result = _send(_adapter_for_env(self._env), self._env)
        assert result.success is False

    def test_error_code_is_channel_not_configured(self):
        result = _send(_adapter_for_env(self._env), self._env)
        assert result.error_code == "channel_not_configured"

    def test_provider_message_id_is_none(self):
        result = _send(_adapter_for_env(self._env), self._env)
        assert result.provider_message_id is None

    def test_error_message_mentions_twilio(self):
        result = _send(_adapter_for_env(self._env), self._env)
        assert "twilio" in result.error_message.lower()

    def test_error_message_confirms_nothing_was_sent(self):
        result = _send(_adapter_for_env(self._env), self._env)
        assert "nothing was sent" in result.error_message.lower()

    def test_twilio_client_is_never_constructed(self):
        """No Client() call may happen when auth is not configured."""
        adapter = _adapter_for_env(self._env)

        with patch.dict(os.environ, self._env, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client") as MockClient:
            run(adapter.send(_make_request()))

        MockClient.assert_not_called()


# ---------------------------------------------------------------------------
# (D) Credentials present but no sender configured
# ---------------------------------------------------------------------------

class TestCredsButNoSender:
    """Auth configured but no sender -> honest failure. messages.create must not be called."""

    _env_api_key = {
        "TWILIO_API_KEY_SID": "SKtest_key_sid_0000000000000000",
        "TWILIO_API_KEY_SECRET": "test_key_secret_value",
        "TWILIO_ACCOUNT_SID": "ACtest_account_sid_0000000000000",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_MESSAGING_SERVICE_SID": "",
        "TWILIO_FROM_NUMBER": "",
        "ALLOW_STUB_CHANNELS": "",
        "RENDER": "true",
    }

    _env_legacy = {
        "TWILIO_API_KEY_SID": "",
        "TWILIO_API_KEY_SECRET": "",
        "TWILIO_ACCOUNT_SID": "AClegacy_account_sid_000000000000",
        "TWILIO_AUTH_TOKEN": "legacy_auth_token_value",
        "TWILIO_MESSAGING_SERVICE_SID": "",
        "TWILIO_FROM_NUMBER": "",
        "ALLOW_STUB_CHANNELS": "",
        "RENDER": "true",
    }

    def test_api_key_mode_no_sender_returns_failure(self):
        result = _send(_adapter_for_env(self._env_api_key), self._env_api_key)
        assert result.success is False

    def test_api_key_mode_no_sender_error_code(self):
        result = _send(_adapter_for_env(self._env_api_key), self._env_api_key)
        assert result.error_code == "channel_not_configured"

    def test_api_key_mode_no_sender_error_mentions_sender_vars(self):
        result = _send(_adapter_for_env(self._env_api_key), self._env_api_key)
        msg = result.error_message.lower()
        assert "messaging_service_sid" in msg or "from_number" in msg or "sender" in msg

    def test_legacy_mode_no_sender_returns_failure(self):
        result = _send(_adapter_for_env(self._env_legacy), self._env_legacy)
        assert result.success is False

    def test_legacy_mode_no_sender_error_code(self):
        result = _send(_adapter_for_env(self._env_legacy), self._env_legacy)
        assert result.error_code == "channel_not_configured"

    def test_messages_create_never_called_when_no_sender(self):
        """Even though auth is valid, messages.create must not be invoked without a sender."""
        adapter = _adapter_for_env(self._env_api_key)
        mock_instance = MagicMock()

        with patch.dict(os.environ, self._env_api_key, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        mock_instance.messages.create.assert_not_called()

    def test_legacy_messages_create_never_called_when_no_sender(self):
        adapter = _adapter_for_env(self._env_legacy)
        mock_instance = MagicMock()

        with patch.dict(os.environ, self._env_legacy, clear=False), \
             patch("channels.sms_email.twilio_sms.pre_check", return_value=None), \
             patch("twilio.rest.Client", return_value=mock_instance):
            run(adapter.send(_make_request()))

        mock_instance.messages.create.assert_not_called()
