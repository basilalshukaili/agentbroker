"""Discovery hygiene (verdict item A8) and the discovery documents after release 1 (OAuth Connect, MCP 2026-07-28).

WHAT WAS MEASURED (production Caddy access logs, 2026-09-30 to 2026-10-04; counts only, no address kept):

  /.well-known/glama.json        268 x 404, from Glama's own checker (empty User-Agent)
  /.well-known/x402.json         101 x 404 (a directory crawler, curl, node)
  /.well-known/mcp/server-card.json  36 x 404 (four scanners and two directories)
  and, since release 1, every discovery document that was written BEFORE OAuth Connect and MCP 2026-07-28 existed
  (llms.txt, /.well-known/mcp.json, the discovery card) said nothing about either.

THE RULES THESE TESTS PIN
  1. A document exists exactly when the thing it describes exists. glama.json is a CLAIM TOKEN that Glama issues to
     the account that owns the listing: without one there is nothing true to serve, so 404; a malformed one is
     never echoed. The x402.json alias is the same document as /.well-known/x402 and is a 404 whenever that is.
  2. Every fact in a document is read from the code that makes it true (the protocol versions from the list
     `server/discover` answers, the tools from the manifest, who needs a key from core/tool_auth.py, the sign-in
     endpoints from the OAuth router). A typed copy is a copy that drifts.
  3. A switch that is off is not advertised: with OAUTH_CONNECT_ENABLED=0 no document mentions a sign-in.
  4. No document names an assistant it has not been walked through (docs/OAUTH-CONNECT.md "Not done here") and none
     carries a price, a credit or a payment claim: those belong to the payments block and its own tests.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import main
from agent_interface.mcp_server import ALL_PROTOCOL_VERSIONS
from agent_interface.oauth import limits
from agent_interface.oauth.store import MemoryStore, set_store
from core import tool_auth

BASE = "https://api.hatchloop.dev"
SITE = "https://hatchloop.dev"
CARD = "/.well-known/mcp/server-card.json"
GLAMA = "/.well-known/glama.json"
TOKEN = "glama_claim_" + "A1b2C3d4" * 4          # glama_claim_ + exactly 32 of [A-Za-z0-9_-]

ASSISTANTS = ("claude", "chatgpt", "openai", "grok", "muse", "gemini", "copilot", "cursor")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OAUTH_CONNECT_ENABLED", raising=False)
    monkeypatch.delenv("OAUTH_CHALLENGE_STYLE", raising=False)
    monkeypatch.delenv("GLAMA_CLAIM_TOKEN", raising=False)
    set_store(MemoryStore())
    limits.LIMITS.reset()
    main._rl_buckets.clear()
    yield
    set_store(None)
    main._rl_buckets.clear()


@pytest.fixture
def client():
    return TestClient(main.app, base_url=BASE, raise_server_exceptions=False)


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def _path(url: str) -> str:
    p = urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


def _all_documents(client) -> dict:
    return {
        "llms.txt": client.get("/llms.txt").text,
        "mcp.json": client.get("/.well-known/mcp.json").text,
        "discovery card": client.get("/.well-known/agent-service").text,
        "server card": client.get(CARD).text,
    }


# ---------------------------------------------------------------------------
# the server card
# ---------------------------------------------------------------------------

def test_the_server_card_is_served_where_scanners_ask_for_it(client):
    r = client.get(CARD)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["access-control-allow-origin"] == "*"
    assert "max-age" in r.headers["cache-control"] and "public" in r.headers["cache-control"]
    card = r.json()
    # SEP-1649 (draft) requires these six. The card says it is a draft shape; nothing here claims the spec is final.
    for key in ("$schema", "version", "protocolVersion", "serverInfo", "transport", "capabilities"):
        assert key in card, key
    assert client.head(CARD).status_code == 200


def test_the_card_speaks_the_versions_server_discover_answers(client):
    discovered = client.post("/mcp", json=rpc("server/discover")).json()["result"]["supportedVersions"]
    card = client.get(CARD).json()
    assert card["supportedProtocolVersions"] == discovered == list(ALL_PROTOCOL_VERSIONS)
    assert card["protocolVersion"] == discovered[0] == "2026-07-28"


def test_the_card_names_the_endpoint_and_the_identity_the_handshake_does(client):
    card = client.get(CARD).json()
    mcp_json = client.get("/.well-known/mcp.json").json()
    assert card["transport"] == {"type": "streamable-http", "endpoint": mcp_json["transport"]["endpoint"]}
    init = client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "1"}}))
    info = init.json()["result"]["serverInfo"]
    assert card["serverInfo"]["name"] == info["name"] and card["serverInfo"]["version"] == info["version"]
    caps = init.json()["result"]["capabilities"]
    for k in ("tools", "resources", "prompts"):
        assert card["capabilities"][k] == caps[k], k


def test_the_card_lists_exactly_the_tools_tools_list_serves_and_who_needs_a_key(client):
    served = [t["name"] for t in client.post("/mcp", json=rpc("tools/list")).json()["result"]["tools"]]
    tools = client.get(CARD).json()["tools"]
    assert [t["name"] for t in tools] == served
    for t in tools:
        assert t["requiresKey"] is tool_auth.requires_key(t["name"]), t["name"]


def test_the_card_carries_the_readiness_tools_list_does_including_what_is_unavailable_here(client, monkeypatch):
    """The card is built through the same two steps tools/list takes, so a tool tools/list marks unavailable on
    this deployment (voice and SMS are not provisioned in production) is never shown as working."""
    from core import tool_readiness
    from agent_interface.manifest_server import get_full_manifest
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "VAPI_API_KEY", "RESEND_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    stored = tool_readiness.all_labelled(get_full_manifest()["operations"])
    assert stored, "the manifest has labelled tools; this test would prove nothing without them"
    listed = client.post("/mcp", json=rpc("tools/list")).json()["result"]["tools"]
    expected = {t["name"]: ((t.get("_meta") or {}).get(tool_readiness.META_KEY) or {}).get("state") for t in listed}
    by_name = {t["name"]: t.get("readiness") for t in client.get(CARD).json()["tools"]}
    assert by_name == expected, "the card and tools/list disagree about which tools are usable"
    for name, state in stored.items():
        assert by_name[name] is not None, f"{name} is labelled {state} in the manifest and must be labelled on the card"


def test_the_card_makes_no_payment_claim_and_names_no_assistant(client):
    text = json.dumps(client.get(CARD).json()).lower()
    for word in ("credit", "price", "usd", "x402", "stripe", "polar", "checkout"):
        assert word not in text, f"the server card must not carry payment wording: {word!r}"
    assert not re.search(r"\$\s?\d", text), "no dollar amount"
    for name in ASSISTANTS:
        assert name not in text, f"the card names an assistant it has not been walked through: {name!r}"


def test_the_card_describes_the_sign_in_from_the_oauth_router_not_from_a_copy(client):
    card = client.get(CARD).json()
    oauth = card["authentication"]["oauth2"]
    prm = client.get(_path(oauth["protected_resource_metadata_url"]))
    asm = client.get(_path(oauth["authorization_server_metadata_url"]))
    assert prm.status_code == 200 and asm.status_code == 200
    assert prm.json()["resource"] == card["transport"]["endpoint"], (
        "the protected-resource document must name the very URL the card tells a client to connect to")
    assert prm.json()["authorization_servers"] == [oauth["authorization_server"]]
    assert asm.json()["issuer"] == oauth["authorization_server"]
    assert asm.json()["code_challenge_methods_supported"] == oauth["code_challenge_methods"]
    assert asm.json()["grant_types_supported"] == oauth["grant_types"]
    assert set(oauth["client_registration"]) == {"client_id_metadata_document", "dynamic_client_registration"}
    assert asm.json()["client_id_metadata_document_supported"] is True and "registration_endpoint" in asm.json()
    assert oauth["tools_that_need_an_account"] == sorted(tool_auth.TOOLS_REQUIRING_KEY)
    assert card["authentication"]["header"] == "X-Agent-Identity"
    assert card["authentication"]["required"] is False, "the keyless tools need no sign-in at all"


# ---------------------------------------------------------------------------
# release 1 reaches the other documents too
# ---------------------------------------------------------------------------

def test_mcp_json_states_the_sign_in_and_the_protocol_versions(client):
    doc = client.get("/.well-known/mcp.json").json()
    oauth = doc["auth"]["oauth2"]
    assert oauth == client.get(CARD).json()["authentication"]["oauth2"], "one derivation, two documents"
    assert doc["auth"]["header"] == "X-Agent-Identity" and doc["auth"]["scheme"] == "bearer", "old fields kept"
    versions = doc["protocol_versions"]
    assert versions["supported"] == list(ALL_PROTOCOL_VERSIONS)
    assert versions["modern"] == ["2026-07-28"] and versions["supported"][: len(versions["modern"])] == versions["modern"]
    assert versions["modern"] + versions["legacy"] == versions["supported"], "two eras, nothing in both or neither"
    assert versions["server_discover"] is True


def test_the_discovery_card_points_an_agent_at_the_sign_in(client):
    auth = client.get("/.well-known/agent-service").json()["auth"]
    assert auth["scheme"] == "AgentIdentity" and auth["header"] == "X-Agent-Identity", "old fields kept"
    assert auth["oauth2"]["protected_resource_metadata_url"].endswith("/.well-known/oauth-protected-resource/mcp")


def test_llms_txt_explains_the_sign_in_and_the_new_handshake(client):
    text = client.get("/llms.txt").text
    assert "/.well-known/oauth-protected-resource/mcp" in text
    assert "/.well-known/oauth-authorization-server" in text
    assert "PKCE" in text and "S256" in text
    assert "server/discover" in text and "2026-07-28" in text
    for v in ALL_PROTOCOL_VERSIONS:
        assert v in text, v
    for t in sorted(tool_auth.TOOLS_REQUIRING_KEY):
        assert f"`{t}`" in text.split("Sign in with OAuth", 1)[1].split("## ", 1)[0], t
    low = text.lower()
    for name in ASSISTANTS:
        assert name not in low.split("sign in with oauth", 1)[1].split("\n## ", 1)[0], name


def test_llms_txt_keeps_the_key_by_email_path_beside_the_sign_in(client):
    text = client.get("/llms.txt").text
    assert "POST https://api.hatchloop.dev/keys/request" in text and "X-Agent-Identity" in text


def _sign_in_section(client) -> str:
    return client.get("/llms.txt").text.split("Sign in with OAuth", 1)[1].split("\n## ", 1)[0]


def test_the_new_sentences_never_equate_free_with_keyless(client):
    """core/tool_auth.py: "Free and keyless are NOT the same set, and conflating them is its own bug". Two tools cost
    nothing and still refuse an anonymous call, so a sentence that says "the free tools need no key" is false of them,
    and the card (which marks both requiresKey) would contradict its own note."""
    costs_nothing_needs_key = sorted(o["name"] for o in tool_auth._ops()
                                     if tool_auth._basis(o) == "free" and tool_auth.requires_key(o["name"]))
    assert costs_nothing_needs_key, "the premise of this test: some tool is free AND needs a key"
    card = client.get(CARD).json()
    for name, text in (("card note", card["authentication"]["note"]), ("llms.txt sign-in", _sign_in_section(client))):
        assert not re.search(r"\bfree tools?\b", text, re.I), f"{name} says 'free tools': {text!r}"
    keyed = {t["name"] for t in card["tools"] if t["requiresKey"]}
    assert set(costs_nothing_needs_key) <= keyed, "the card marks them requiresKey, so its note must be able to say so"
    # and the referent of what the llms.txt paragraph does say is the one set tool_auth publishes
    assert tool_auth.usable_without_key() + tool_auth.needs_key() == tool_auth.total_tools()


def test_the_llms_txt_sign_in_names_the_methods_the_router_advertises_not_a_typed_list(client, monkeypatch):
    """The prose is read from the same metadata the block is, so a router that drops dynamic registration or moves
    PKCE off S256 changes the paragraph with it instead of leaving it true only by coincidence."""
    from agent_interface.oauth import router as oauth_router
    real = oauth_router.authorization_server_metadata
    default = _sign_in_section(client)
    assert "Client ID Metadata Document" in default and "dynamic registration" in default and "`S256`" in default

    def only_plain_no_registration():
        m = dict(real())
        m.pop("registration_endpoint", None)
        m["client_id_metadata_document_supported"] = False
        m["code_challenge_methods_supported"] = ["plain"]
        return m
    monkeypatch.setattr(oauth_router, "authorization_server_metadata", only_plain_no_registration)
    text = _sign_in_section(client)
    assert "S256" not in text and "`plain`" in text
    assert "dynamic registration" not in text and "Client ID Metadata Document" not in text

    def only_dynamic():
        m = dict(real())
        m["client_id_metadata_document_supported"] = False
        return m
    monkeypatch.setattr(oauth_router, "authorization_server_metadata", only_dynamic)
    text = _sign_in_section(client)
    assert "dynamic registration" in text and "Client ID Metadata Document" not in text


def test_the_cards_sign_in_url_is_on_the_host_it_tells_a_client_to_connect_to(client):
    """OAUTH_ISSUER and PUBLIC_BASE_URL are the same host today and the card derives its protected-resource URL from the
    first and its endpoint from the second. Pinned, not assumed: a deployment that separates them would hand a client
    a `resource` that differs from the URL it connected to (RFC 9728 says refuse), so it must be a conscious change
    (docs/DISCOVERY.md, "OAUTH_ISSUER must equal PUBLIC_BASE_URL")."""
    from agent_interface.oauth import settings
    card = client.get(CARD).json()
    assert settings.issuer() == BASE
    assert card["transport"]["endpoint"] == f"{settings.issuer()}/mcp"
    assert card["authentication"]["oauth2"]["protected_resource_metadata_url"].startswith(settings.issuer() + "/")


def test_with_the_sign_in_switched_off_no_document_mentions_it(client, monkeypatch):
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "0")
    for name, text in _all_documents(client).items():
        assert "oauth" not in text.lower(), f"{name} advertises a sign-in that is switched off"
        assert "pkce" not in text.lower(), name
    assert client.get("/.well-known/oauth-protected-resource").status_code == 404, "and the endpoints are gone too"
    card = client.get(CARD).json()
    assert "oauth2" not in card["authentication"] and card["authentication"]["header"] == "X-Agent-Identity"
    assert "oauth2" not in client.get("/.well-known/mcp.json").json()["auth"]
    # the protocol versions are not behind that switch
    assert client.get("/.well-known/mcp.json").json()["protocol_versions"]["supported"] == list(ALL_PROTOCOL_VERSIONS)


def test_the_origin_names_the_host_a_protected_resource_request_arrived_on(client):
    """The property the Caddy route for hatchloop.dev relies on (deploy/caddy/oauth_prm.py): the origin builds
    `resource` from the Host header, so a proxy that replaces Host (Next.js's rewrite does) gets the wrong one."""
    door = "/.well-known/oauth-protected-resource/mcp/sanctions-screening"
    site = client.get(door, headers={"host": "hatchloop.dev"}).json()["resource"]
    api = client.get(door, headers={"host": "api.hatchloop.dev"}).json()["resource"]
    assert site == f"{SITE}/mcp/sanctions-screening" and api == f"{BASE}/mcp/sanctions-screening"
    full = client.get("/.well-known/oauth-protected-resource/mcp/agent-broker", headers={"host": "api.hatchloop.dev"})
    assert full.json()["resource"] == f"{SITE}/mcp/agent-broker", "the site's full-server URL exists only on the site"


# ---------------------------------------------------------------------------
# glama.json: a claim token or nothing
# ---------------------------------------------------------------------------

def test_without_a_claim_token_there_is_no_glama_file(client):
    assert client.get(GLAMA).status_code == 404
    assert client.head(GLAMA).status_code == 404


def test_with_a_claim_token_the_file_is_exactly_the_two_fields_glama_reads(client, monkeypatch):
    monkeypatch.setenv("GLAMA_CLAIM_TOKEN", TOKEN)
    r = client.get(GLAMA)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"$schema": "https://glama.ai/mcp/schemas/connector.json", "claim": TOKEN}
    assert client.head(GLAMA).status_code == 200
    # the same token on the site host (Next.js proxies /.well-known/* to the origin): the file is host-independent
    assert client.get(GLAMA, headers={"host": "hatchloop.dev"}).json()["claim"] == TOKEN


