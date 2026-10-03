"""The rule set a send is judged under must be the recipient's, and must never be a US one for a non-US number.

THE DEFECT (Door Reliability Run, 2026-10-03, defect D2). `check_compliance` for the Omani number
+96891234567 with country_code "OM" answered

    jurisdiction: OM     rule: TCPA_marketing_consent
    remediation: "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers..."

A US statute, named as the legal basis, for a number that is not American. Three things were wrong at once,
and each is pinned below:

  1. THE RULE NAME WAS HARD-WIRED. The marketing-SMS consent branch raised "TCPA_marketing_consent" for every
     jurisdiction whose rule set asks for opt-in, which is all of them (the field defaults to True), so a
     German or Canadian marketing SMS was also refused "under the TCPA". The same was true of quiet hours
     ("TCPA_quiet_hours" for any country) and of 10DLC, which was applied to any SMS sent without a
     country_code, an Omani number included.
  2. THE JURISDICTION WAS WHATEVER THE CALLER SAID. The tool's own schema promises the country is
     "auto-inferred from phone if omitted"; nothing did that except the quiet-hours check.
  3. AN UNKNOWN JURISDICTION WAS LABELLED "US" in the result and the message.

THE CONTRACT NOW. The rule set follows the recipient's number when the number identifies a country. Where no
country-specific statute is implemented (every GCC state, for one) the answer says so and applies the
service's own conservative default, instead of citing a law that was not applied. The US, GDPR and CASL
answers are unchanged.
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

from compliance import consent_store as cs_module  # noqa: E402
from compliance.consent_store import ConsentStatus, ConsentStore  # noqa: E402
from core.check_compliance import handle_check_compliance  # noqa: E402
from core.models import ComplianceViolationError  # noqa: E402

US_WORDS = re.compile(r"TCPA|10DLC|\bUS\b|U\.S\.|United States|\bUSA\b|American")


def run(coro):
    return asyncio.run(coro)


def check(recipient_id, content="20% off this week only!", **kw):
    kw.setdefault("channel", "sms")
    kw.setdefault("message_type", "marketing")
    return run(handle_check_compliance(recipient_id=recipient_id, content=content, **kw))


def everything_said(r) -> str:
    """Every sentence a caller reads in a result."""
    res = r.result or {}
    parts = [r.human_message or "", *(r.next_actions or []),
             res.get("remediation") or "", res.get("human_message") or "", json.dumps(res.get("rule_set") or {}),
             res.get("jurisdiction_conflict") or ""]
    return "\n".join(parts)


@pytest.fixture
def fresh_consent_store():
    original = cs_module._store
    cs_module._store = ConsentStore()
    try:
        yield cs_module._store
    finally:
        cs_module._store = original


# ---------------------------------------------------------------------------
# Which country is a number from
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("number,country", [
    ("+96891234567", "OM"), ("+971501234567", "AE"), ("+966512345678", "SA"), ("+97455123456", "QA"),
    ("+96551234567", "KW"), ("+97336123456", "BH"), ("+442071838750", "GB"), ("+4915112345678", "DE"),
    ("+33612345678", "FR"), ("+393123456789", "IT"), ("+34612345678", "ES"), ("+31612345678", "NL"),
    ("+919876543210", "IN"), ("+923001234567", "PK"), ("+81312345678", "JP"), ("+821012345678", "KR"),
    ("+6591234567", "SG"), ("+61412345678", "AU"), ("+64211234567", "NZ"), ("+5511912345678", "BR"),
    ("+5215512345678", "MX"), ("+628123456789", "ID"), ("+66812345678", "TH"), ("+2348012345678", "NG"),
    ("+27821234567", "ZA"), ("+201001234567", "EG"), ("+905321234567", "TR"), ("+8613812345678", "CN"),
    ("+85291234567", "HK"), ("+962791234567", "JO"), ("+9647701234567", "IQ"), ("+96171123456", "LB"),
    ("+212612345678", "MA"), ("+254712345678", "KE"), ("+380501234567", "UA"), ("+353851234567", "IE"),
    ("+358401234567", "FI"), ("+972501234567", "IL"), ("+9779812345678", "NP"), ("+998901234567", "UZ"),
    ("+670771234567", "TL"), ("+77011234567", None), ("+14045550100", None),
])
def test_a_number_names_its_own_country(number, country):
    from compliance.number_jurisdiction import country_of_number
    assert country_of_number(number) == country


@pytest.mark.parametrize("separated", [
    "+968 9123 4567", "+968-9123-4567", "+968 (9123) 4567", "00968 91234567", "+968.9123.4567", " +96891234567 ",
])
def test_the_usual_ways_of_writing_a_number_all_resolve(separated):
    from compliance.number_jurisdiction import country_of_number
    assert country_of_number(separated) == "OM"


@pytest.mark.parametrize("not_a_number", [
    None, "", "   ", "anna@example.de", "user968@example.com", "96891234567", "91234567", "+968", "+",
    "+9689123456789012345", "call me", "+96891234567x", "smb_12345", "+999123456789", "+800123456789",
])
def test_anything_that_is_not_an_e164_number_names_no_country(not_a_number):
    """Digits inside an email address or a bare local number must never be read as a calling code."""
    from compliance.number_jurisdiction import country_of_number
    assert country_of_number(not_a_number) is None


def test_the_calling_code_table_is_prefix_free():
    """E.164 calling codes are prefix-free by design; a table that is not has an entry that can never match."""
    from compliance import number_jurisdiction as nj
    codes = set(nj.CALLING_CODES) | set(nj.SHARED_CALLING_CODES)
    for a in codes:
        for b in codes:
            assert a == b or not b.startswith(a), f"+{a} is a prefix of +{b}"
    assert all(re.fullmatch(r"[A-Z]{2}", c) for c in nj.CALLING_CODES.values())
    assert len(nj.CALLING_CODES) > 180


def test_the_table_agrees_with_the_one_quiet_hours_used_before():
    from compliance.number_jurisdiction import country_of_number
    from compliance.quiet_hours import country_from_number
    for n in ("+96891234567", "+971501234567", "+442071838750", "+919876543210", "+6591234567"):
        assert country_from_number(n) == country_of_number(n)


# ---------------------------------------------------------------------------
# Which country the gate decides under
# ---------------------------------------------------------------------------

def test_the_number_decides_when_no_country_code_is_given():
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction("+96891234567", None)
    assert (r.country, r.source, r.conflict) == ("OM", "recipient_number", None)


def test_a_consistent_country_code_is_the_callers():
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction("+96891234567", "om")
    assert (r.country, r.source, r.conflict) == ("OM", "caller", None)


def test_a_country_code_that_contradicts_the_number_loses_and_says_so():
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction("+96891234567", "US")
    assert r.country == "OM" and r.source == "recipient_number"
    assert r.conflict and "US" in r.conflict and "968" in r.conflict and "OM" in r.conflict


def test_a_plus_one_number_is_either_us_or_canada_and_the_caller_picks():
    from compliance.number_jurisdiction import resolve_jurisdiction
    assert resolve_jurisdiction("+14045550100", "US").country == "US"
    assert resolve_jurisdiction("+14165550100", "CA").country == "CA"
    assert resolve_jurisdiction("+14045550100", None).country is None       # cannot tell: unresolved


def test_a_plus_one_number_with_a_non_north_american_country_code_is_unresolved_not_guessed():
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction("+14045550100", "OM")
    assert r.country is None and r.source == "unknown" and r.conflict


def test_an_email_has_only_the_callers_country():
    from compliance.number_jurisdiction import resolve_jurisdiction
    assert resolve_jurisdiction("anna@example.de", "de").country == "DE"
    assert resolve_jurisdiction("anna@example.de", None).country is None


def test_hostile_country_code_text_is_ignored_and_never_repeated():
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction("+96891234567", "zzz hostile text")
    assert r.country == "OM" and r.source == "recipient_number"
    assert r.conflict and "hostile" not in r.conflict.lower() and "ignored" in r.conflict
    assert resolve_jurisdiction("anna@example.de", "zzz hostile text").country is None


def test_hostile_text_never_reaches_the_answer_label_or_the_receipts_evidence():
    """The receipt's `subject` and `inputs` record what was submitted, verbatim and hash-bound, on purpose; the
    answer, the label and the receipt's evidence are OUR sentences and must not repeat it."""
    from core import compliance_receipt as CR
    r = check("anna@example.de", channel="email", country_code="zzz hostile text", state_code="drop table")
    answer = {k: v for k, v in r.result.items() if k != CR.RECEIPT_FIELD}
    said = everything_said(r) + json.dumps(answer, default=str) + json.dumps(
        r.result[CR.RECEIPT_FIELD]["payload"]["evidence"], default=str)
    assert "hostile" not in said.lower() and "drop table" not in said.lower()
    assert r.result["jurisdiction"] == "unknown" and r.result["rule"] == "jurisdiction_required"


