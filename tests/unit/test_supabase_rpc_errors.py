"""
storage.supabase_client.rpc / rpc_sync must say WHY a call failed.

BACKGROUND (tm_requirements row 358, 2026-10-03). The live log of the VPS container had
`usage_log_failed` lines for 13 failed usage_events writes. Each said only
`rpc('usage_events_insert_v2') transport error: ` -- nothing after the colon -- because an httpx
timeout has an EMPTY message. Whether the cause was a timeout, a refused connection or a TLS failure
could not be read from the log, so the cause had to be reconstructed from the database proxy's logs:
stalls of PostgREST's connection pool (PGRST003, HTTP 504 after its 10 second acquisition timeout)
with the client's own 10 second timeout expiring at the same moment for some of the writes.

These tests fake httpx itself (no network, and no patching of the rpc function other tests stub).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from storage import supabase_client as sb


class _Resp:
    def __init__(self, status, body="", json_value=None, json_error=False):
        self.status_code = status
        self.text = body
        self._json_value = json_value
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._json_value


def _fake_async_client(outcome):
    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
    return Client


def _fake_sync_client(outcome):
    class Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
    return Client


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.example.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-test-key")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- transport

def test_a_timeout_names_itself_instead_of_leaving_an_empty_message(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(httpx.ReadTimeout("")))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("usage_events_insert_v2", {}))
    err = info.value
    assert err.kind == "transport"
    assert err.cause_name == "ReadTimeout"
    assert str(err) == "rpc('usage_events_insert_v2') transport error: ReadTimeout"


def test_a_transport_error_with_text_keeps_the_text(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient",
                        _fake_async_client(httpx.ConnectError("[Errno 111] Connection refused")))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("f", {}))
    assert info.value.cause_name == "ConnectError"
    assert str(info.value) == "rpc('f') transport error: ConnectError: [Errno 111] Connection refused"


def test_it_is_still_a_runtime_error_and_still_matches_the_classifier(monkeypatch):
    """billing/data_quota._classify_rpc_exception reads message substrings; the format it reads
    must survive."""
    from billing.data_quota import _classify_rpc_exception
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(httpx.ReadTimeout("")))
    with pytest.raises(RuntimeError) as info:
        _run(sb.rpc("anon_data_quota_consume", {}))
    assert isinstance(info.value, sb.RpcError)
    kind, _ = _classify_rpc_exception(info.value)
    assert kind == "outage"


# --------------------------------------------------------------------------- http

def test_the_postgrest_pool_timeout_is_recognised_by_its_code(monkeypatch):
    body = '{"code":"PGRST003","details":null,"hint":null,"message":"Timed out acquiring connection from connection pool."}'
    resp = _Resp(504, body, json_value={"code": "PGRST003", "message": "Timed out"})
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(resp))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("usage_events_insert_v2", {}))
    err = info.value
    assert (err.kind, err.status, err.pg_code) == ("http", 504, "PGRST003")
    assert "HTTP 504" in str(err)


def test_an_http_error_with_a_non_json_body_has_no_code_and_does_not_crash(monkeypatch):
    resp = _Resp(502, "<html>bad gateway</html>", json_error=True)
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(resp))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("f", {}))
    assert (info.value.kind, info.value.status, info.value.pg_code) == ("http", 502, None)


def test_a_json_body_that_is_not_an_object_has_no_code(monkeypatch):
    resp = _Resp(500, "[]", json_value=[])
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(resp))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("f", {}))
    assert info.value.pg_code is None


def test_an_absurd_code_is_not_trusted(monkeypatch):
    resp = _Resp(500, "x", json_value={"code": "x" * 500})
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(resp))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("f", {}))
    assert info.value.pg_code is None


def test_a_2xx_with_a_body_that_is_not_json_is_a_decode_error(monkeypatch):
    resp = _Resp(200, "<html>interstitial</html>", json_error=True)
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(resp))
    with pytest.raises(sb.RpcError) as info:
        _run(sb.rpc("f", {}))
    assert info.value.kind == "decode"
    assert "JSON decode error" in str(info.value)


def test_success_is_unchanged(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _fake_async_client(_Resp(201, "{}", json_value={"id": 7})))
    assert _run(sb.rpc("f", {})) == {"id": 7}


def test_unconfigured_is_still_a_plain_runtime_error(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(RuntimeError) as info:
        _run(sb.rpc("f", {}))
    assert not isinstance(info.value, sb.RpcError)
    assert "not configured" in str(info.value)


# --------------------------------------------------------------------------- the sync twin

def test_rpc_sync_names_a_timeout_too(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _fake_sync_client(httpx.ConnectTimeout("")))
    with pytest.raises(sb.RpcError) as info:
        sb.rpc_sync("optout_hydrate", {})
    assert info.value.kind == "transport" and info.value.cause_name == "ConnectTimeout"
    assert str(info.value) == "rpc_sync('optout_hydrate') transport error: ConnectTimeout"


def test_rpc_sync_http_error_carries_status_and_code(monkeypatch):
    resp = _Resp(504, "x", json_value={"code": "PGRST003"})
    monkeypatch.setattr(httpx, "Client", _fake_sync_client(resp))
    with pytest.raises(sb.RpcError) as info:
        sb.rpc_sync("f", {})
    assert (info.value.kind, info.value.status, info.value.pg_code) == ("http", 504, "PGRST003")
