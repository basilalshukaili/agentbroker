"""The compliance gate, after the independent review of the D2 fix (2026-10-04).

The review ran the gate on the base commit and on the D2 commit over a 46,980-case matrix and found where the
D2 change moved an answer in the unsafe direction, or left a US rule on a non-US number. Each defect below was
reproduced first and each test here failed on the D2 commit.

  P1  WHATSAPP. A marketing message on WhatsApp has no consent branch in the gate, because the branches cover
      sms, voice and email and, inside the GDPR and CASL blocs, every channel. Before D2 such a send to an
      Omani number was refused only because no country was known (`jurisdiction_required`); D2 inferred the
      country from the number and so removed the only barrier. Marketing on ANY channel with no branch of its
      own now needs a recorded opt-in on THAT channel.
  P2  +7. Russia and Kazakhstan share the calling code, so the number cannot say which; the jurisdiction is
      unresolved, and unresolved was still treated as "maybe American" for 10DLC, a US carrier rule. A number
      that cannot be a US number is never judged by a US rule.
  P2  "THE NUMBER WINS" IS NOT THE SAFE SIDE. A caller who states a country that contradicts the number used
      to be judged by the country they stated; D2 judged them by the number's country instead, which turned
      2,914 refusals into allows (1,154 of them US calling-hours blocks). For a solicitation (marketing and
      follow-up) the gate now refuses to choose and says so (`jurisdiction_conflict`). For everything else the
      number still wins, and the answer reports that.
  P2  SEND_MESSAGE did not tell a sender that their country_code had been overridden; only the free
      preview did.
  P3  Voice and email refusals outside a modeled statute read like local law; they now say they are the
      service's own default. The public HTTP check returns the structured rule basis. Common non-ISO country
      aliases (UK, USA, GBR...) are read as the country they name instead of as a contradiction.
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
from compliance.pre_check import pre_check  # noqa: E402
from core.check_compliance import handle_check_compliance  # noqa: E402
from core.models import ComplianceViolationError  # noqa: E402

US_RULES = re.compile(r"TCPA|10DLC|A2P|carrier-registered")

NOON_MUSCAT = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)          # 12:00 in Muscat, 04:00 in California
THREE_AM_UTC = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)         # 08:30 in India, 20:00 the day before in CA


def run(coro):
    return asyncio.run(coro)


def check(recipient_id, content="20% off this week only!", **kw):
    kw.setdefault("channel", "sms")
    kw.setdefault("message_type", "marketing")
    return run(handle_check_compliance(recipient_id=recipient_id, content=content, **kw))


def said(r) -> str:
    res = r.result or {}
    return "\n".join([r.human_message or "", *(r.next_actions or []), res.get("remediation") or "",
                      res.get("human_message") or "", json.dumps(res.get("rule_set") or {}),
                      res.get("jurisdiction_conflict") or ""])


def gate(recipient, channel, message_type="marketing", content="20% off this week only!", **kw):
    pre_check(recipient_id=recipient, channel=channel, message_type=message_type, content=content,
              preview=True, **kw)


@pytest.fixture
def consent():
    original = cs_module._store
    cs_module._store = ConsentStore()
    try:
        yield cs_module._store
    finally:
        cs_module._store = original


def opt_in(store, recipient, channel, country):
    store.record_consent(recipient, channel, "marketing", ConsentStatus.OPTED_IN, country,
                         "express_written", "test")


def freeze_clock(monkeypatch, when: datetime):
    import compliance.quiet_hours as qh
    original = qh.check
    monkeypatch.setattr(qh, "check", lambda mt, cc=None, sc=None, now_utc=None, recipient_id=None, channel=None:
                        original(mt, cc, sc, when, recipient_id=recipient_id, channel=channel))


# ---------------------------------------------------------------------------
# P1: marketing on a channel the gate has no branch for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recipient,kw", [
    ("+96891234567", {}),                                    # no country_code: the number names Oman
    ("+96891234567", {"country_code": "OM"}),
    ("+971501234567", {}),
    ("+966512345678", {"country_code": "SA"}),
    ("+66812345678", {}),                                    # Thailand: no rule set at all
    ("+14045550100", {"country_code": "US", "state_code": "GA"}),
], ids=["om-no-code", "om", "ae", "sa", "th", "us"])
def test_a_whatsapp_marketing_message_with_no_recorded_opt_in_is_refused(consent, recipient, kw):
    with pytest.raises(ComplianceViolationError) as ei:
        gate(recipient, "whatsapp", **kw)
    assert ei.value.rule == "whatsapp_marketing_consent"
    assert ei.value.channel == "whatsapp"
    assert not US_RULES.search(ei.value.message), ei.value.message


@pytest.mark.parametrize("recipient,country,rule", [
    ("+4915112345678", "DE", "GDPR_marketing_consent"),
    ("+442071838750", "GB", "GDPR_marketing_consent"),
    ("+14165550100", "CA", "CASL_marketing_consent"),
])
def test_a_whatsapp_marketing_message_in_a_statute_bloc_keeps_the_statutes_own_rule(consent, recipient, country, rule):
    with pytest.raises(ComplianceViolationError) as ei:
        gate(recipient, "whatsapp", country_code=country)
    assert ei.value.rule == rule


def test_a_whatsapp_marketing_message_with_a_recorded_opt_in_goes_through(consent, monkeypatch):
    opt_in(consent, "+96891234567", "whatsapp", "OM")
    freeze_clock(monkeypatch, NOON_MUSCAT)
    gate("+96891234567", "whatsapp")                                  # does not raise


def test_an_opt_in_for_sms_does_not_cover_whatsapp(consent, monkeypatch):
    opt_in(consent, "+96891234567", "sms", "OM")
    freeze_clock(monkeypatch, NOON_MUSCAT)
    with pytest.raises(ComplianceViolationError) as ei:
        gate("+96891234567", "whatsapp")
    assert ei.value.rule == "whatsapp_marketing_consent"


def test_a_whatsapp_message_that_is_not_marketing_needs_no_opt_in(consent):
    gate("+96891234567", "whatsapp", message_type="transactional", content="Your booking is confirmed.")
    gate("+96891234567", "whatsapp", message_type="reminder", content="Your appointment is tomorrow at 10:30.")


@pytest.mark.parametrize("channel", ["telegram", "rcs", "push", "signal"])
def test_marketing_on_any_channel_the_gate_has_no_branch_for_fails_closed(consent, channel):
    with pytest.raises(ComplianceViolationError) as ei:
        gate("+96891234567", channel)
    assert ei.value.rule == "marketing_consent"


def test_the_whatsapp_refusal_reaches_the_sender_through_send_message(consent):
    import core.send_message as SM
    from core.models import (ChannelPreference, MessageContent, MessageType, Recipient, RecipientIdType,
                             SendMessageRequest)
    req = SendMessageRequest(
        recipient=Recipient(id_type=RecipientIdType.PHONE, id_value="+96891234567"),
        content=MessageContent(body="20% off this week only!"), message_type=MessageType.MARKETING,
        preferred_channel=ChannelPreference.WHATSAPP)
    receipt = run(SM.handle_send_message(req))
    assert receipt.reason_code == "compliance_violation", receipt
    assert receipt.result["rule"] == "whatsapp_marketing_consent"


def test_the_new_rules_have_their_own_remediation_and_none_of_it_is_american():
    from compliance.remediation import remediation_for
    generic = remediation_for("a_rule_nobody_has_heard_of")
    for rule in ("whatsapp_marketing_consent", "marketing_consent", "jurisdiction_conflict"):
        text = remediation_for(rule)
        assert text != generic, rule
        assert not US_RULES.search(text), (rule, text)


# ---------------------------------------------------------------------------
# P2: +7 is Russia or Kazakhstan, and never a US number
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("number", ["+77011234567", "+79161234567"])
@pytest.mark.parametrize("country_code", [None, "KZ", "RU", "DE", "OM", "US"])
def test_a_plus_seven_number_is_never_judged_by_a_us_rule(number, country_code):
    from core import compliance_receipt as CR
    r = check(number, channel="sms", message_type="transactional",
              content="Your appointment is confirmed for 10:30.", country_code=country_code)
    assert r.result["legal"] is True, (country_code, r.result.get("rule"), said(r))
    assert r.result["rule"] is None
    assert not US_RULES.search(said(r).replace(r.result.get("jurisdiction_conflict") or "", "")), said(r)
    scope = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["scope"]
    assert "10DLC" not in scope, scope


@pytest.mark.parametrize("number", ["+14045550100", "+14165550100"])
@pytest.mark.parametrize("country_code", [None, "OM", "US"])
def test_a_north_american_number_still_gets_the_10dlc_check_when_the_country_is_unsettled(number, country_code):
    """+1 can be a US number, so the US carrier rule still applies unless the caller says it is Canadian."""
    r = check(number, channel="sms", message_type="transactional",
              content="Your appointment is confirmed for 10:30.", country_code=country_code)
    assert r.result["rule"] == "10DLC_campaign_not_registered", (number, country_code, r.result)


def test_a_canadian_number_with_the_country_given_is_not_a_10dlc_case():
    r = check("+14165550100", channel="sms", message_type="transactional",
              content="Your appointment is confirmed for 10:30.", country_code="CA")
    assert r.result["legal"] is True and r.result["rule"] is None


def test_a_plus_seven_number_needs_the_caller_to_say_which_country():
    from compliance.number_jurisdiction import resolve_jurisdiction
    assert resolve_jurisdiction("+77011234567", None).country is None
    assert resolve_jurisdiction("+77011234567", "KZ").country == "KZ"
    assert resolve_jurisdiction("+79161234567", "ru").country == "RU"
    r = resolve_jurisdiction("+77011234567", "US")
    assert r.country is None and r.conflict and "Russia or Kazakhstan" in r.conflict
    r = resolve_jurisdiction("+77011234567", "DE")
    assert r.country is None and r.conflict


def test_the_shared_calling_codes_are_exactly_the_two_this_module_documents():
    from compliance import number_jurisdiction as nj
    assert set(nj.SHARED_CALLING_CODES) == {"1", "7"}
    assert set(nj.SHARED_CALLING_CODES["7"]) == {"RU", "KZ"}
    assert "7" not in nj.CALLING_CODES, "+7 names no single country; it must not be in the single-country table"
    assert nj.country_of_number("+77011234567") is None


def test_a_plus_seven_marketing_send_is_refused_for_want_of_a_country_and_then_judged_as_the_country_named():
    r = check("+77011234567", channel="sms", message_type="marketing")
    assert r.result["rule"] == "jurisdiction_required"
    r = check("+77011234567", channel="sms", message_type="marketing", country_code="KZ")
    assert r.result["rule"] == "sms_marketing_consent" and r.result["jurisdiction"] == "KZ"
    assert not US_RULES.search(said(r))


# ---------------------------------------------------------------------------
# P2: a contradicting country_code must not weaken a solicitation
# ---------------------------------------------------------------------------

SOLICITATIONS = [
    # (recipient, channel, message_type, country_code, state_code, clock, what the BASE commit answered)
    ("+96891234567", "sms", "marketing", "US", "CA", NOON_MUSCAT, "TCPA_quiet_hours"),
    ("+96891234567", "voice", "marketing", "US", "CA", NOON_MUSCAT, "TCPA_quiet_hours"),
    ("+919876543210", "sms", "marketing", "US", None, THREE_AM_UTC, "TCPA_quiet_hours"),
    ("+919876543210", "voice", "marketing", "US", None, THREE_AM_UTC, "TCPA_quiet_hours"),
    ("+919876543210", "sms", "marketing", "US", "CA", THREE_AM_UTC, "10DLC_campaign_not_registered"),
    ("+96891234567", "sms", "follow_up", "US", "CA", NOON_MUSCAT, "TCPA_quiet_hours"),
    ("+96891234567", "voice", "follow_up", "US", "CA", NOON_MUSCAT, "TCPA_quiet_hours"),
]


@pytest.mark.parametrize("recipient,channel,mtype,supplied,state,clock,base_rule", SOLICITATIONS)
def test_a_solicitation_whose_stated_country_contradicts_the_number_is_not_made_weaker(
        consent, monkeypatch, recipient, channel, mtype, supplied, state, clock, base_rule):
    """Before D2 these were all BLOCKED (base_rule). D2 let the number's country override the caller's, and the
    number's country had the milder window or no carrier rule: every one became an allow."""
    opt_in(consent, recipient, channel, supplied)
    freeze_clock(monkeypatch, clock)
    with pytest.raises(ComplianceViolationError) as ei:
        gate(recipient, channel, message_type=mtype, country_code=supplied, state_code=state)
    assert ei.value.rule == "jurisdiction_conflict", (ei.value.rule, base_rule)
    assert ei.value.jurisdiction == "unknown"
    assert supplied in ei.value.message and ei.value.message.count("contradicts") == 1