def test_a_lower_case_country_code_is_the_same_country():
    r = check("+96891234567", country_code="om")
    assert r.result["jurisdiction"] == "OM" and r.result["jurisdiction_source"] == "caller"


# ---------------------------------------------------------------------------
# THE DEFECT, through the tool a reviewer calls
# ---------------------------------------------------------------------------

def test_drr_d2_an_omani_number_is_never_judged_under_a_us_statute():
    r = check("+96891234567", country_code="OM")
    res = r.result
    assert res["legal"] is False                       # not green-lit: the recipient has not opted in
    assert res["jurisdiction"] == "OM"
    assert res["rule"] == "sms_marketing_consent"
    assert not US_WORDS.search(everything_said(r)), everything_said(r)
    assert res["rule"] not in ("TCPA_marketing_consent", "10DLC_campaign_not_registered", "TCPA_quiet_hours")


def test_the_omani_answer_says_honestly_that_no_omani_statute_is_modelled():
    r = check("+96891234567", country_code="OM")
    rs = r.result["rule_set"]
    assert rs["basis"] == "conservative_default"
    assert rs["statutes_modeled"] == []
    said = everything_said(r)
    assert "No OM-specific consent statute is implemented" in said
    assert "conservative default" in said
    assert "not a citation of OM law" in said