@pytest.mark.parametrize("bad", [
    "glama_claim_short", "glama_claim_" + "A" * 33, "glama_claim_" + "A" * 31, "claim_" + "A" * 32,
    "glama_claim_" + "A" * 31 + "!", "<script>", "", "   ", "none", "glama_claim_" + "A" * 16 + " " + "A" * 16,
])
def test_a_malformed_token_is_never_echoed(client, monkeypatch, bad):
    monkeypatch.setenv("GLAMA_CLAIM_TOKEN", bad)
    r = client.get(GLAMA)
    assert r.status_code == 404
    if bad.strip():
        assert bad.strip() not in r.text


def test_whitespace_around_a_pasted_token_is_trimmed_not_served(client, monkeypatch):
    monkeypatch.setenv("GLAMA_CLAIM_TOKEN", "  " + TOKEN + "\n")
    assert client.get(GLAMA).json()["claim"] == TOKEN


def test_the_token_is_an_optional_variable_a_deploy_does_not_require():
    """scripts/check_deploy_env.py (hatchloop tree) derives the REQUIRED container variables from the code: a getenv
    with no default, or an empty-string default, is required. The container has no token today, and a deploy must
    not start demanding one, so the default must be a non-empty literal (the OAuth settings use "none" the same way)."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "agent_interface" / "discovery_extras.py").read_text("utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "getenv" and n.args
             and isinstance(n.args[0], ast.Constant) and n.args[0].value == "GLAMA_CLAIM_TOKEN"]
    assert len(calls) == 1, "exactly one read of the variable"
    default = calls[0].args[1] if len(calls[0].args) > 1 else None
    assert isinstance(default, ast.Constant) and isinstance(default.value, str) and default.value.strip(), (
        "a missing or empty default makes the variable REQUIRED in every deploy")


# ---------------------------------------------------------------------------
# x402.json: the same document as /.well-known/x402, or nothing
# ---------------------------------------------------------------------------

DOC = {"version": 1, "x402Version": 2, "resources": ["https://api.hatchloop.dev/mcp"], "payTo": "0xabc"}
X402 = "/.well-known/x402"
X402_JSON = "/.well-known/x402.json"


@pytest.fixture
def primary_route():
    """The route feat/x402-advertise-20261003 (and feat/x402-honesty-20261004) add to main.py, registered here exactly
    as they write it: it asks billing.x402_gate.discovery_document() and raises a 404 whose detail names the host.
    This base has no such route, so the tests that need the integrated state register it and take it away again."""
    from fastapi import HTTPException
    from billing import x402_gate

    async def well_known_x402():
        doc = x402_gate.discovery_document()
        if doc is None:
            raise HTTPException(status_code=404, detail="x402 is not enabled on this host")
        return doc

    main.app.add_api_route(X402, well_known_x402, methods=["GET"], include_in_schema=False)
    route = main.app.router.routes[-1]
    yield route
    main.app.router.routes.remove(route)


def _pair(client):
    return client.get(X402), client.get(X402_JSON)


def test_the_alias_serves_the_x402_document_byte_for_byte(client, monkeypatch, primary_route):
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "discovery_document", lambda: dict(DOC), raising=False)
    primary, alias = _pair(client)
    assert primary.status_code == 200 and alias.status_code == 200 and alias.json() == DOC
    assert alias.content == primary.content, "the alias and the document it aliases must never differ"
    assert alias.headers["content-type"] == primary.headers["content-type"]
    assert client.head(X402_JSON).status_code == 200


def test_the_alias_is_the_primarys_own_404_byte_for_byte_when_the_rail_is_off(client, monkeypatch, primary_route):
    """The 404 case is part of the claim ("the same document as /.well-known/x402, or 404 whenever that is"). The
    integrated primary answers {"detail": "x402 is not enabled on this host"}; a different body on the alias is a
    second writer of the same answer, which is exactly what the live verifier would then fail on."""
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "discovery_document", lambda: None, raising=False)
    primary, alias = _pair(client)
    assert primary.status_code == 404 and primary.json() == {"detail": "x402 is not enabled on this host"}, (
        "the fixture must be the sibling branch's route, or this test proves nothing")
    assert alias.status_code == 404
    assert alias.content == primary.content, f"alias {alias.content!r} != primary {primary.content!r}"


def test_the_alias_is_the_same_404_on_a_build_with_no_primary_route(client, monkeypatch):
    """This base: there is no /.well-known/x402 route at all, so the primary answers the framework's own 404."""
    primary, alias = _pair(client)
    assert primary.status_code == 404 and alias.status_code == 404
    assert alias.content == primary.content


