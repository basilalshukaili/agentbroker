"""Release 2 (2026-10-04): what the integration gate found when seven item branches met, pinned.

Each branch was right alone; these are the places they disagreed once merged.

1. QUOTA PROMISE IN DISCOVERY. discovery-hygiene wrote "(the premium data tools within a daily quota)" into the
   /llms.txt sign-in paragraph and the /.well-known/mcp/server-card.json note as fixed text. x402-honesty's rule is
   that a quota is promised only while DATA_METERING_ENABLED enforces one, and production runs with it off - so the
   merged build said one thing in mcp.json (premium_data_quota_enforced: false) and the opposite in llms.txt.

2. THE CHATGPT DOOR AND THE TYPE GUARD. drr-defects checks every argument's JSON type against the inputSchema before
   anything runs (defect D1: `screen_sanctions {"name": 12345}` came back as -32603 "Internal error: 'int' object has
   no attribute 'strip'"). chatgpt-free-door leaves the dispatcher before that guard, so D1 survived on /mcp/chatgpt
   alone. And the guard's and the envelope's refusals end "nothing was run or charged" - "charged" is a word that
   door must never say (agent_interface/no_commerce.py FORBIDDEN_RE).

3. "PII STORED AS A SHA-256 HASH ONLY". Only the compliance audit log hashes recipient identifiers; leads, opt-outs,
   conversations, WhatsApp replies and the supply directory hold them readable. The checkout page (served at
   /checkout) and the home card said otherwise. The privacy policy itself is the legal-pages item's (not here).
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_interface import mcp_server, no_commerce  # noqa: E402
from agent_interface.mcp_server import handle_mcp_request  # noqa: E402

DOOR = "chatgpt"
OLD_DOOR = "sanctions-screening"


def rpc(method, params=None, profile=DOOR, rid=1, raw_params=False):
    payload = {"jsonrpc": "2.0", "id": rid, "method": method}
    payload["params"] = params if raw_params else (params or {})
    return asyncio.run(handle_mcp_request(payload, headers={}, profile=profile))


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def commerce_words(obj) -> list:
    return [m.group(0) for s in strings(obj) for m in no_commerce.FORBIDDEN_RE.finditer(s)]


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    for var in ("DATA_METERING_ENABLED", "CREDITS_ENABLED", "X402_ENABLED", "CHATGPT_DOOR_DAILY_CEILING"):
        monkeypatch.delenv(var, raising=False)
    reset = getattr(no_commerce, "reset_ceiling_for_tests", None)
    if reset:
        reset()


@pytest.fixture
def no_dispatch(monkeypatch):
    """Nothing in these tests may reach a tool: a refusal is decided before the engine is asked."""
    calls = []

    async def fake(name, args, headers=None, skip_auth=False):
        calls.append((name, args))
        raise AssertionError(f"dispatched {name} {args!r}: a wrong-typed call must be refused before it runs")
    monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
    return calls


# ---------------------------------------------------------------------------
# 1. The quota caveat in discovery follows the switch
# ---------------------------------------------------------------------------

def _discovery_texts():
    from agent_interface import discovery_auth, well_known
    note = well_known.get_server_card()["authentication"]["note"]
    sign_in = " ".join(discovery_auth.llms_txt_sign_in_lines("https://api.hatchloop.dev"))
    return note, sign_in


def test_with_metering_off_neither_discovery_text_promises_a_quota(monkeypatch):
    monkeypatch.setenv("DATA_METERING_ENABLED", "false")
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "1")
    note, sign_in = _discovery_texts()
    assert sign_in, "the sign-in paragraph is switched on here, so this test reads it"
    for name, text in (("server-card note", note), ("llms.txt sign-in", sign_in)):
        assert "quota" not in text.lower(), (name, text)
        assert "unmetered" in text, (name, text)


def test_with_metering_on_both_discovery_texts_name_the_quota(monkeypatch):
    """The control: the same two sentences DO carry the quota when one is enforced."""
    monkeypatch.setenv("DATA_METERING_ENABLED", "true")
    monkeypatch.setenv("OAUTH_CONNECT_ENABLED", "1")
    note, sign_in = _discovery_texts()
    for name, text in (("server-card note", note), ("llms.txt sign-in", sign_in)):
        assert "free within a daily quota" in text, (name, text)


# ---------------------------------------------------------------------------
# 2. The ChatGPT door refuses a wrong type the way every other door does, in its own words
# ---------------------------------------------------------------------------

WRONG_TYPES = [
    ("screen_sanctions", {"name": 12345}, "name"),
    ("screen_sanctions", {"name": "Alpha Trading", "country": 7}, "country"),
    ("verify_company_record", {"name": ["Apple"]}, "name"),
    ("verify_company_record", {"name": "Apple Inc", "lei": 5493}, "lei"),
    ("map_trade_restriction", {"product": 42, "destination_country": "IR"}, "product"),
    ("map_trade_restriction", {"product": "steel pipes", "destination_country": "IR", "parties": "Acme"},
     "parties"),
]


@pytest.mark.parametrize("tool, arguments, field", WRONG_TYPES, ids=[f"{t}-{f}" for t, _, f in WRONG_TYPES])
def test_the_door_refuses_a_wrong_type_with_a_guided_error_and_runs_nothing(no_dispatch, tool, arguments, field):
    resp = rpc("tools/call", {"name": tool, "arguments": arguments})
    err = resp.get("error")
    assert err, resp
    assert err["code"] == -32602, err
    assert "Internal error" not in err["message"] and "attribute" not in err["message"], err["message"]
    assert err["data"]["error_code"] == "invalid_argument"
    assert any(f.startswith(field) for f in err["data"]["invalid_fields"]), err["data"]
    assert err["data"]["expected_types"].get(field), err["data"]
    assert commerce_words(resp) == [], commerce_words(resp)
    assert no_dispatch == []


def test_the_other_doors_still_say_the_refusal_cost_nothing(no_dispatch):
    """The control: on a door that sells, the same refusal DOES say charged/cost, so the door test above could fail."""
    resp = rpc("tools/call", {"name": "screen_sanctions", "arguments": {"name": 12345}}, profile=OLD_DOOR)
    assert resp["error"]["code"] == -32602
    assert "charged" in resp["error"]["message"]
    assert commerce_words(resp)


@pytest.mark.parametrize("label, method, params", [
    ("params not an object", "tools/call", 5),
    ("params a list", "tools/call", ["screen_sanctions"]),
    ("method not a string", ["tools/call"], {}),
    ("tool name not a string", "tools/call", {"name": ["screen_sanctions"], "arguments": {}}),
    ("arguments not an object", "tools/call", {"name": "screen_sanctions", "arguments": [1]}),
])
def test_every_envelope_refusal_on_the_door_is_clean(no_dispatch, label, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = asyncio.run(handle_mcp_request(payload, headers={}, profile=DOOR))
    err = resp.get("error")
    assert err and err["code"] in (-32600, -32602), (label, resp)
    assert "Internal error" not in err["message"], (label, err["message"])
    assert commerce_words(resp) == [], (label, commerce_words(resp), err["message"])
    assert no_dispatch == []


def test_the_envelope_refusal_still_says_charged_off_the_door():
    """The control for the envelope: the full server keeps its own sentence."""
    resp = asyncio.run(handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": 5},
                                          headers={}, profile=None))
    assert "Nothing was run or charged." in resp["error"]["message"]


def test_a_correct_call_on_the_door_still_runs_with_optional_nulls_removed(monkeypatch):
    """The guard hands the door the cleaned arguments (an explicit null on an optional argument is 'not given')."""
    seen = []

    async def fake(name, args, headers=None, skip_auth=False):
        seen.append((name, args))
        return {"status": "success", "result": {"matched": False}, "human_message": "No match."}
    monkeypatch.setattr(mcp_server, "_dispatch_and_label", fake)
    resp = rpc("tools/call", {"name": "screen_sanctions", "arguments": {"name": "Alpha Trading", "country": None}})
    assert "error" not in resp, resp
    assert seen == [("screen_sanctions", {"name": "Alpha Trading"})]


# ---------------------------------------------------------------------------
# 3. No served page says every identifier is stored only as a hash
# ---------------------------------------------------------------------------

def test_checkout_and_home_do_not_claim_every_identifier_is_hashed():
    from web import pages
    for name, html in (("checkout", pages.render_checkout(None)), ("home", pages.render_home())):
        assert "SHA-256 hash only" not in html, name
        assert "PII stored as" not in html and "PII (phone, email) is stored as" not in html, name
        # what IS true stays said: the audit log hashes recipient identifiers
        assert "audit log" in html and "SHA-256" in html, name


def test_the_compliance_doc_scopes_the_hash_to_the_audit_log():
    with open(os.path.join(ROOT, "docs", "compliance.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "stored as SHA-256 hash only — never in plaintext\n" not in text.replace("\r\n", "\n")
    assert "In this log, the recipient" in text
