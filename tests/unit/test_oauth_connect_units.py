"""The pieces of the Connect sign-in that can be tested without a browser or a database.

  * discovery documents say what is true and point at endpoints that exist;
  * which resources are ours, and that a token for anyone else's is refused;
  * redirect URIs: what is accepted, what matches, and that loopback is the only relaxation;
  * the client-metadata fetch cannot be aimed at our own network (the SSRF cases are the point);
  * PKCE against the RFC 7636 test vector; the account an email stands for matches the one /keys/verify derives;
  * the audience claim: enforced for the tokens that carry one, absent and ignored for every older key;
  * the email: three honest outcomes, no injected markup, nothing secret in the log;
  * the migration, read as text: every function pinned and closed to everyone but the service.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from agent_interface import identity as ident
from agent_interface.oauth import clients as oc
from agent_interface.oauth import emailer, limits, pages, resources, settings, tokens
from agent_interface.oauth.router import authorization_server_metadata, protected_resource_metadata
from agent_interface.oauth.store import MemoryStore, set_store
from tests.oauth_support import CLAUDE_CALLBACK, CLAUDE_CIMD, claude_document, install_cimd

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean():
    set_store(MemoryStore())
    limits.LIMITS.reset()
    yield
    set_store(None)
    limits.LIMITS.reset()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_the_feature_is_on_by_default_and_every_off_spelling_turns_it_off(monkeypatch):
    monkeypatch.delenv("OAUTH_CONNECT_ENABLED", raising=False)
    assert settings.enabled() is True
    for off in ("0", "false", "FALSE", "no", "off", " Off "):
        monkeypatch.setenv("OAUTH_CONNECT_ENABLED", off)
        assert settings.enabled() is False, off
    for on in ("1", "true", "yes", ""):
        monkeypatch.setenv("OAUTH_CONNECT_ENABLED", on)
        assert settings.enabled() is True, on


def test_the_lifetimes_are_the_ones_the_specification_and_the_clients_ask_for():
    assert settings.ACCESS_TTL_S == 3600                       # SHOULD be short-lived
    assert settings.CODE_TTL_S <= 600                          # RFC 6749: at most ten minutes
    assert settings.REFRESH_TTL_S < settings.REFRESH_FAMILY_TTL_S
    assert settings.SIGNIN_TTL_S == 900


def test_the_challenge_style_defaults_to_auto_and_ignores_garbage(monkeypatch):
    monkeypatch.delenv("OAUTH_CHALLENGE_STYLE", raising=False)
    assert settings.challenge_style() == "auto"
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", "nonsense")
    assert settings.challenge_style() == "auto"
    monkeypatch.setenv("OAUTH_CHALLENGE_STYLE", "HTTP401")
    assert settings.challenge_style() == "http401"


# ---------------------------------------------------------------------------
# discovery documents
# ---------------------------------------------------------------------------

def test_authorization_server_metadata_advertises_exactly_what_claude_and_chatgpt_look_for():
    m = authorization_server_metadata()
    iss = settings.issuer()
    assert m["issuer"] == iss and not iss.endswith("/")
    assert m["code_challenge_methods_supported"] == ["S256"]            # clients refuse to proceed without it
    assert m["client_id_metadata_document_supported"] is True
    assert "none" in m["token_endpoint_auth_methods_supported"]         # Claude's CIMD client is a public client
    assert m["authorization_response_iss_parameter_supported"] is True  # ChatGPT's stable redirect needs it
    assert set(m["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert m["response_types_supported"] == ["code"]
    assert "offline_access" in m["scopes_supported"]                    # Claude appends it to ask for a refresh token
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert m[key].startswith(iss + "/oauth/")
    assert not any(k in m for k in ("userinfo_endpoint", "jwks_uri", "id_token_signing_alg_values_supported"))   # not an OIDC provider


def test_the_protected_resource_names_one_issuer_and_no_refresh_scope():
    doc = protected_resource_metadata("https://api.hatchloop.dev/mcp")
    assert doc["authorization_servers"] == [settings.issuer()]          # Claude uses only the first entry
    assert doc["resource"] == "https://api.hatchloop.dev/mcp"
    assert "offline_access" not in doc["scopes_supported"]              # the spec: a resource SHOULD NOT list it
    assert doc["bearer_methods_supported"] == ["header"]


def test_every_endpoint_the_metadata_advertises_exists():
    c = TestClient(main.app, base_url="https://api.hatchloop.dev", follow_redirects=False)
    m = authorization_server_metadata()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        path = m[key][len(settings.issuer()):]
        assert c.options(path).status_code in (204, 200) or c.get(path).status_code != 404, key


def test_discovery_is_served_for_every_door_and_nothing_else():
    c = TestClient(main.app, base_url="https://api.hatchloop.dev")
    from agent_interface import profiles
    for door in profiles.oauth_profiles():
        assert c.get(f"/.well-known/oauth-protected-resource/mcp/{door}").json()["resource"].endswith(f"/mcp/{door}")
    # the anonymous ChatGPT door has no sign-in, so it publishes no protected-resource document
    assert set(profiles.PROFILES) - set(profiles.oauth_profiles()) == {"chatgpt"}
    assert c.get("/.well-known/oauth-protected-resource/mcp/chatgpt").status_code == 404
    assert c.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == "https://api.hatchloop.dev/mcp"
    for nothing in ("evil", "mcp/not-a-door", "ops/find_business", "mcp/agent-broker/extra", "..%2Fetc"):
        assert c.get(f"/.well-known/oauth-protected-resource/{nothing}").status_code == 404, nothing
    r = c.get("/.well-known/oauth-protected-resource", headers={"Origin": "https://inspector.example"})
    assert r.headers["access-control-allow-origin"] == "*"              # browser-based inspectors read it
    assert "host" in r.headers["vary"].lower() and "max-age=300" in r.headers["cache-control"]


def test_the_host_parameter_is_honoured_only_for_our_own_hosts():
    c = TestClient(main.app, base_url="https://api.hatchloop.dev")
    ok = c.get("/.well-known/oauth-protected-resource/mcp/agent-broker?host=hatchloop.dev").json()
    assert ok["resource"] == "https://hatchloop.dev/mcp/agent-broker"
    hostile = c.get("/.well-known/oauth-protected-resource/mcp?host=evil.example.net").json()
    assert hostile["resource"] == "https://api.hatchloop.dev/mcp"       # never reflected back
    spoof = c.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": "evil.example.net"}).json()
    assert spoof["resource"] == "https://api.hatchloop.dev/mcp"


# ---------------------------------------------------------------------------
# resources and the audience claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("https://api.hatchloop.dev/mcp", "https://api.hatchloop.dev/mcp"),
    ("https://API.HatchLoop.dev/mcp/", "https://api.hatchloop.dev/mcp"),
    ("https://hatchloop.dev/mcp/agent-broker", "https://hatchloop.dev/mcp/agent-broker"),
    ("https://hatchloop.dev/mcp/sanctions-screening", "https://hatchloop.dev/mcp/sanctions-screening"),
    ("https://api.hatchloop.dev", "https://api.hatchloop.dev"),
    ("https://api.hatchloop.dev/mcp?x=1", None),
    ("https://api.hatchloop.dev/mcp#frag", None),
    ("https://api.hatchloop.dev.evil.example/mcp", None),
    ("https://evil.example/mcp", None),
    ("https://user:pw@api.hatchloop.dev/mcp", None),
    ("https://api.hatchloop.dev/other", None),
    ("api.hatchloop.dev/mcp", None),
    ("", None), (None, None), ("x" * 600, None),
])
def test_only_resources_we_serve_are_accepted_and_always_in_canonical_form(value, expected):
    assert resources.canonical_resource(value) == expected


def test_the_metadata_url_the_401_points_at_is_always_on_the_origin_and_names_the_public_url():
    api = resources.metadata_url("api.hatchloop.dev", "/mcp")
    assert api == "https://api.hatchloop.dev/.well-known/oauth-protected-resource/mcp"
    # Caddy rewrites hatchloop.dev/mcp/agent-broker to the origin's /mcp: translate back to the name typed
    site = resources.metadata_url("hatchloop.dev", "/mcp")
    assert site == "https://api.hatchloop.dev/.well-known/oauth-protected-resource/mcp/agent-broker?host=hatchloop.dev"
    door = resources.metadata_url("hatchloop.dev", "/mcp/sanctions-screening")
    assert door == "https://api.hatchloop.dev/.well-known/oauth-protected-resource/mcp/sanctions-screening?host=hatchloop.dev"
    # an unknown Host is never echoed
    assert "evil" not in resources.metadata_url("evil.example.net", "/mcp")


def test_a_token_for_another_audience_is_refused_and_every_older_key_is_untouched():
    old = ident.issue_token(ident.TokenRequest(agent_id="free_old", principal_id="free_old"))
    assert ident.validate_token(old.token).valid                                       # no aud: exactly as before
    good = ident.issue_token(ident.TokenRequest(agent_id="a", principal_id="a", extra_claims={"aud": "https://api.hatchloop.dev/mcp"}))
    assert ident.validate_token(good.token).valid
    for aud in ("https://somebody.else/mcp", "", None, 5, ["https://api.hatchloop.dev/mcp"]):
        bad = ident.issue_token(ident.TokenRequest(agent_id="a", principal_id="a", extra_claims={"aud": aud}))
        v = ident.validate_token(bad.token)
        assert not v.valid and "audience" in v.error, aud


def test_extra_claims_can_never_replace_a_claim_the_issuer_sets():
    t = ident.issue_token(ident.TokenRequest(agent_id="real", principal_id="p", ttl_seconds=60,
                                             extra_claims={"agent_id": "admin", "exp": 9e12, "jti": "x", "iss": "evil"}))
    c = ident._verify(t.token)
    assert c["agent_id"] == "real" and c["exp"] < 9e12 and c["jti"] != "x" and c["iss"] == "smb-broker-v1"


# ---------------------------------------------------------------------------
# redirect URIs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uri,private,ok", [
    ("https://claude.ai/api/mcp/auth_callback", False, True),
    ("http://localhost:3118/callback", False, True),
    ("http://127.0.0.1/callback", False, True),
    ("http://[::1]:8080/cb", False, True),
    ("http://evil.example.net/cb", False, False),
    ("http://localhost.evil.example.net/cb", False, False),
    ("cursor://anysphere.cursor/cb", False, False),
    ("cursor://anysphere.cursor/cb", True, True),
    ("vscode://ms-vscode.mcp/cb", True, True),
    ("javascript:alert(1)", True, False), ("data:text/html,x", True, False), ("file:///etc/passwd", True, False),
    ("https://a.example.org/cb#frag", True, False),
    ("https://u:p@a.example.org/cb", True, False),
    ("https://a.example.org/cb with space", True, False),
    ("https://a.example.org/cb\r\nSet-Cookie: x=1", True, False),
    ("https:///nohost", True, False),
    ("", True, False), (None, True, False), (5, True, False), ("https://a.example.org/" + "x" * 2100, True, False),
])
def test_redirect_uri_syntax(uri, private, ok):
    assert (oc.check_redirect_uri(uri, allow_private_scheme=private) is None) is ok


def test_matching_is_exact_except_for_the_port_of_a_loopback_address():
    reg = ["https://claude.ai/api/mcp/auth_callback", "http://localhost/callback", "http://127.0.0.1/callback"]
    assert oc.redirect_matches(reg, "https://claude.ai/api/mcp/auth_callback")
    assert not oc.redirect_matches(reg, "https://claude.ai/api/mcp/auth_callback/")
    assert not oc.redirect_matches(reg, "https://claude.ai/api/mcp/auth_callback?x=1")
    assert not oc.redirect_matches(reg, "https://claude.ai:444/api/mcp/auth_callback")
    assert oc.redirect_matches(reg, "http://localhost:3118/callback")                 # RFC 8252 section 7.3
    assert oc.redirect_matches(reg, "http://127.0.0.1:55555/callback")
    assert not oc.redirect_matches(reg, "http://localhost:3118/other")                # path still binds
    assert not oc.redirect_matches(reg, "http://[::1]:3118/callback")                 # a host not declared is not matched
    assert not oc.redirect_matches(["https://claude.ai/cb"], "http://localhost:80/cb")
    assert not oc.redirect_matches(["https://a.example/cb"], "https://a.example/cb@evil.example")


# ---------------------------------------------------------------------------
# the metadata-document fetch: the SSRF cases
# ---------------------------------------------------------------------------

def _fetcher(resolver, handler=None, clock=None):
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(
        200, json=claude_document(), headers={"content-type": "application/json"})))
    kw = {"clock": clock} if clock else {}
    return oc.MetadataFetcher(resolver=resolver, transport=transport, **kw)


def _resolves(*addrs):
    async def r(host):
        return list(addrs)
    return r


@pytest.mark.parametrize("url", [
    "http://claude.ai/oauth/c.json",                  # not https
    "https://claude.ai/",                             # no path
    "https://claude.ai",                              # no path
    "https://claude.ai:8443/oauth/c.json",            # not the standard port
    "https://127.0.0.1/oauth/c.json", "https://[::1]/oauth/c.json", "https://10.0.0.5/c.json",
    "https://localhost/c.json", "https://metadata.internal/c.json", "https://printer.local/c.json",
    "https://user:pw@claude.ai/c.json", "https://claude.ai/c.json#frag", "https://singlelabel/c.json",
    "https://xn--bcher-kva.example/c.json".replace("xn--", "ü"),    # non-ASCII host
    "https://claude.ai/c.json\nHost: evil", "ftp://claude.ai/c.json", "https://" + "a" * 600 + ".com/c.json",
])
def test_a_client_id_that_could_aim_the_fetch_at_us_is_refused_before_any_lookup(url):
    looked_up = []

    async def resolver(host):
        looked_up.append(host)
        return ["8.8.8.8"]
    with pytest.raises(oc.ClientError):
        run(_fetcher(resolver).get(url))
    assert looked_up == []


@pytest.mark.parametrize("addrs", [
    ["127.0.0.1"], ["10.1.2.3"], ["192.168.0.10"], ["172.16.5.5"], ["169.254.169.254"], ["100.64.0.1"],
    ["0.0.0.0"], ["::1"], ["fe80::1"], ["fc00::5"], ["224.0.0.1"],
    ["8.8.8.8", "127.0.0.1"],                          # ONE private address among public ones is enough to refuse
    ["8.8.8.8", "169.254.169.254"],
    [], ["not-an-ip"],
])
def test_a_name_that_resolves_anywhere_private_is_never_fetched(addrs):
    hit = []
    fetcher = _fetcher(_resolves(*addrs), lambda req: hit.append(req) or httpx.Response(200, json=claude_document()))
    with pytest.raises(oc.ClientError):
        run(fetcher.get(CLAUDE_CIMD))
    assert hit == []


def test_the_connection_goes_to_the_address_that_was_checked_while_presenting_the_name():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=claude_document(), headers={"content-type": "application/json"})
    doc = run(_fetcher(_resolves("160.79.104.10"), handler).get(CLAUDE_CIMD))
    assert doc["redirect_uris"][0] == CLAUDE_CALLBACK
    assert seen[0].url.host == "160.79.104.10" and seen[0].headers["host"] == "claude.ai"
    assert seen[0].extensions.get("sni_hostname") == "claude.ai"      # TLS is verified against the NAME, not the address
    v6 = _fetcher(_resolves("2607:f8b0:4005::1"), handler)
    run(v6.get(CLAUDE_CIMD))
    assert seen[-1].url.host == "2607:f8b0:4005::1"


def test_a_redirect_is_not_followed():
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(oc.ClientError):
        run(_fetcher(_resolves("160.79.104.10"), handler).get(CLAUDE_CIMD))
    assert len(calls) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={}), httpx.Response(500, text="x"),
    httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"[]", headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"x" * 70000, headers={"content-type": "application/json"}),
], ids=["404", "500", "html", "badjson", "array", "too-large"])
def test_a_bad_answer_is_a_refusal(response):
    with pytest.raises(oc.ClientError):
        run(_fetcher(_resolves("160.79.104.10"), lambda req: response).get(CLAUDE_CIMD))


@pytest.mark.parametrize("override", [
    {"client_id": "https://claude.ai/oauth/other.json"},          # the document must say it is THIS url
    {"redirect_uris": []}, {"redirect_uris": "https://x"}, {"redirect_uris": ["https://a.example.org/cb"] * 11},
    {"redirect_uris": ["http://evil.example.net/cb"]}, {"redirect_uris": ["cursor://x/cb"]},
    {"client_secret": "s3cret"}, {"token_endpoint_auth_method": "client_secret_basic"},
    {"grant_types": ["implicit"]},
])
def test_a_document_that_is_not_a_valid_public_client_is_refused(override):
    doc = claude_document(**override)
    with pytest.raises(oc.ClientError):
        run(_fetcher(_resolves("160.79.104.10"), lambda req: httpx.Response(
            200, json=doc, headers={"content-type": "application/json"})).get(CLAUDE_CIMD))


def test_a_declared_private_key_jwt_method_is_tolerated_as_a_public_client():
    """ChatGPT's document declares private_key_jwt; our token endpoint accepts public clients only, and the
    intersection of what both sides support is `none`."""
    doc = claude_document(token_endpoint_auth_method="private_key_jwt")
    got = run(_fetcher(_resolves("160.79.104.10"), lambda req: httpx.Response(
        200, json=doc, headers={"content-type": "application/json"})).get(CLAUDE_CIMD))
    assert got["redirect_uris"]


def test_documents_are_cached_and_failures_are_remembered_briefly():
    now = [1000.0]
    count = []

    def handler(req):
        count.append(1)
        return httpx.Response(200, json=claude_document(), headers={"content-type": "application/json", "cache-control": "max-age=600"})
    f = _fetcher(_resolves("160.79.104.10"), handler, clock=lambda: now[0])
    run(f.get(CLAUDE_CIMD)); run(f.get(CLAUDE_CIMD))
    assert len(count) == 1
    now[0] += 601
    run(f.get(CLAUDE_CIMD))
    assert len(count) == 2

    bad_count = []
    f2 = _fetcher(_resolves("160.79.104.10"), lambda req: bad_count.append(1) or httpx.Response(500), clock=lambda: now[0])
    for _ in range(3):
        with pytest.raises(oc.ClientError):
            run(f2.get(CLAUDE_CIMD))
    assert len(bad_count) == 1                                          # a failing host is not hammered
    now[0] += 31
    with pytest.raises(oc.ClientError):
        run(f2.get(CLAUDE_CIMD))
    assert len(bad_count) == 2


def test_the_fetch_is_rate_limited_per_caller_and_per_target_host(monkeypatch):
    install_cimd(monkeypatch, {CLAUDE_CIMD: claude_document()})
    c = TestClient(main.app, base_url="https://api.hatchloop.dev", follow_redirects=False)
    params = {"response_type": "code", "client_id": CLAUDE_CIMD, "redirect_uri": CLAUDE_CALLBACK,
              "code_challenge": "A" * 43, "code_challenge_method": "S256"}
    codes = [c.get("/oauth/authorize", params=params).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and set(codes[30:]) == {429}


# ---------------------------------------------------------------------------
# limits, tokens, PKCE
# ---------------------------------------------------------------------------

def test_the_sliding_window_counts_only_what_was_allowed():
    now = [0.0]
    rl = limits.RateLimiter(clock=lambda: now[0])
    assert [rl.allow("k", 3, 10) for _ in range(5)] == [True, True, True, False, False]
    now[0] = 5.0
    assert rl.allow("k", 3, 10) is False            # the refused attempts did not extend the lockout
    now[0] = 10.5
    assert rl.allow("k", 3, 10) is True
    assert rl.allow("other", 3, 10) is True         # keys are independent


def test_pkce_matches_the_rfc_7636_test_vector_and_nothing_weaker():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert tokens.pkce_matches(verifier, challenge)
    assert not tokens.pkce_matches(verifier, "A" * 43)
    assert not tokens.pkce_matches(verifier[:-1], challenge)               # below the 43-character minimum
    assert not tokens.pkce_matches(None, challenge) and not tokens.pkce_matches("", challenge)
    assert not tokens.pkce_matches(challenge, challenge)                   # the challenge is not its own verifier
    assert not tokens.pkce_matches(verifier + "!", challenge)
    assert tokens.valid_challenge(challenge) and not tokens.valid_challenge("short") and not tokens.valid_challenge(None)


def test_an_email_stands_for_the_same_free_account_everywhere_it_is_used():
    # key_requests.verify_free_key and portal.key/generate derive free_<sha256(email)[:16]> from the lower-cased address
    email = "Person.Name@Example.ORG "
    legacy = "free_" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    assert tokens.free_account_id(tokens.email_hash(email)) == legacy
    assert tokens.email_hash("A@b.co") == tokens.email_hash(" a@B.CO ")
    assert tokens.mask_email("Person.Name@Example.ORG") == "p***@example.org"
    assert tokens.mask_email("garbage") == "your email"


def test_secrets_are_long_random_and_never_equal():
    seen = {tokens.new_secret(32) for _ in range(200)}
    assert len(seen) == 200 and all(len(s) >= 43 for s in seen)
    assert tokens.sha256_hex("x") == hashlib.sha256(b"x").hexdigest()


def test_a_paid_account_is_used_when_there_is_one_and_the_free_one_when_the_lookup_fails():
    store = MemoryStore()
    digest = tokens.email_hash("buyer@example.org")
    store.links[digest] = {"account_id": "sub_cus_1", "customer_id": "cus_1", "plan": "business"}
    paid = run(tokens.resolve_subject(store, digest))
    assert (paid.agent_id, paid.principal_id, paid.paid, paid.plan) == ("sub_cus_1", "cus_1", True, "business")
    tok = tokens.mint_access_token(paid, resource="https://api.hatchloop.dev/mcp", scope="agentbroker.tools",
                                   client_id="dcr_x", family_id="fam")
    ident_ = ident.validate_token(tok.token).identity
    assert ident_.scope.budget_cap == ident._PLAN_SCOPES["business"][1]

    class Broken(MemoryStore):
        async def account_for_email(self, d):
            raise RuntimeError("database down")
    free = run(tokens.resolve_subject(Broken(), digest))
    assert free.paid is False and free.agent_id == f"free_{digest[:16]}"

    store.links[digest] = {"account_id": "free_looks_paid", "customer_id": None, "plan": "developer"}
    assert run(tokens.resolve_subject(store, digest)).paid is False        # only sub_ accounts are ever treated as paid


# ---------------------------------------------------------------------------
# the email
# ---------------------------------------------------------------------------

def _mail_client(monkeypatch, status=201, raises=False, captured=None):
    def handler(req):
        if captured is not None:
            captured.append(json.loads(req.content))
        if raises:
            raise httpx.ConnectError("boom")
        return httpx.Response(status, json={"id": "x"})
    monkeypatch.setattr(emailer, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("status,expected", [(200, "sent"), (201, "sent"), (422, "rejected"),
                                              (400, "unavailable"), (401, "unavailable"), (429, "unavailable"), (500, "unavailable")])
def test_the_provider_answer_becomes_one_of_three_honest_outcomes(monkeypatch, status, expected):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    _mail_client(monkeypatch, status)
    assert run(emailer.send_signin_link("a@example.org", "https://api.hatchloop.dev/oauth/verify?t=x", "claude.ai", "claude.ai")) == expected


def test_no_key_and_no_network_are_unavailable_never_sent(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert run(emailer.send_signin_link("a@example.org", "l", "x", "y")) == "unavailable"
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    _mail_client(monkeypatch, raises=True)
    assert run(emailer.send_signin_link("a@example.org", "l", "x", "y")) == "unavailable"


def test_the_request_to_the_provider_is_well_formed_and_the_log_holds_neither_address_nor_link(monkeypatch, caplog):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    cap = []
    _mail_client(monkeypatch, 201, captured=cap)
    link = "https://api.hatchloop.dev/oauth/verify?t=SECRETLINKTOKEN123"
    with caplog.at_level(logging.DEBUG):
        run(emailer.send_signin_link("someone@example.org", link, "Acme", "acme.example.org"))
        _mail_client(monkeypatch, 422)
        run(emailer.send_signin_link("someone@example.org", link, "Acme", "acme.example.org"))
        _mail_client(monkeypatch, 500)
        run(emailer.send_signin_link("someone@example.org", link, "Acme", "acme.example.org"))
    body = cap[0]
    assert body["to"] == ["someone@example.org"] and body["from"] == "HatchLoop <hello@hatchloop.dev>"
    assert link in body["html"] and link in body["text"] and "re_test_key" not in json.dumps(body)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "someone@example.org" not in logged and "SECRETLINKTOKEN123" not in logged and "re_test_key" not in logged


def test_a_hostile_app_name_cannot_inject_markup_into_the_email_or_the_pages():
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    _, html_body, text_body = emailer.compose("https://api.hatchloop.dev/oauth/verify?t=a&b=c", evil, evil)
    assert "<script>" not in html_body and "<img" not in html_body and "&lt;script&gt;" in html_body
    assert "&amp;b=c" in html_body                           # the link's own ampersand is escaped inside the attribute
    client = oc.ClientInfo(client_id="dcr_x", kind="dcr", name=evil, host="", redirect_uris=("https://a.example.org/cb",))
    for page in (pages.start_page(client, "https://a.example.org/cb", "rid", "n0nce"),
                 pages.confirm_page(evil, "https://a.example.org/cb", evil, "t" * 40, "n0nce", client=client),
                 pages.message_page(evil, evil, "n0nce"),
                 pages.wait_page("rid", "poll", evil, "n0nce", notice=evil)):
        assert "<script>alert" not in page and "<img src=x" not in page


def test_the_waiting_pages_script_data_cannot_close_its_own_tag():
    page = pages.wait_page("a</script><script>alert(1)", "p</script>", "j***@x.org", "n0nce")
    cfg = re.search(r'<script id="cfg"[^>]*>(.*?)</script>', page, re.S).group(1)
    assert "</script" not in cfg.lower() and json.loads(cfg)["rid"] == "a</script><script>alert(1)"


# ---------------------------------------------------------------------------
# the migration, read as text (runs everywhere - the database tests need docker)
# ---------------------------------------------------------------------------

def _migration():
    return (ROOT / "migrations" / "spine" / "011_oauth_connect.sql").read_text(encoding="utf-8")


def test_every_function_is_security_definer_pinned_and_closed_to_everyone_but_the_service():
    sql = _migration()
    names = re.findall(r"create or replace function public\.(oauth_\w+)\s*\(", sql)
    assert len(names) == 16 and len(set(names)) == 16
    for name in names:
        body = sql[sql.index(f"public.{name}("):]
        head = body[: body.index("$$")]
        assert "security definer" in head and "set search_path = public" in head, name
        assert re.search(rf"revoke all on function public\.{name}\([^)]*\)\s+from public, anon, authenticated;", sql), name
        assert re.search(rf"grant execute on function public\.{name}\([^)]*\)\s+to anon, service_role;", sql), name


def test_every_table_is_closed_and_no_column_can_hold_a_raw_secret_or_an_address():
    sql = _migration()
    tables = re.findall(r"create table if not exists public\.(oauth_\w+)", sql)
    assert len(tables) == 5
    for t in tables:
        assert re.search(rf"revoke all on public\.{t}\s+from anon, authenticated, public;", sql), t
        assert re.search(rf"alter table public\.{t}\s+enable row level security;", sql), t
    columns = set(re.findall(r"^\s{4}(\w+)\s+(?:text|jsonb|integer|timestamptz)", sql, re.M))
    for forbidden in ("email", "token", "code", "secret", "password", "refresh_token", "access_token", "key"):
        assert forbidden not in columns, forbidden
    for digest_col in ("poll_hash", "magic_hash", "code_hash", "token_hash", "email_hash"):
        assert digest_col in columns


def test_the_migration_only_adds():
    sql = _migration().lower()
    for destructive in ("drop table", "drop function", "drop column", "truncate", "alter table public.usage_events",
                        "alter table public.credit_accounts", "alter table public.pending_keys", "drop index"):
        assert destructive not in sql, destructive


def test_no_page_uses_an_inline_style_or_event_handler_that_its_own_csp_would_block():
    client = oc.ClientInfo(client_id="dcr_x", kind="dcr", name="App", host="", redirect_uris=("https://a.example.org/cb",))
    rendered = [
        pages.start_page(client, "https://a.example.org/cb", "rid", "N", error="e", poll_secret="p"),
        pages.wait_page("rid", "poll", "j***@x.org", "N", notice="n", match_code="4821"),
        pages.confirm_page("App", "https://a.example.org/cb", "j***@x.org", "t" * 40, "N", client=client, ask_code=True, error="e"),
        pages.confirm_page("App", "https://a.example.org/cb", "j***@x.org", "t" * 40, "N", client=client),
        pages.message_page("T", "m", "N"),
    ]
    for html_text in rendered:
        assert 'style="' not in html_text and not re.search(r"\son[a-z]+\s*=", html_text), "CSP would block this"
        assert html_text.count("<script") <= 2 and "javascript:" not in html_text


def test_the_waiting_page_shows_the_match_code_and_the_confirm_page_asks_for_it_only_when_told_to():
    wait = pages.wait_page("rid", "poll", "j***@x.org", "N", match_code="4821")
    assert 'id="match-code">4821<' in wait and "Never give it to anyone" in wait
    assert "match-code" not in pages.wait_page("rid", "poll", "j***@x.org", "N")
    client = oc.ClientInfo(client_id="dcr_x", kind="dcr", name="App", host="", redirect_uris=("https://a.example.org/cb",))
    asking = pages.confirm_page("App", "https://a.example.org/cb", "j***@x.org", "t" * 40, "N", client=client, ask_code=True)
    assert 'name="code"' in asking and "formnovalidate" in asking              # Cancel must work with the code box empty
    assert 'name="code"' not in pages.confirm_page("App", "https://a.example.org/cb", "j***@x.org", "t" * 40, "N", client=client)


def test_a_header_url_is_built_only_from_known_paths():
    assert resources.metadata_url("api.hatchloop.dev", '/mcp/x"; evil="1') == \
        "https://api.hatchloop.dev/.well-known/oauth-protected-resource/mcp"
    assert resources.metadata_url("api.hatchloop.dev", "/mcp/../../etc") == \
        "https://api.hatchloop.dev/.well-known/oauth-protected-resource/mcp"


def test_the_package_reads_only_variables_a_deploy_already_provides_or_that_default_to_something():
    """scripts/check_deploy_env.py derives the variables a production deploy REQUIRES from the code and treats a
    getenv with no literal default as required. This feature must never make the box demand a new variable, so
    every NEW name carries a non-empty literal default and every other name is one the service already reads."""
    import ast
    existing = {"PUBLIC_BASE_URL", "KEY_VERIFY_SECRET", "JWT_SIGNING_SECRET", "RESEND_API_KEY"}
    new_with_default = {"OAUTH_CONNECT_ENABLED", "OAUTH_CHALLENGE_STYLE", "OAUTH_CHALLENGE_401_CLIENTS", "OAUTH_ISSUER",
                        "OAUTH_SITE_HOST"}
    seen = set()
    for path in (ROOT / "agent_interface" / "oauth").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "getenv"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                name = node.args[0].value
                seen.add(name)
                assert name in existing | new_with_default, f"{path.name} reads a new variable {name}"
                if name in new_with_default:
                    default = node.args[1] if len(node.args) > 1 else None
                    assert default is not None and (not isinstance(default, ast.Constant) or default.value), \
                        f"{path.name}: {name} needs a non-empty default"
    assert new_with_default <= seen
