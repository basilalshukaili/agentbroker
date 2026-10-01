"""Who is the caller - and does a key holder get a bucket of their own?

THE FIELD CASE (audit 2026-09-30, fixes 4 and 5): hatchloop.dev proxied /mcp/* through the Next.js app
on :3000, so the origin saw every caller as the box's own address. One 60-burst / 1-per-second bucket
served them all (17 HTTP 429s in four days, including one on a scanner's burst that a key holder shared),
and `ip_hash` in usage telemetry was the same value on 16,357 of 19,405 rows.

Two fixes live in the app (the Caddy half is deploy/caddy):
  * the address is believed only through a trusted proxy, and read from the right;
  * a caller holding a correctly signed key is limited by THAT KEY, not by an address it shares.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from agent_interface.identity import issue_token, TokenRequest, peek_agent_id
from core import client_ip as cip


def _req(peer, xff=None, real=None, extra=None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if real is not None:
        headers.append((b"x-real-ip", real.encode()))
    for k, v in (extra or {}).items():
        headers.append((k.encode(), v.encode()))
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": headers,
                    "client": (peer, 5555) if peer else None, "query_string": b""})


# ---------------------------------------------------------------------------
# address resolution
# ---------------------------------------------------------------------------

def test_a_direct_untrusted_peer_cannot_choose_its_own_address():
    """Anyone who can reach the port directly could previously claim any bucket they liked."""
    assert cip.resolve_client_ip("203.0.113.9", "1.2.3.4") == "203.0.113.9"
    assert cip.resolve_client_ip("203.0.113.9", "1.2.3.4", real_ip="5.6.7.8") == "203.0.113.9"


def test_through_a_trusted_proxy_the_forwarded_address_is_the_client():
    # Caddy on the host reaches the container from the Docker bridge gateway.
    assert cip.resolve_client_ip("172.17.0.1", "198.51.100.20") == "198.51.100.20"
    assert cip.resolve_client_ip("127.0.0.1", "198.51.100.20") == "198.51.100.20"


def test_the_chain_is_read_from_the_right_so_a_spoofed_left_hop_is_ignored():
    """The caller sends 'X-Forwarded-For: 9.9.9.9'; a proxy that APPENDS yields '9.9.9.9, <real>'.
    The old 'first hop wins' rule attributed the request to 9.9.9.9."""
    assert cip.resolve_client_ip("172.17.0.1", "9.9.9.9, 198.51.100.20") == "198.51.100.20"
    assert cip.resolve_client_ip("172.17.0.1", "9.9.9.9, 198.51.100.20, 10.0.0.7") == "198.51.100.20"


def test_trusted_hops_are_skipped_not_returned():
    assert cip.resolve_client_ip("172.17.0.1", "10.1.1.1, 192.168.0.4") == "10.1.1.1"   # all internal: nearest
    assert cip.resolve_client_ip("172.17.0.1", "203.0.113.50, 10.1.1.1") == "203.0.113.50"


def test_ipv6_and_ports_and_garbage():
    assert cip.resolve_client_ip("::1", "2001:db8::7") == "2001:db8::7"
    assert cip.resolve_client_ip("127.0.0.1", "198.51.100.20:4433") == "198.51.100.20"
    assert cip.resolve_client_ip("127.0.0.1", "not-an-ip, 198.51.100.21") == "198.51.100.21"
    assert cip.resolve_client_ip("127.0.0.1", "garbage") == "127.0.0.1"


def test_a_trusted_peer_with_no_forwarding_is_the_peer():
    assert cip.resolve_client_ip("172.17.0.1", None) == "172.17.0.1"
    assert cip.resolve_client_ip("127.0.0.1", "") == "127.0.0.1"


def test_x_real_ip_is_used_only_through_a_trusted_proxy():
    assert cip.resolve_client_ip("172.17.0.1", None, real_ip="198.51.100.30") == "198.51.100.30"
    assert cip.resolve_client_ip("203.0.113.9", None, real_ip="198.51.100.30") == "203.0.113.9"


def test_no_socket_peer_falls_back_to_the_headers_then_the_name():
    """In-process / test clients: starlette's TestClient reports the peer as 'testclient'."""
    assert cip.resolve_client_ip("testclient", "198.51.100.40") == "198.51.100.40"
    assert cip.resolve_client_ip("testclient", None) == "testclient"
    assert cip.resolve_client_ip(None, None) == "unknown"


