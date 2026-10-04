"""The second independent review of the DRR defects fix (2026-10-04): what was still not true.

Every test here failed on the branch tip before the fix.

  P2  A UNIVERSAL THAT IS NOT TRUE. The public text said "marketing without recorded consent is rejected at
      runtime" with no exception. The gate has one on purpose: marketing EMAIL to a US recipient is an
      opt-out regime (CAN-SPAM) and is permitted with an empty consent store. Every surface that makes the
      claim now says so, and one test ties the sentence to the gate over every channel and country.
  P2  message_type IS NOT NORMALISED ON THE FREE PREVIEWS. "Marketing", "MARKETING", " marketing" and any
      unknown type ("promotional", the HTTP field's own documented "opt-in-confirm") skipped the marketing
      consent branch and came back `legal: true` for an SMS with no consent on file, while the real send path
      refuses all of them. The previews now refuse an unknown type and read a known one in any case, and the
      gate itself no longer treats an unrecognised type as "not marketing".
  P3  The X-Idempotency-Key HEADER was still cut to 128 characters, so two different long keys shared a claim.
  P3  A body whose `arguments` is not an object took the idempotency claim before being refused.
  P3  With COMPLIANCE_DEFAULT_JURISDICTION=US an unresolved +7 recipient (known not to be American) was judged
      by US rules while the same answer reported could_be_us false.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("JWT_SIGNING_SECRET", "test-secret-long-enough-for-the-check")

from agent_interface import mcp_server as ms  # noqa: E402
from compliance import consent_store as cs_module  # noqa: E402
from compliance.consent_store import ConsentStore  # noqa: E402
from compliance.pre_check import pre_check  # noqa: E402
from core.check_compliance import handle_check_compliance  # noqa: E402
from core.models import ComplianceViolationError, MessageType  # noqa: E402

NOON_MUSCAT = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)

ERR_INVALID_PARAMS = -32602
HDRS = {"x-agent-identity": "test-bearer-token-abc"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def consent():
    original = cs_module._store
    cs_module._store = ConsentStore()
    try:
        yield cs_module._store
    finally:
        cs_module._store = original


def freeze_clock(monkeypatch, when: datetime):
    import compliance.quiet_hours as qh
    original = qh.check
    monkeypatch.setattr(qh, "check", lambda mt, cc=None, sc=None, now_utc=None, recipient_id=None, channel=None:
                        original(mt, cc, sc, when, recipient_id=recipient_id, channel=channel))


def gate(recipient, channel, message_type="marketing", content="20% off this week only!", **kw):
    pre_check(recipient_id=recipient, channel=channel, message_type=message_type, content=content,
              preview=True, **kw)


def check(recipient_id, content="20% off this week only!", **kw):
    kw.setdefault("channel", "sms")
    kw.setdefault("message_type", "marketing")
    return run(handle_check_compliance(recipient_id=recipient_id, content=content, **kw))


# ---------------------------------------------------------------------------
# P2: the consent claim is true, and says what the gate really does
# ---------------------------------------------------------------------------

CONSENT_RULES = {"TCPA_marketing_consent", "sms_marketing_consent", "GDPR_marketing_consent",
                 "CASL_marketing_consent", "voice_marketing_consent", "email_marketing_consent",
                 "whatsapp_marketing_consent", "marketing_consent"}
CHANNELS = ("sms", "email", "voice", "whatsapp")


def _countries():
    from compliance.jurisdiction_rules import list_supported_jurisdictions
    return sorted(set(list_supported_jurisdictions()) | {"OM", "AE", "SA", "QA", "KW", "BH", "TH", "ZA", "EG", "TR"})


def _recipient(channel):
    # Not an E.164 number, so the country is the one the caller states: the matrix varies the country on its own.
    return "buyer@example.com" if channel == "email" else "buyer-0001"


def test_the_gate_demands_a_recorded_opt_in_for_marketing_everywhere_except_us_email(consent):
    """The fact the published sentence rests on, measured over every channel and every modeled country."""
    permitted, wrong_reason = [], []
    for country in _countries():
        for channel in CHANNELS:
            try:
                gate(_recipient(channel), channel, country_code=country)
                permitted.append((channel, country))
            except ComplianceViolationError as exc:
                if exc.rule not in CONSENT_RULES:
                    wrong_reason.append((channel, country, exc.rule))
    assert not wrong_reason, f"refused for a reason other than missing consent: {wrong_reason}"
    assert permitted == [("email", "US")], permitted


def test_us_marketing_email_with_no_opt_in_is_permitted_and_a_us_state_does_not_change_that(consent):
    gate("buyer@example.com", "email", country_code="US")
    gate("buyer@example.com", "email", country_code="US", state_code="CA")


EXEMPTION = "US marketing email follows CAN-SPAM opt-out rules"


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _is_the_universal_claim(passage: str) -> bool:
    """A passage that says the gate rejects marketing for want of consent / an opt-in."""
    return bool(re.search(r"(?i)\bmarketing\b", passage)
                and re.search(r"(?i)\bconsent\b|opt-in|consent_record_id", passage)
                and re.search(r"(?i)\breject", passage))


def _passages(text: str, html: bool = False):
    """Paragraphs of a text. In HTML source a sentence runs over several lines, so only a closing tag ends one."""
    splitter = r"</p>|</li>|</div>|</header>|</section>" if html else r"\n+|</p>|</li>|</div>|</header>|</section>"
    for p in re.split(splitter, text):
        yield re.sub(r"\s+", " ", p)


def _surface_texts():
    """(label, passage) for every published text that can say what the gate rejects."""
    def load(rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return json.load(fh)

    for label, doc in (("manifest.json", load("manifest/manifest.json")),
                       ("mcp_tools.json", load("manifest/mcp_tools.json"))):
        for s in _strings(doc):
            for p in _passages(s):
                yield label, p
    for tool in ms._build_tool_list():
        for s in _strings(tool):
            yield "tools/list", s
    resp = run(ms.handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, {}, None))
    for res in resp["result"]["resources"]:
        body = run(ms.handle_mcp_request(
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": res["uri"]}}, {}, None))
        for s in _strings(body):
            for p in _passages(s):
                yield "cookbook " + res["uri"], p
    with open(os.path.join(ROOT, "web", "pages.py"), encoding="utf-8") as fh:
        for p in _passages(fh.read(), html=True):
            yield "web/pages.py", p


def test_every_passage_that_says_the_gate_rejects_marketing_without_consent_names_the_us_email_exemption():
    offenders = [f"{label}: {p[:240]}" for label, p in _surface_texts()
                 if _is_the_universal_claim(p) and not re.search(r"(?i)CAN-SPAM", p)]
    assert not offenders, "a universal the gate does not honour:\n" + "\n".join(offenders)


def test_the_claim_scan_is_not_vacuous():
    claims = [(label, p) for label, p in _surface_texts() if _is_the_universal_claim(p)]
    labels = {label for label, _ in claims}
    for expected in ("manifest.json", "mcp_tools.json", "tools/list", "web/pages.py"):
        assert expected in labels, (expected, sorted(labels))
    assert any(label.startswith("cookbook") for label in labels), sorted(labels)


def _manifest_send_message():
    with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
        return next(o for o in json.load(fh)["operations"] if o["name"] == "send_message")


def test_the_send_message_description_carries_the_exact_exemption_sentence_and_still_fits():
    desc = _manifest_send_message()["description"]
    assert EXEMPTION in desc
    assert len(desc) <= ms._MAX_DESC_CHARS, f"{len(desc)} chars would be cut by tools/list"
    tool = next(t for t in ms._build_tool_list() if t["name"] == "send_message")
    assert EXEMPTION in tool["description"] and "…" not in tool["description"]


def test_the_message_type_argument_names_the_exemption_inside_the_part_tools_list_keeps():
    """tools/list cuts argument descriptions at _MAX_PROP_DESC_CHARS: the exemption must be inside what survives."""
    tool = next(t for t in ms._build_tool_list() if t["name"] == "send_message")
    shown = tool["inputSchema"]["properties"]["message_type"]["description"]
    assert "CAN-SPAM" in shown and "US" in shown, shown


def test_the_pages_that_promise_marketing_needs_opt_in_regardless_of_payment_name_the_exemption():
    """Not every page says 'rejects': the pricing hero promises 'regardless of how you pay' in other words."""
    with open(os.path.join(ROOT, "web", "pages.py"), encoding="utf-8") as fh:
        passages = [p for p in _passages(fh.read(), html=True) if re.search(r"(?i)regardless of how you pa", p)]
    assert len(passages) >= 2, "the scan must find the pricing hero and the rights list"
    for p in passages:
        assert "CAN-SPAM" in p, p[:240]


def test_the_error_reference_documents_the_new_refusal_the_exemption_and_the_header_rule():
    with open(os.path.join(ROOT, "api", "errors.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "invalid_message_type" in text and "422" in text
    assert "CAN-SPAM" in text
    assert "X-Idempotency-Key" in text and "before an idempotency key is" in text.replace("\r\n", " ").replace("\n", " ")


def test_the_when_to_use_when_not_to_use_and_constraints_all_name_it():
    op = _manifest_send_message()
    for field in ("when_to_use", "when_not_to_use"):
        assert "CAN-SPAM" in op[field], field
    consent_lines = [c for c in op["compliance_constraints"] if c.lower().startswith("marketing")]
    assert consent_lines and all("CAN-SPAM" in c for c in consent_lines), consent_lines


def test_the_generated_catalogue_matches_the_manifest_for_this_sentence():
    with open(os.path.join(ROOT, "manifest", "mcp_tools.json"), encoding="utf-8") as fh:
        tools = {t["name"]: t for t in json.load(fh)}
    assert EXEMPTION in tools["send_message"]["description"]


# ---------------------------------------------------------------------------
# P2: message_type is read the same way on every surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["Marketing", "MARKETING", " marketing", "marketing ", "\tMarketing\n", "mArKeTiNg"])
def test_marketing_in_any_case_or_with_spaces_is_refused_by_the_preview_tool_without_consent(consent, variant):
    r = check("+96891234567", message_type=variant, country_code="OM")
    assert r.reason_code == "not_compliant", (variant, r.reason_code, r.human_message)
    assert r.result["rule"] == "sms_marketing_consent"
    assert r.result["message_type"] == "marketing", "the answer reports the type it judged"


@pytest.mark.parametrize("variant", ["Marketing", "MARKETING", " marketing"])
def test_marketing_in_any_case_is_refused_by_the_public_http_preview_without_consent(consent, variant):
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app, raise_server_exceptions=False).post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": variant,
        "content": "Big sale today!", "country_code": "OM"})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["legal"] is False and body["rule"] == "sms_marketing_consent", body


@pytest.mark.parametrize("variant", ["Marketing", "MARKETING", " marketing"])
def test_the_gate_itself_reads_marketing_in_any_case(consent, variant):
    with pytest.raises(ComplianceViolationError) as ei:
        gate("+96891234567", "sms", message_type=variant, country_code="OM")
    assert ei.value.rule == "sms_marketing_consent"


UNKNOWN_TYPES = ["promotional", "promo", "opt-in-confirm", "customer-service", "marketing_blast", "market ing",
                 "marketing;transactional", "x" * 500, "", "   "]


@pytest.mark.parametrize("value", UNKNOWN_TYPES, ids=[f"u{i}" for i in range(len(UNKNOWN_TYPES))])
def test_an_unknown_message_type_is_a_guided_bad_input_from_the_preview_tool_not_a_pass(consent, value):
    r = check("+96891234567", message_type=value, country_code="OM")
    assert r.reason_code == "bad_input", (value[:30], r.reason_code)
    assert r.status.value == "failure" and r.retriable is False
    assert r.cost.amount == 0.0
    for allowed in ("transactional", "marketing", "reminder", "follow_up", "notification"):
        assert allowed in r.human_message, r.human_message
    assert "x" * 100 not in r.human_message, "the caller's value is not echoed without bound"


@pytest.mark.parametrize("value", ["promotional", "opt-in-confirm", "customer-service", "x" * 500, ""])
def test_an_unknown_message_type_is_a_422_from_the_public_http_preview(consent, value):
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app, raise_server_exceptions=False).post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": value,
        "content": "Big sale today!", "country_code": "OM"})
    assert r.status_code == 422, (value[:20], r.status_code, r.text[:200])
    assert "legal" not in r.json(), "an unknown type must not come back as a legal / not-legal verdict"


@pytest.mark.parametrize("value", ["promotional", "opt-in-confirm", "x" * 500, ""])
def test_the_gate_itself_refuses_a_message_type_it_does_not_recognise(consent, value):
    """Defence in depth: a caller that skips the preview layers must not read 'unknown' as 'not marketing'."""
    with pytest.raises(ComplianceViolationError) as ei:
        gate("+96891234567", "sms", message_type=value, country_code="OM")
    assert ei.value.rule == "invalid_message_type"
    assert "x" * 100 not in ei.value.message


def test_the_invalid_type_rule_has_remediation_text():
    from compliance.remediation import remediation_for
    text = remediation_for("invalid_message_type")
    assert "transactional" in text and "marketing" in text


def test_the_accepted_spellings_follow_the_enum_so_a_new_type_needs_one_edit():
    from compliance.message_type import VALID_MESSAGE_TYPES, canonical_message_type
    assert set(VALID_MESSAGE_TYPES) == {m.value for m in MessageType}
    for m in MessageType:
        assert canonical_message_type(m) == m.value
        assert canonical_message_type(m.value.upper()) == m.value
        assert canonical_message_type(f"  {m.value}\n") == m.value
    for bad in ("", "  ", None, 5, ["marketing"], "marketing blast", "market"):
        assert canonical_message_type(bad) is None, bad


def test_a_known_non_marketing_type_in_any_case_is_still_compliant_and_is_reported_normalised(consent):
    r = check("+96891234567", message_type="Transactional", country_code="OM",
              content="Your appointment is confirmed for 10:30.")
    assert r.reason_code == "compliant", r.human_message
    assert r.result["message_type"] == "transactional"


def test_a_message_type_of_none_means_the_default_and_the_enum_is_accepted(consent):
    r = check("+96891234567", message_type=None, country_code="OM", content="Your appointment is confirmed.")
    assert r.reason_code == "compliant"
    gate("+96891234567", "sms", message_type=MessageType.TRANSACTIONAL, country_code="OM",
         content="Your appointment is confirmed.")


def test_the_http_field_describes_the_five_real_types():
    import main
    description = main._ComplianceCheckRequest.model_fields["message_type"].description
    for real in ("transactional", "marketing", "reminder", "follow_up", "notification"):
        assert real in description
    for invented in ("opt-in-confirm", "customer-service"):
        assert invented not in description


def test_the_check_compliance_tool_text_lists_the_accepted_types_and_says_the_rest_are_refused():
    with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
        op = next(o for o in json.load(fh)["operations"] if o["name"] == "check_compliance")
    text = op["input_schema"]["properties"]["message_type"]["description"]
    assert "any other value is refused" in text.lower() or "any other value is rejected" in text.lower(), text


def test_the_marketing_consent_gate_reads_the_type_the_tool_judged_end_to_end_through_mcp(consent):
    resp = run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "check_compliance",
                    "arguments": {"recipient_id": "+96891234567", "content": "Big sale today!", "channel": "sms",
                                  "message_type": "MARKETING", "country_code": "OM"}}}, {}, None))
    text = json.dumps(resp)
    assert "sms_marketing_consent" in text and '"legal\\": true' not in text and '"legal": true' not in text, text[:400]


# ---------------------------------------------------------------------------
# P3: the idempotency key header is held to the same rule as the argument
# ---------------------------------------------------------------------------

@pytest.fixture
def idem(monkeypatch):
    """A hermetic idempotency store, a spy on the claim, and a stub for everything after the guard."""
    import storage.supabase_client as sb
    from agent_interface import idempotency_gate as ig
    from storage.idempotency_store import get_idempotency_store

    async def _no_rows(*a, **k):
        return []

    async def _no_insert(*a, **k):
        return None

    monkeypatch.setattr(sb, "select_rows", _no_rows)
    monkeypatch.setattr(sb, "insert_row", _no_insert)
    get_idempotency_store()._store.clear()

    claims, impl_calls = [], []
    real_claim = ig.claim

    async def spy_claim(scope, tool, key):
        claims.append((tool, key))
        return await real_claim(scope, tool, key)

    async def impl(params, headers=None):
        impl_calls.append(dict(params.get("arguments") or {}))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(ig, "claim", spy_claim)
    monkeypatch.setattr(ms, "_h_tools_call_impl", impl)
    yield claims, impl_calls
    get_idempotency_store()._store.clear()


def rpc_call(name, arguments, headers=None, profile=None):
    return run(ms.handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers or {}, profile))


def _send_args(**extra):
    return {"recipient": {"id_value": "+15551230000"}, "message_type": "transactional",
            "content": {"body": "hi"}, **extra}


@pytest.mark.parametrize("value", ["k" * 129, "k" * 500])
def test_an_idempotency_key_header_over_the_maximum_is_refused_not_cut_to_128(idem, value):
    claims, impl_calls = idem
    resp = rpc_call("send_message", _send_args(), headers={**HDRS, "x-idempotency-key": value})
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "X-Idempotency-Key" in err["message"] and "128" in err["message"]
    assert err["data"]["error_code"] == "invalid_argument" and err["data"]["retriable"] is False
    assert value not in err["message"]
    assert not claims and not impl_calls


def test_two_different_long_header_keys_that_share_a_128_character_prefix_never_become_one_key(idem):
    claims, impl_calls = idem
    prefix = "p" * 128
    for suffix in ("A", "B"):
        resp = rpc_call("send_message", _send_args(), headers={**HDRS, "x-idempotency-key": prefix + suffix})
        assert (resp.get("error") or {}).get("code") == ERR_INVALID_PARAMS
    assert claims == [] and impl_calls == []


def test_a_blank_idempotency_key_header_is_refused_like_a_blank_argument(idem):
    claims, impl_calls = idem
    resp = rpc_call("send_message", _send_args(), headers={**HDRS, "x-idempotency-key": "   "})
    assert (resp.get("error") or {}).get("code") == ERR_INVALID_PARAMS, resp
    assert not claims and not impl_calls


@pytest.mark.parametrize("value", ["k", "a-retry-key-0001", "k" * 128])
def test_a_proper_idempotency_key_header_is_claimed_as_given(idem, value):
    claims, impl_calls = idem
    resp = rpc_call("send_message", _send_args(), headers={**HDRS, "x-idempotency-key": value})
    assert "error" not in resp, resp
    assert claims == [("send_message", value)] and len(impl_calls) == 1


def test_no_header_means_no_claim_and_an_empty_header_means_no_claim(idem):
    claims, impl_calls = idem
    for headers in (HDRS, {**HDRS, "x-idempotency-key": ""}):
        assert "error" not in rpc_call("send_message", _send_args(), headers=headers)
    assert claims == [] and len(impl_calls) == 2


def test_an_argument_key_still_wins_over_a_header_key(idem):
    claims, _ = idem
    rpc_call("send_message", _send_args(idempotency_key="from-argument"),
             headers={**HDRS, "x-idempotency-key": "from-header"})
    assert claims == [("send_message", "from-argument")]


def test_a_long_header_on_a_tool_that_does_not_declare_the_key_is_ignored_as_before(idem):
    claims, impl_calls = idem
    resp = rpc_call("lookup_us_contracts", {"company_name": "Acme"}, headers={**HDRS, "x-idempotency-key": "k" * 500})
    assert "error" not in resp, resp
    assert not claims and len(impl_calls) == 1


# ---------------------------------------------------------------------------
# P3: arguments that are not an object are refused before the claim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arguments", [[1], ["a", "b"], "text", 5, 1.5, True],
                         ids=["list", "list2", "string", "int", "float", "true"])
def test_arguments_that_are_not_an_object_never_take_the_idempotency_claim(idem, arguments):
    claims, impl_calls = idem
    resp = rpc_call("send_message", arguments, headers={**HDRS, "x-idempotency-key": "k-held"})
    err = resp.get("error") or {}
    assert err.get("code") == ERR_INVALID_PARAMS, resp
    assert "'arguments' must be a JSON object" in err["message"], err["message"]
    assert claims == [], "the key was claimed for a body that was never going to run"
    assert not impl_calls


def test_the_impl_still_refuses_non_object_arguments_for_a_caller_that_skips_the_wrapper():
    with pytest.raises(ms._ParamError):
        run(ms._h_tools_call_impl({"name": "send_message", "arguments": [1]}, {}))


def test_empty_arguments_keep_their_leniency(idem):
    """`arguments: []`, `0` and `""` have always meant 'no parameters'; that is not changed here."""
    claims, _ = idem
    for empty in ([], "", 0, None):
        resp = rpc_call("lookup_us_contracts", empty, headers=HDRS)
        assert "'arguments' must be a JSON object" not in json.dumps(resp), empty


# ---------------------------------------------------------------------------
# P3: a number that cannot be American is not judged by a US default from the environment
# ---------------------------------------------------------------------------

def test_a_plus_seven_recipient_is_not_judged_by_us_rules_when_the_environment_defaults_to_us(consent, monkeypatch):
    monkeypatch.setenv("COMPLIANCE_DEFAULT_JURISDICTION", "US")
    freeze_clock(monkeypatch, datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc))
    r = check("+77011234567", message_type="follow_up", content="Your quote is ready.")
    rule_set = r.result["rule_set"]
    assert rule_set["code"] != "US" and rule_set["basis"] == "conservative_default", rule_set
    assert rule_set["statutes_modeled"] == [], rule_set
    assert r.result.get("rule") != "TCPA_quiet_hours", r.result
    assert "TCPA" not in json.dumps(r.result)


def test_the_gate_agrees_with_its_own_description_for_that_recipient(consent, monkeypatch):
    monkeypatch.setenv("COMPLIANCE_DEFAULT_JURISDICTION", "US")
    freeze_clock(monkeypatch, datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc))
    with pytest.raises(ComplianceViolationError) as ei:
        gate("+77011234567", "sms", message_type="follow_up", content="Your quote is ready.")
    assert ei.value.rule != "TCPA_quiet_hours", ei.value.message


def test_the_environment_default_still_applies_where_nothing_rules_the_us_out(consent, monkeypatch):
    """The override exists for a recipient the gate knows nothing about; that purpose is kept."""
    monkeypatch.setenv("COMPLIANCE_DEFAULT_JURISDICTION", "US")
    r = check("buyer@example.com", channel="email", message_type="transactional",
              content="Your appointment is confirmed.")
    assert r.result["rule_set"]["code"] == "US"
    monkeypatch.delenv("COMPLIANCE_DEFAULT_JURISDICTION")
    r = check("buyer@example.com", channel="email", message_type="transactional",
              content="Your appointment is confirmed.")
    assert r.result["rule_set"]["code"] == "INTERNATIONAL"