def test_a_refused_conflict_is_reported_by_the_free_preview_with_its_own_remediation():
    from core import compliance_receipt as CR
    r = check("+96891234567", country_code="US", state_code="CA")
    res = r.result
    assert res["legal"] is False and res["rule"] == "jurisdiction_conflict"
    assert res["jurisdiction"] == "unknown" and res["jurisdiction_source"] == "unknown"
    assert "OM" in res["jurisdiction_conflict"] and "US" in res["jurisdiction_conflict"]
    assert res["rule_set"]["basis"] == "undecided" and res["rule_set"]["code"] is None
    assert res["remediation"] and "country_code" in res["remediation"]
    assert not US_RULES.search(said(r))
    ev = res[CR.RECEIPT_FIELD]["payload"]["evidence"]
    assert ev["decision"]["rule"] == "jurisdiction_conflict"
    assert ev["ruleset"]["country_applied"] is None
    assert ev["ruleset"]["jurisdiction_conflict"]


@pytest.mark.parametrize("message_type", ["transactional", "reminder", "notification", "otp"])
def test_a_conflict_on_a_message_that_is_not_a_solicitation_keeps_number_wins_and_says_so(message_type):
    r = check("+96891234567", channel="sms", message_type=message_type, country_code="US",
              content="Your appointment is confirmed for 10:30.")
    res = r.result
    assert res["legal"] is True and res["jurisdiction"] == "OM"
    assert res["jurisdiction_source"] == "recipient_number"
    assert "US" in res["jurisdiction_conflict"] and "968" in res["jurisdiction_conflict"]
    assert "OM was used" in res["jurisdiction_conflict"]