def test_with_no_country_code_the_number_alone_selects_omani_treatment():
    """The schema promised 'auto-inferred from phone if omitted'. Before: jurisdiction_required."""
    r = check("+96891234567")
    res = r.result
    assert res["rule"] == "sms_marketing_consent" and res["jurisdiction"] == "OM"
    assert res["jurisdiction_source"] == "recipient_number"
    assert not US_WORDS.search(everything_said(r))


def test_the_door_a_reviewer_uses_gives_the_same_answer():
    from agent_interface.mcp_server import handle_mcp_request
    resp = run(handle_mcp_request({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "check_compliance", "arguments": {
            "recipient_id": "+96891234567", "content": "Big sale today!", "channel": "sms",
            "message_type": "marketing", "country_code": "OM"}}}, {}, "compliance-check"))
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["result"]["rule"] == "sms_marketing_consent"
    assert not US_WORDS.search(json.dumps(body))


@pytest.mark.parametrize("number,country", [
    ("+971501234567", "AE"), ("+966512345678", "SA"), ("+97455123456", "QA"),
    ("+96551234567", "KW"), ("+97336123456", "BH"),
])
def test_every_gulf_state_gets_the_same_honest_treatment(number, country):
    r = check(number, country_code=country)
    assert r.result["rule"] == "sms_marketing_consent" and r.result["jurisdiction"] == country
    assert f"No {country}-specific consent statute is implemented" in everything_said(r)
    assert not US_WORDS.search(everything_said(r))


def test_a_country_with_no_rule_set_at_all_says_that_in_those_words():
    r = check("+66812345678", country_code="TH")
    assert r.result["rule"] == "sms_marketing_consent"
    assert r.result["rule_set"]["code"] == "INTERNATIONAL"
    said = everything_said(r)
    assert "No rule set is implemented for TH" in said and "INTERNATIONAL" in said
    assert not US_WORDS.search(said)


def test_a_us_number_keeps_the_us_answer_exactly():
    r = check("+14045550200", country_code="US")
    assert r.result["rule"] == "TCPA_marketing_consent"
    assert "TCPA" in r.result["remediation"] and "US numbers" in r.result["remediation"]
    assert r.result["jurisdiction"] == "US"
    assert r.result["rule_set"]["statutes_modeled"] == ["TCPA", "CAN-SPAM", "10DLC"]
    assert r.result["rule_set"]["basis"] == "statute"


def test_a_german_marketing_sms_is_judged_under_gdpr_not_the_tcpa():
    r = check("+4915112345678", country_code="DE")
    assert r.result["rule"] == "GDPR_marketing_consent"
    assert "TCPA" not in everything_said(r)
    assert r.result["rule_set"]["statutes_modeled"] == ["GDPR"]


def test_a_canadian_marketing_sms_is_judged_under_casl_not_the_tcpa():
    r = check("+14165550100", country_code="CA")
    assert r.result["rule"] == "CASL_marketing_consent"
    assert "TCPA" not in everything_said(r)


def test_a_us_state_keeps_its_state_label():
    r = check("+14155551234", country_code="US", state_code="CA")
    assert r.result["rule"] == "TCPA_marketing_consent" and r.result["jurisdiction"] == "US-CA"


