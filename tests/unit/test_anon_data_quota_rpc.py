"""
Unit tests for billing/data_quota.py's anon-quota RPC path (2026-09-23 fix).

Covers docs/reviews/2026-09-23-agentbroker-anon-quota-root-cause.md's dormant
bug: the anon-key counter used to be unable to distinguish "I could not check"
from "I checked and it's fine", and both logged at debug. These tests prove,
against KNOWN inputs (all offline -- no network, everything mocked), that:

  1. _classify_rpc_exception buckets a misconfigured credential (permission
     denied, function not deployed) separately from a genuine outage
     (transport failure, 5xx) -- [classify_*]
  2. _verify_response_shape refuses to trust an RPC response that does not
     have the exact shape anon_data_quota_consume contracts to return, rather
     than defaulting to "allowed" -- [shape_*]
  3. _consume_anon_data logs LOUDLY (never at debug) and at a level that
     matches the classification, while still failing OPEN in every case (the
     deliberate availability choice this task was told to keep) -- [consume_*]
  4. The honest-path behaviour (within quota / at quota) still round-trips
     the RPC's own verdict faithfully -- [consume_*]
"""
from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# classify_* -- _classify_rpc_exception on known message shapes
# ---------------------------------------------------------------------------

class TestClassifyRpcException:
    def test_classify_not_configured_is_unconfigured(self):
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError(
            "rpc('anon_data_quota_consume') aborted: SUPABASE_URL or service "
            "key not configured. Cannot proceed on spend path without Supabase."
        )
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "unconfigured"

    def test_classify_transport_error_is_outage(self):
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError(
            "rpc('anon_data_quota_consume') transport error: "
            "ConnectError: [Errno 111] Connection refused"
        )
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "outage"

    @pytest.mark.parametrize("status", [401, 403, 404, 400, 422])
    def test_classify_permission_and_client_errors_are_misconfigured(self, status):
        """401/403 = permission denied; 404 = function not deployed yet
        (exactly what a not-yet-applied 002_*.sql migration returns, verified
        live 2026-09-23: PGRST202); 400/422 = a parameter/shape mismatch this
        workspace's own code caused. None of these are "try again later"."""
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError(
            f"rpc('anon_data_quota_consume') failed: HTTP {status} "
            f"body={{\"code\":\"PGRST202\"}}"
        )
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "misconfigured", f"HTTP {status} should classify as misconfigured"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_classify_server_errors_are_outage(self, status):
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError(f"rpc('anon_data_quota_consume') failed: HTTP {status} body=")
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "outage"

    def test_classify_json_decode_error_is_outage_not_misconfigured(self):
        """A 2xx whose body isn't JSON (e.g. a WAF/proxy interstitial) is not
        a credential/grant problem this workspace can fix in SQL -- treated
        as outage, never invented as "misconfigured" without real evidence."""
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError(
            "rpc('anon_data_quota_consume') JSON decode error: "
            "Expecting value: line 1 column 1 (char 0) body=<html>..."
        )
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "outage"

    def test_classify_unrecognised_message_defaults_to_outage_not_misconfigured(self):
        """Never invent 'misconfigured' (a human-actionable, alarming
        classification) from a message shape this function does not
        recognise -- default to the less alarming bucket."""
        from billing.data_quota import _classify_rpc_exception
        exc = RuntimeError("some completely unexpected failure shape")
        kind, _ = _classify_rpc_exception(exc)
        assert kind == "outage"


# ---------------------------------------------------------------------------
# shape_* -- _verify_response_shape refuses to trust a malformed payload
# ---------------------------------------------------------------------------