def test_first_hop_never_returns_the_chain():
    assert cip.first_hop("203.0.113.5, 10.0.0.1") == "203.0.113.5"
    assert cip.first_hop("") == "" and cip.first_hop("nope") == ""


def test_trusted_proxies_are_configurable_and_default_is_non_empty(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "203.0.113.0/24")
    cip._parse_networks.cache_clear()
    try:
        assert cip.is_trusted_proxy("203.0.113.9") is True
        assert cip.is_trusted_proxy("172.17.0.1") is False
        assert cip.resolve_client_ip("203.0.113.9", "198.51.100.1") == "198.51.100.1"
    finally:
        monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)
        cip._parse_networks.cache_clear()
    assert cip.is_trusted_proxy("172.17.0.1") is True


def test_the_knob_does_not_become_a_required_deploy_variable():
    """scripts/check_deploy_env.py treats a getenv with a NON-EMPTY literal default as optional. A
    default of '' would make the next deploy demand TRUSTED_PROXY_CIDRS in the container env."""
    import re
    src = open(cip.__file__, encoding="utf-8").read()
    m = re.search(r'os\.getenv\("TRUSTED_PROXY_CIDRS",\s*([^)]+)\)', src)
    assert m and m.group(1).strip() not in ('""', "''", ""), m.group(1)


def test_main_rl_client_ip_uses_the_resolver():
    import main
    assert main._rl_client_ip(_req("203.0.113.9", xff="1.1.1.1")) == "203.0.113.9"
    assert main._rl_client_ip(_req("172.17.0.1", xff="1.1.1.1, 198.51.100.8")) == "198.51.100.8"
    assert main._rl_client_ip(_req("testclient", xff="198.51.100.9")) == "198.51.100.9"


# ---------------------------------------------------------------------------
# peek_agent_id: the I/O-free signature check the limiter uses
# ---------------------------------------------------------------------------

def test_peek_agent_id_accepts_only_a_signed_unexpired_key():
    good = issue_token(TokenRequest(agent_id="free_peek_ok", principal_id="p")).token
    assert peek_agent_id(good) == "free_peek_ok"
    assert peek_agent_id(issue_token(TokenRequest(agent_id="x", principal_id="p", ttl_seconds=-5)).token) is None
    payload, _ = good.split(".")
    assert peek_agent_id(payload + "." + "0" * 64) is None
    for junk in (None, "", "env:KEY", "a.b.c", 123, b"x"):
        assert peek_agent_id(junk) is None            # type: ignore[arg-type]


def test_peek_does_no_io(monkeypatch):
    """It runs on the event loop for every /mcp request; the revocation lists can hydrate over the
    network, so it must not touch them."""
    from agent_interface import identity
    monkeypatch.setattr(identity, "is_jti_revoked",
                        lambda *_: (_ for _ in ()).throw(AssertionError("peek consulted revocation")))
    monkeypatch.setattr(identity, "is_customer_revoked",
                        lambda *_: (_ for _ in ()).throw(AssertionError("peek consulted revocation")))
    good = issue_token(TokenRequest(agent_id="free_peek_io", principal_id="p")).token
    assert peek_agent_id(good) == "free_peek_io"


# ---------------------------------------------------------------------------
# the bucket
# ---------------------------------------------------------------------------

def test_bucket_is_the_key_for_a_valid_key_and_the_address_otherwise():
    import main
    good = issue_token(TokenRequest(agent_id="free_bucket_a", principal_id="p")).token
    assert main._rl_bucket_for(_req("172.17.0.1", "198.51.100.5", extra={"x-agent-identity": good}),
                               "198.51.100.5") == ("key:free_bucket_a", "free_bucket_a")
    assert main._rl_bucket_for(_req("172.17.0.1", "198.51.100.5"), "198.51.100.5") == (
        "ip:198.51.100.5", None)
    # a key presented in the other two accepted places counts too (hosted connectors only send these)
    assert main._rl_bucket_for(_req("172.17.0.1", "1.1.1.1", extra={"authorization": "Bearer " + good}),
                               "1.1.1.1")[0] == "key:free_bucket_a"
    assert main._rl_bucket_for(_req("172.17.0.1", "1.1.1.1", extra={"x-api-key": good}),
                               "1.1.1.1")[0] == "key:free_bucket_a"