def test_a_conflicting_country_code_is_reported_and_the_number_wins():
    """For a message that is NOT a solicitation. (This test first used a marketing message and pinned "the
    number wins" for it; the review of 2026-10-04 showed that was the unsafe direction, because the number's
    country can have the milder calling-hours window. A marketing or follow-up message with a contradicting
    country_code is now refused as `jurisdiction_conflict` - see test_gate_review_fixes_20261004.py.)"""
    r = check("+96891234567", country_code="US", message_type="reminder",
              content="Your appointment is tomorrow at 10:30.")
    res = r.result
    assert res["jurisdiction"] == "OM" and res["legal"] is True
    assert res["jurisdiction_source"] == "recipient_number"
    assert "US" in res["jurisdiction_conflict"] and "968" in res["jurisdiction_conflict"]
    assert not re.search(r"TCPA|10DLC", everything_said(r).replace(res["jurisdiction_conflict"], ""))


def test_an_unresolvable_marketing_jurisdiction_is_refused_and_says_why():
    r = check("+14045550100", country_code="OM")
    assert r.result["rule"] == "jurisdiction_required" and r.result["legal"] is False
    assert "calling code" in r.result["human_message"] or "calling code" in r.human_message


def test_a_marketing_email_with_no_country_is_still_refused_for_want_of_a_jurisdiction():
    r = check("anna@example.de", channel="email")
    assert r.result["rule"] == "jurisdiction_required"
    assert r.result["jurisdiction"] == "unknown"                    # never "US"
    assert "pass a country_code" in r.result["remediation"].lower() or "country_code" in r.result["remediation"]


def test_an_unknown_jurisdiction_is_not_labelled_us():
    r = check("anna@example.de", channel="email", message_type="transactional", content="Your booking is confirmed.")
    assert r.result["legal"] is True
    assert r.result["jurisdiction"] == "unknown"
    assert not US_WORDS.search(r.human_message), r.human_message
    assert r.result["rule_set"]["code"] == "INTERNATIONAL"
    assert "No country was supplied" in r.result["rule_set"]["note"]


def test_a_permitted_send_under_a_default_rule_set_does_not_claim_a_local_determination(
        monkeypatch, fresh_consent_store):
    fresh_consent_store.record_consent("+96891234567", "sms", "marketing", ConsentStatus.OPTED_IN, "OM",
                                       "express_written", "test")
    _frozen_quiet_hours(monkeypatch, datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))        # noon in Muscat
    r = check("+96891234567", country_code="OM")
    assert r.result["legal"] is True
    assert "not a determination of OM law" in r.human_message
    assert not US_WORDS.search(everything_said(r))


# ---------------------------------------------------------------------------
# 10DLC and quiet hours were US rules applied to everyone
# ---------------------------------------------------------------------------

def test_10dlc_is_not_applied_to_an_omani_number_sent_without_a_country_code():
    """Transactional SMS, no country_code: before, any SMS without a code was assumed American and refused for
    want of a US carrier registration."""
    from compliance.pre_check import pre_check
    pre_check(recipient_id="+96891234567", channel="sms", message_type="transactional",
              content="Your appointment is confirmed for 10:30.", preview=True)


def test_10dlc_still_applies_to_a_us_number():
    from compliance.pre_check import pre_check
    with pytest.raises(ComplianceViolationError) as ei:
        pre_check(recipient_id="+14045550100", channel="sms", message_type="transactional",
                  content="Your appointment is confirmed for 10:30.", country_code="US", preview=True)
    assert ei.value.rule == "10DLC_campaign_not_registered"


def _frozen_quiet_hours(monkeypatch, when: datetime):
    import compliance.quiet_hours as qh
    original = qh.check
    monkeypatch.setattr(qh, "check", lambda mt, cc=None, sc=None, now_utc=None, recipient_id=None, channel=None:
                        original(mt, cc, sc, when, recipient_id=recipient_id, channel=channel))


def test_quiet_hours_outside_the_us_are_not_called_tcpa(monkeypatch, fresh_consent_store):
    fresh_consent_store.record_consent("+96891234567", "sms", "marketing", ConsentStatus.OPTED_IN, "OM",
                                       "express_written", "test")
    _frozen_quiet_hours(monkeypatch, datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc))      # 03:00 in Muscat
    r = check("+96891234567", country_code="OM")
    assert r.result["legal"] is False
    assert r.result["rule"] == "quiet_hours"
    said = everything_said(r)
    assert "TCPA" not in said and "No OM-specific hours are implemented" in said
    assert r.result["remediation"] and "TCPA" not in r.result["remediation"]