def test_the_alias_never_serves_a_document_the_primary_does_not(client, monkeypatch):
    """A discovery_document() function with no route behind it is not a published document: the alias answers
    whatever the primary path answers, so it can never be a 200 where /.well-known/x402 is a 404."""
    from billing import x402_gate
    monkeypatch.setattr(x402_gate, "discovery_document", lambda: dict(DOC), raising=False)
    primary, alias = _pair(client)
    assert primary.status_code == 404 and alias.status_code == 404
    assert alias.content == primary.content


def test_the_alias_answers_what_the_primary_does_whatever_the_state(client, monkeypatch, primary_route):
    """The invariant itself, over every state the document can be in: status and bytes equal."""
    from billing import x402_gate
    for build in (lambda: dict(DOC), lambda: None):
        monkeypatch.setattr(x402_gate, "discovery_document", build, raising=False)
        primary, alias = _pair(client)
        assert (alias.status_code, alias.content) == (primary.status_code, primary.content)


def test_a_document_that_cannot_be_built_is_a_404_not_a_500(client, monkeypatch, primary_route):
    from billing import x402_gate

    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(x402_gate, "discovery_document", boom, raising=False)
    assert client.get(X402_JSON).status_code == 404


def test_a_failing_document_is_logged_by_type_never_by_message(client, monkeypatch, primary_route, caplog):
    """The rule for this module is length only, never the value (the glama warning obeys it): a builder that raises
    with something private in its message must not put it in the log, and no traceback carries it either."""
    import logging
    from billing import x402_gate
    marker = "SYNTHETIC_PRIVATE_MARKER"

    def boom():
        raise RuntimeError(marker)
    monkeypatch.setattr(x402_gate, "discovery_document", boom, raising=False)
    with caplog.at_level(logging.DEBUG, logger="smb_broker.discovery"):
        assert client.get(X402_JSON).status_code == 404
    mine = [r for r in caplog.records if r.name == "smb_broker.discovery"]
    assert mine, "the failure must still be logged: a discovery file that fails silently is its own problem"
    assert any("x402_discovery_document_failed" in r.getMessage() and "RuntimeError" in r.getMessage() for r in mine)
    for r in mine:
        assert marker not in r.getMessage() and marker not in str(r.args)
        assert r.exc_info is None, "no traceback: it prints the exception message"
    assert marker not in caplog.text


def test_payment_manifests_we_do_not_implement_stay_404(client):
    """mpp and payment-manifest are probed (about 100 times in four days). We implement neither, and an answer
    for a protocol we do not speak would be a claim, so they are not aliased to anything."""
    for p in ("/.well-known/mpp", "/.well-known/payment-manifest", "/.well-known/payment-manifest.json",
              "/.well-known/mpp.json"):
        assert client.get(p).status_code == 404, p