def test_a_consistent_country_code_is_not_a_conflict_for_a_solicitation(consent, monkeypatch):
    opt_in(consent, "+96891234567", "sms", "OM")
    freeze_clock(monkeypatch, NOON_MUSCAT)
    gate("+96891234567", "sms", country_code="om")                    # does not raise
    gate("+96891234567", "sms")                                       # no country_code at all: does not raise


def test_an_eu_wide_country_code_is_consistent_with_a_member_states_number():
    """"EU" is a rule set the service has; a German number with country_code "EU" is not a contradiction."""
    r = check("+4915112345678", country_code="EU")
    assert r.result["rule"] == "GDPR_marketing_consent" and r.result["jurisdiction"] == "DE"
    assert "jurisdiction_conflict" not in r.result


def test_an_eu_country_code_does_not_cover_a_number_from_outside_the_eu():
    r = check("+442071838750", country_code="EU")                       # the UK is not an EU member state
    assert r.result["rule"] == "jurisdiction_conflict", r.result


def test_a_hostile_country_code_is_ignored_not_treated_as_a_conflict(consent, monkeypatch):
    opt_in(consent, "+96891234567", "sms", "OM")
    freeze_clock(monkeypatch, NOON_MUSCAT)
    gate("+96891234567", "sms", country_code="zzz hostile text")      # ignored, so nothing contradicts