def test_a_forged_or_placeholder_key_does_not_earn_a_private_bucket():
    import main
    good = issue_token(TokenRequest(agent_id="free_bucket_b", principal_id="p")).token
    forged = good.split(".")[0] + "." + "0" * 64
    for bad in (forged, "env:KEY", "Bearer x", "garbage"):
        assert main._rl_bucket_for(_req("172.17.0.1", "198.51.100.6", extra={"x-agent-identity": bad}),
                                   "198.51.100.6") == ("ip:198.51.100.6", None)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    main._rl_buckets.clear()
    c = TestClient(main.app, raise_server_exceptions=False)
    yield c
    main._rl_buckets.clear()


def _ping(client, **headers):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers).status_code


def test_a_scanner_exhausting_its_address_does_not_lock_out_a_key_holder_on_the_same_address(client):
    """THE SHARED-BUCKET BUG: behind the proxy every caller was one address."""
    import main
    shared = {"x-forwarded-for": "198.51.100.77"}
    for _ in range(int(main._RL_BUCKET_SIZE) + 5):
        _ping(client, **shared)
    assert _ping(client, **shared) == 429, "the anonymous bucket must be exhausted for this to mean anything"

    key = issue_token(TokenRequest(agent_id="free_holder_one", principal_id="p")).token
    assert _ping(client, **shared, **{"x-agent-identity": key}) == 200, (
        "a key holder was refused because an anonymous scanner on the same address spent the tokens")


def test_two_key_holders_do_not_share_a_bucket_and_a_noisy_one_only_limits_itself(client):
    import main
    a = issue_token(TokenRequest(agent_id="free_holder_a", principal_id="p")).token
    b = issue_token(TokenRequest(agent_id="free_holder_b", principal_id="p")).token
    shared = {"x-forwarded-for": "198.51.100.88"}
    for _ in range(int(main._RL_BUCKET_SIZE) + 5):
        _ping(client, **shared, **{"x-agent-identity": a})
    assert _ping(client, **shared, **{"x-agent-identity": a}) == 429
    assert _ping(client, **shared, **{"x-agent-identity": b}) == 200


def test_a_placeholder_key_is_limited_like_an_anonymous_caller(client):
    import main
    shared = {"x-forwarded-for": "198.51.100.99"}
    for _ in range(int(main._RL_BUCKET_SIZE) + 5):
        _ping(client, **shared)
    assert _ping(client, **shared, **{"x-agent-identity": "env:KEY"}) == 429


def test_different_addresses_still_have_different_buckets(client):
    import main
    for _ in range(int(main._RL_BUCKET_SIZE) + 5):
        _ping(client, **{"x-forwarded-for": "198.51.100.1"})
    assert _ping(client, **{"x-forwarded-for": "198.51.100.1"}) == 429
    assert _ping(client, **{"x-forwarded-for": "198.51.100.2"}) == 200


def test_the_caller_key_published_to_handlers_is_the_bucket_and_the_ip_is_separate(client, monkeypatch):
    """find_business limits per caller via CALLER_KEY; telemetry wants the address via CALLER_IP."""
    from core.caller_context import CALLER_KEY, CALLER_IP
    seen = {}
    import agent_interface.mcp_server as m
    real = m.handle_mcp_request

    async def spy(payload, headers=None, profile=None):
        seen["key"], seen["ip"] = CALLER_KEY.get(), CALLER_IP.get()
        return await real(payload, headers=headers, profile=profile)
    import main
    monkeypatch.setattr(main, "handle_mcp_request", spy)
    good = issue_token(TokenRequest(agent_id="free_ctx", principal_id="p")).token
    _ping(client, **{"x-forwarded-for": "198.51.100.10", "x-agent-identity": good})
    assert seen == {"key": "key:free_ctx", "ip": "198.51.100.10"}
    _ping(client, **{"x-forwarded-for": "198.51.100.11"})
    assert seen == {"key": "ip:198.51.100.11", "ip": "198.51.100.11"}