class TestVerifyResponseShape:
    def test_shape_valid_payload_passes_through(self):
        from billing.data_quota import _verify_response_shape
        payload = {"allowed": True, "remaining": 42, "count": 8}
        assert _verify_response_shape(payload) == payload

    @pytest.mark.parametrize("bad_payload", [
        None,
        [],
        {},
        {"allowed": True},                                   # missing remaining/count
        {"allowed": "true", "remaining": 1, "count": 1},      # allowed not bool
        {"allowed": True, "remaining": "1", "count": 1},      # remaining not int
        {"allowed": True, "remaining": 1, "count": None},     # count not int
        "allowed",                                            # not even a dict
    ])
    def test_shape_malformed_payload_raises(self, bad_payload):
        from billing.data_quota import _verify_response_shape, _AnonQuotaRpcFailure
        with pytest.raises(_AnonQuotaRpcFailure) as exc_info:
            _verify_response_shape(bad_payload)
        assert exc_info.value.kind == "bad_response_shape"


# ---------------------------------------------------------------------------
# consume_* -- _consume_anon_data: loud + distinguishable logs, fail-open
# ---------------------------------------------------------------------------

class TestConsumeAnonDataLogging:
    """Every failure path must fail OPEN (never blocks a real caller because
    OUR infra is broken -- unchanged design choice) but must never again log
    at debug for a case that used to be silent. Asserts both the return value
    and the actual log record emitted."""

    @pytest.mark.asyncio
    async def test_misconfigured_credential_fails_open_and_logs_at_error(self, caplog):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with patch(
                "storage.supabase_client.rpc", new_callable=AsyncMock,
                side_effect=RuntimeError(
                    "rpc('anon_data_quota_consume') failed: HTTP 404 "
                    "body={\"code\":\"PGRST202\"}"
                ),
            ):
                with caplog.at_level(logging.DEBUG, logger="smb_broker.data_quota"):
                    from billing.data_quota import _consume_anon_data
                    allowed, remaining = await _consume_anon_data("1.2.3.4")

        assert allowed is True, "must still fail OPEN on a misconfigured credential"
        assert remaining == 100
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "a misconfigured credential must log at ERROR, not debug"
        msg = error_records[0].getMessage()
        assert "kind=misconfigured" in msg
        assert not any(
            r.levelno < logging.WARNING and "misconfigured" in r.getMessage()
            for r in caplog.records
        ), "the misconfigured-credential path must not ALSO log a quiet debug copy"

    @pytest.mark.asyncio
    async def test_outage_fails_open_and_logs_at_warning_not_debug(self, caplog):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with patch(
                "storage.supabase_client.rpc", new_callable=AsyncMock,
                side_effect=RuntimeError(
                    "rpc('anon_data_quota_consume') transport error: timed out"
                ),
            ):
                with caplog.at_level(logging.DEBUG, logger="smb_broker.data_quota"):
                    from billing.data_quota import _consume_anon_data
                    allowed, remaining = await _consume_anon_data("1.2.3.5")

        assert allowed is True, "must still fail OPEN on a genuine outage"
        assert remaining == 100
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn_records, "an outage must log at WARNING, not debug"
        assert "kind=outage" in warn_records[0].getMessage()
        assert not any(r.levelno >= logging.ERROR for r in caplog.records), (
            "an outage is not the same severity as a misconfiguration -- must not "
            "also log at ERROR"
        )

    @pytest.mark.asyncio
    async def test_misconfigured_and_outage_are_distinguishable_from_each_other(self, caplog):
        """The exact regression this task closes: today both cases are
        IDENTICAL in the logs (both debug, both silent). Prove they are not,
        by comparing the two records this test produces directly."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            from billing.data_quota import _consume_anon_data
            with caplog.at_level(logging.DEBUG, logger="smb_broker.data_quota"):
                with patch(
                    "storage.supabase_client.rpc", new_callable=AsyncMock,
                    side_effect=RuntimeError(
                        "rpc('anon_data_quota_consume') failed: HTTP 401 body=denied"
                    ),
                ):
                    await _consume_anon_data("9.9.9.1")
                with patch(
                    "storage.supabase_client.rpc", new_callable=AsyncMock,
                    side_effect=RuntimeError(
                        "rpc('anon_data_quota_consume') failed: HTTP 503 body=down"
                    ),
                ):
                    await _consume_anon_data("9.9.9.2")

        misconfig_records = [r for r in caplog.records if "kind=misconfigured" in r.getMessage()]
        outage_records = [r for r in caplog.records if "kind=outage" in r.getMessage()]
        assert misconfig_records and outage_records
        assert misconfig_records[0].levelno != outage_records[0].levelno, (
            "misconfiguration and outage must not render at the same log level"
        )
        assert misconfig_records[0].levelno > outage_records[0].levelno, (
            "misconfiguration (a real, persistent defect) must be at least as "
            "loud as outage (transient, expected)"
        )

    @pytest.mark.asyncio
    async def test_bad_response_shape_fails_open_and_logs_at_error(self, caplog):
        """Even a 2xx, no-exception response is verified before being
        trusted -- this is the second half of 'verify its own write'."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with patch(
                "storage.supabase_client.rpc", new_callable=AsyncMock,
                return_value={"unexpected": "shape"},
            ):
                with caplog.at_level(logging.DEBUG, logger="smb_broker.data_quota"):
                    from billing.data_quota import _consume_anon_data
                    allowed, remaining = await _consume_anon_data("1.2.3.6")

        assert allowed is True
        assert remaining == 100
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records
        assert "bad_response_shape" in error_records[0].getMessage()

    @pytest.mark.asyncio
    async def test_no_supabase_config_is_silent_and_fast(self, caplog):
        """Unconfigured (no SUPABASE_URL/key at all) is expected in dev/test
        and stays quiet -- unchanged behaviour, not the regression this task
        closes."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_KEY": "",
            "SUPABASE_ANON_KEY": "",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with caplog.at_level(logging.DEBUG, logger="smb_broker.data_quota"):
                from billing.data_quota import _consume_anon_data
                allowed, remaining = await _consume_anon_data("1.2.3.7")

        assert allowed is True
        assert remaining == 100
        assert not caplog.records, "no-config is an expected dev/test condition, not a defect"


# ---------------------------------------------------------------------------
# consume_* -- the RPC's own verdict is round-tripped faithfully
# ---------------------------------------------------------------------------

class TestConsumeAnonDataHonestRoundTrip:
    @pytest.mark.asyncio
    async def test_allowed_true_passes_through_remaining(self):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with patch(
                "storage.supabase_client.rpc", new_callable=AsyncMock,
                return_value={"allowed": True, "remaining": 63, "count": 37},
            ):
                from billing.data_quota import _consume_anon_data
                allowed, remaining = await _consume_anon_data("2.2.2.2")
        assert (allowed, remaining) == (True, 63)

    @pytest.mark.asyncio
    async def test_allowed_false_returns_zero_remaining(self):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "100",
        }):
            with patch(
                "storage.supabase_client.rpc", new_callable=AsyncMock,
                return_value={"allowed": False, "remaining": 0, "count": 100},
            ):
                from billing.data_quota import _consume_anon_data
                allowed, remaining = await _consume_anon_data("2.2.2.3")
        assert (allowed, remaining) == (False, 0)

    @pytest.mark.asyncio
    async def test_rpc_is_called_with_the_expected_parameters(self):
        """The exact parameter names anon_data_quota_consume's SQL signature
        expects (sql/agentbroker/002_anon_data_quota_security_definer_rpc.sql)
        -- a name mismatch here would silently 404 in production."""
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-key",
            "ANON_DATA_QUOTA_PER_DAY": "77",
        }):
            mock_rpc = AsyncMock(return_value={"allowed": True, "remaining": 76, "count": 1})
            with patch("storage.supabase_client.rpc", mock_rpc):
                from billing.data_quota import _consume_anon_data
                await _consume_anon_data("3.3.3.3")

        mock_rpc.assert_awaited_once()
        fn_name, payload = mock_rpc.await_args.args
        assert fn_name == "anon_data_quota_consume"
        assert set(payload.keys()) == {"p_bucket_key", "p_quota_date", "p_limit"}
        assert payload["p_limit"] == 77
        assert isinstance(payload["p_bucket_key"], str) and len(payload["p_bucket_key"]) == 64
        assert isinstance(payload["p_quota_date"], str)