def test_the_http_check_refuses_the_same_conflict():
    from fastapi.testclient import TestClient
    import main
    body = TestClient(main.app, raise_server_exceptions=False).post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": "marketing",
        "content": "Big sale today!", "country_code": "US", "state_code": "CA"}).json()
    assert body["legal"] is False and body["rule"] == "jurisdiction_conflict", body
    assert body["jurisdiction_conflict"] and not US_RULES.search(body["remediation"])


# ---------------------------------------------------------------------------
# P2: send_message tells the sender what was done with their country_code
# ---------------------------------------------------------------------------

def _send_request(recipient, message_type, country_code=None, channel="sms"):
    from core.models import (ChannelPreference, MessageContent, Recipient, RecipientIdType,
                             SendMessageRequest)
    return SendMessageRequest(
        recipient=Recipient(id_type=RecipientIdType.PHONE, id_value=recipient, country_code=country_code),
        content=MessageContent(body="Your appointment is confirmed for 10:30."), message_type=message_type,
        preferred_channel=ChannelPreference(channel))


def test_send_message_refusal_carries_the_conflict_and_how_the_jurisdiction_was_settled(consent):
    import core.send_message as SM
    from core.models import MessageType
    receipt = run(SM.handle_send_message(_send_request("+96891234567", MessageType.MARKETING, "US")))
    assert receipt.reason_code == "compliance_violation"
    assert receipt.result["rule"] == "jurisdiction_conflict"
    assert receipt.result["jurisdiction_source"] == "unknown"
    assert "contradicts" in receipt.result["jurisdiction_conflict"]