def test_quiet_hours_in_the_us_are_still_the_tcpa(monkeypatch, fresh_consent_store):
    fresh_consent_store.record_consent("+14155551234", "sms", "marketing", ConsentStatus.OPTED_IN, "US",
                                       "express_written", "test")
    _frozen_quiet_hours(monkeypatch, datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc))      # 04:00 in California
    r = check("+14155551234", country_code="US", state_code="CA")
    assert r.result["rule"] == "TCPA_quiet_hours"


def test_quiet_hours_infers_the_country_from_the_number_through_the_shared_table():
    from compliance.quiet_hours import check as qh_check
    d = qh_check("marketing", None, None, datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc),
                 recipient_id="+96891234567", channel="sms")
    assert d.allowed and d.reason == "within_window"


# ---------------------------------------------------------------------------
# The receipt, the HTTP twin and the shared remediation text
# ---------------------------------------------------------------------------

def test_the_evidence_receipt_records_the_jurisdiction_actually_applied():
    from core import compliance_receipt as CR
    r = check("+96891234567")
    ev = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]
    assert ev["decision"]["rule"] == "sms_marketing_consent"
    assert ev["decision"]["jurisdiction"] == "OM"
    assert ev["ruleset"]["country_applied"] == "OM"
    assert ev["ruleset"]["jurisdiction_source"] == "recipient_number"
    assert ev["ruleset"]["jurisdiction_supplied_by_caller"] is False
    assert CR.verify_compliance_receipt(r.result[CR.RECEIPT_FIELD], response_payload=r.result)["hash_ok"] is True


def test_the_receipt_scope_names_10dlc_only_where_it_applies():
    from core import compliance_receipt as CR
    oman = check("+96891234567")
    assert "10DLC" not in oman.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["scope"]
    us = check("+14045550200", country_code="US")
    assert "10DLC" in us.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["scope"]
    email = check("anna@example.de", channel="email", country_code="DE", message_type="transactional",
                  content="Your booking is confirmed.")
    assert "10DLC" not in email.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["scope"]


def test_the_receipt_no_longer_says_the_jurisdiction_was_only_ever_the_callers():
    from core import check_compliance as cc
    assert any("country calling code" in s for s in cc._DOES_NOT_ASSERT)
    assert not any("was supplied by the caller or defaulted" in s for s in cc._DOES_NOT_ASSERT)


def test_the_http_twin_and_the_tool_share_one_remediation_table():
    import main
    from compliance.remediation import remediation_for
    from core import check_compliance as cc
    for rule in ("TCPA_marketing_consent", "sms_marketing_consent", "quiet_hours", "jurisdiction_required",
                 "GDPR_marketing_consent", "CASL_marketing_consent", "10DLC_campaign_not_registered",
                 "restricted_content", "recipient_opted_out", "voice_marketing_consent",
                 "email_marketing_consent", "a_rule_nobody_has_heard_of"):
        assert main._remediation_for(rule) == cc._remediation_for(rule) == remediation_for(rule)


def test_the_new_rules_have_specific_remediation_and_none_of_it_is_american():
    from compliance.remediation import remediation_for
    generic = remediation_for("a_rule_nobody_has_heard_of")
    for rule in ("sms_marketing_consent", "quiet_hours", "jurisdiction_required",
                 "voice_marketing_consent", "email_marketing_consent"):
        text = remediation_for(rule)
        assert text != generic, rule
        assert not US_WORDS.search(text), (rule, text)


def test_the_http_check_picks_the_number_s_country_too():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": "marketing",
        "content": "Big sale today!"})
    body = resp.json()
    assert resp.status_code == 200, body
    text = json.dumps(body)
    assert body.get("rule") == "sms_marketing_consent" and body.get("rule_set") == "OM", body
    assert not US_WORDS.search(text), text


def test_the_http_twin_labels_a_permitted_send_with_the_country_it_resolved():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app, raise_server_exceptions=False)
    body = client.post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": "transactional",
        "content": "Your appointment is confirmed for 10:30."}).json()
    assert body.get("legal") is True and body.get("rule_set") == "OM", body


def test_the_public_jurisdiction_list_says_which_rule_sets_are_statutes():
    from fastapi.testclient import TestClient
    import main
    body = TestClient(main.app).get("/compliance/jurisdictions").json()
    by = {r["code"]: r for r in body["rule_sets"]}
    assert by["US"]["basis"] == "statute" and "TCPA" in by["US"]["statutes_modeled"]
    assert by["DE"]["statutes_modeled"] == ["GDPR"] and by["CA"]["statutes_modeled"] == ["CASL"]
    assert by["OM"]["basis"] == "conservative_default" and by["OM"]["statutes_modeled"] == []
    assert body["supported"], "the existing key must stay"
    assert "conservative" in body["note"].lower()