def test_send_message_success_says_when_the_number_overrode_the_country_code(monkeypatch):
    import core.send_message as SM
    from channels.adapter_interface import ChannelResponse
    from core.models import MessageType

    async def delivered(request):
        return ChannelResponse(success=True, provider_message_id="SM_TEST_1")

    monkeypatch.setattr(SM._SMS_ADAPTER, "send", delivered)
    receipt = run(SM.handle_send_message(_send_request("+96891234567", MessageType.TRANSACTIONAL, "US")))
    assert receipt.reason_code == "message_sent", receipt
    assert receipt.result["jurisdiction_source"] == "recipient_number"
    assert "OM was used" in receipt.result["jurisdiction_conflict"]


def test_send_message_success_adds_nothing_when_there_was_no_conflict(monkeypatch):
    import core.send_message as SM
    from channels.adapter_interface import ChannelResponse
    from core.models import MessageType

    async def delivered(request):
        return ChannelResponse(success=True, provider_message_id="SM_TEST_2")

    monkeypatch.setattr(SM._SMS_ADAPTER, "send", delivered)
    for cc in (None, "OM"):
        receipt = run(SM.handle_send_message(_send_request("+96891234567", MessageType.TRANSACTIONAL, cc)))
        assert receipt.reason_code == "message_sent"
        assert "jurisdiction_conflict" not in receipt.result and "jurisdiction_source" not in receipt.result


def test_the_errors_doc_does_not_promise_a_report_send_message_does_not_make():
    text = open(os.path.join(ROOT, "api", "errors.md"), encoding="utf-8").read()
    assert "send_message" in text.split("Which rule a compliance refusal names")[1].split("---")[0]


# ---------------------------------------------------------------------------
# P3: voice and email refusals outside a modeled statute do not read like local law
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("channel,number,country", [
    ("voice", "+96891234567", "OM"), ("voice", "+971501234567", "AE"), ("voice", "+66812345678", "TH"),
    ("email", "anna@example.om", "OM"), ("email", "anna@example.ae", "AE"), ("email", "anna@example.th", "TH"),
])
def test_a_voice_or_email_refusal_outside_a_modeled_statute_says_it_is_the_services_own_policy(
        consent, channel, number, country):
    r = check(number, channel=channel, country_code=country)
    assert r.result["legal"] is False
    assert r.result["rule"] in ("voice_marketing_consent", "email_marketing_consent")
    msg = r.result["human_message"]
    assert "not a citation of" in msg and "service's own policy" in msg, msg
    assert not US_RULES.search(msg)


def test_a_us_voice_refusal_does_not_claim_to_be_only_a_default(consent):
    r = check("+14045550100", channel="voice", country_code="US", state_code="GA")
    assert r.result["rule"] == "voice_marketing_consent"
    assert "not a citation of" not in r.result["human_message"]


def test_the_http_check_returns_the_structured_rule_basis_on_both_branches():
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app, raise_server_exceptions=False)
    refused = client.post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": "marketing",
        "content": "Big sale today!"}).json()
    allowed = client.post("/compliance/check", json={
        "recipient_id": "+96891234567", "channel": "sms", "message_type": "transactional",
        "content": "Your appointment is confirmed for 10:30."}).json()
    for body in (refused, allowed):
        assert body["rule_set"] == "OM"                               # the existing key keeps its shape
        basis = body["rule_basis"]
        assert basis["basis"] == "conservative_default" and basis["statutes_modeled"] == []
        assert "not a citation of OM law" in basis["note"]
    us = client.post("/compliance/check", json={
        "recipient_id": "+14045550200", "channel": "sms", "message_type": "marketing",
        "content": "Big sale today!", "country_code": "US"}).json()
    assert us["rule_basis"]["basis"] == "statute" and "TCPA" in us["rule_basis"]["statutes_modeled"]


# ---------------------------------------------------------------------------
# P3: country aliases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("number,alias,country", [
    ("+442071838750", "UK", "GB"), ("+442071838750", "gbr", "GB"), ("+14045550100", "USA", "US"),
    ("+14165550100", "can", "CA"), ("+4915112345678", "DEU", "DE"), ("+96891234567", "OMN", "OM"),
    ("+971501234567", "ARE", "AE"), ("+966512345678", "sau", "SA"), ("+33612345678", "FRA", "FR"),
])
def test_a_common_alias_is_read_as_the_country_it_names_and_is_not_a_conflict(number, alias, country):
    from compliance.number_jurisdiction import resolve_jurisdiction
    r = resolve_jurisdiction(number, alias)
    assert r.country == country and r.conflict is None and r.source == "caller", r


def test_a_usa_marketing_message_to_a_north_american_number_is_judged_as_american():
    r = check("+14045550100", country_code="USA")
    assert r.result["rule"] == "TCPA_marketing_consent" and r.result["jurisdiction"] == "US"


def test_every_country_in_the_tables_has_an_alpha_3_alias_that_leads_back_to_it():
    from compliance import number_jurisdiction as nj
    known = set(nj.CALLING_CODES.values()) | set(nj.NANP_COUNTRIES) | {"RU", "KZ"}
    alpha3 = {k: v for k, v in nj.COUNTRY_ALIASES.items() if len(k) == 3}
    assert all(re.fullmatch(r"[A-Z]{3}", k) for k in alpha3)
    assert set(alpha3.values()) == known, sorted(known - set(alpha3.values()))
    assert len(alpha3) == len(set(alpha3.values())), "two alpha-3 codes lead to one country, or the reverse"
    assert nj.COUNTRY_ALIASES["UK"] == "GB"
    assert all(v in known for v in nj.COUNTRY_ALIASES.values())
