"""A delivery tool that cannot deliver here must say so BEFORE the work starts - and before any charge.

THE FIELD CASE (audit 2026-09-30, fix 7): SMS cannot be sent from the live deployment (no carrier
account is configured; US business SMS also needs a registered 10DLC campaign), and five keyed
messaging attempts failed on exactly that. They failed LATE: after the credit hold, after the free-quota
decrement, inside a compliance gate whose first answer ("No registered 10DLC campaign") read as if the
caller's message were wrong. A key holder's first signal was a hold and a refund.

These tests pin: availability is read from the environment at call time; tools/list marks the channel
tools; tools/call answers `channel_unavailable` first thing, without touching billing; an unreadable
request keeps its precise validation error; a tool whose channel IS configured is untouched; and no
real call or message is ever placed (every adapter is replaced by a tripwire).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_interface.mcp_server import handle_mcp_request
from core import channel_status as cs

CHANNEL_ENV = [
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
    "TWILIO_MESSAGING_SERVICE_SID", "TWILIO_FROM_NUMBER", "RESEND_API_KEY", "SENDGRID_API_KEY",
    "VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID", "VAPI_OUTBOUND_VERIFIED",
    "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_ID",
    "ALLOW_STUB_CHANNELS",
]


@pytest.fixture(autouse=True)
def clean_channels(monkeypatch):
    """Production posture, nothing configured: stubs impossible, every channel down."""
    for n in CHANNEL_ENV:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    yield


def _run(coro):
    return asyncio.run(coro)


def _rpc(method, params=None, headers=None):
    return _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers or {"user-agent": "pytest"}))


def _tool(name, arguments):
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


SMS_ARGS = {"recipient": {"id_type": "phone", "id_value": "+15551230000"},
            "content": {"body": "hi"}, "message_type": "transactional"}
EMAIL_ARGS = {"recipient": {"id_type": "email", "id_value": "x@example.com"},
              "content": {"body": "hi", "subject": "s"}, "message_type": "transactional"}


class Tripwire:
    """Replaces billing + dispatch so a test can prove nothing past the gate ran."""

    def __init__(self, monkeypatch):
        import agent_interface.mcp_server as m
        self.hit = []

        async def _dispatch(name, args, headers=None, skip_auth=False):
            self.hit.append(name)
            return {"status": "success", "reason_code": "stub", "operation_id": "op"}
        monkeypatch.setattr(m, "_dispatch_and_label", _dispatch)


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

def test_nothing_configured_means_every_channel_is_down_with_a_reason():
    states = cs.all_channel_states()
    assert all(not s.available for s in states.values())
    assert all(s.reason for s in states.values())
    assert cs.unavailable_summary() == ["sms", "email", "voice", "whatsapp"]


def test_sms_needs_credentials_AND_a_sender(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    assert cs.channel_state("sms").available is False, "credentials without a sender cannot send"
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550001111")
    assert cs.channel_state("sms").available is True
    monkeypatch.delenv("TWILIO_AUTH_TOKEN")
    assert cs.channel_state("sms").available is False, "a sender without credentials cannot send"
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKx")
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "s")
    assert cs.channel_state("sms").available is True, "API-key auth mode counts"


def test_voice_needs_the_key_and_the_phone_number_and_a_verified_outbound_line(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    assert cs.channel_state("voice").available is False
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "pn")
    # Configured is not the same as able to call. The line this deployment holds is a free number from
    # the voice vendor, which the vendor documents as inbound-only and US-national, and no outbound call
    # has ever been placed on it (the account's call history is empty). Until an operator attests that an
    # outbound call has actually succeeded, the honest answer is "not enabled".
    s = cs.channel_state("voice")
    assert s.available is False
    assert "not been verified" in (s.reason or "")
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", "true")
    assert cs.channel_state("voice").available is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "nope", " "])
def test_only_an_explicit_yes_counts_as_verified(monkeypatch, value):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "pn")
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", value)
    assert cs.channel_state("voice").available is False, value


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " Yes "])
def test_explicit_yes_spellings_count_as_verified(monkeypatch, value):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "pn")
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", value)
    assert cs.channel_state("voice").available is True, value


def test_an_attestation_without_credentials_does_not_make_voice_available(monkeypatch):
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", "true")
    assert cs.channel_state("voice").available is False
    monkeypatch.setenv("VAPI_API_KEY", "k")
    assert cs.channel_state("voice").available is False, "a number is still needed"


def test_email_and_whatsapp(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    assert cs.channel_state("email").available
    monkeypatch.delenv("RESEND_API_KEY")
    monkeypatch.setenv("SENDGRID_API_KEY", "k")
    assert cs.channel_state("email").available
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    assert not cs.channel_state("whatsapp").available
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "1")
    assert cs.channel_state("whatsapp").available


def test_availability_is_read_at_call_time_not_import_time(monkeypatch):
    assert cs.channel_state("voice").available is False
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "p")
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", "true")
    assert cs.channel_state("voice").available is True
    monkeypatch.delenv("VAPI_API_KEY")
    assert cs.channel_state("voice").available is False


def test_stub_channels_count_as_available_only_outside_production(monkeypatch):
    monkeypatch.setenv("ALLOW_STUB_CHANNELS", "1")
    assert cs.channel_state("sms").available is False, "production can never use stubs"
    monkeypatch.setenv("ENVIRONMENT", "development")
    s = cs.channel_state("sms")
    assert s.available and s.simulated


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

def _tools():
    return {t["name"]: t for t in _rpc("tools/list")["result"]["tools"]}


def test_tools_list_marks_the_channel_tools_that_cannot_deliver():
    tools = _tools()
    cb = tools["call_business"]
    assert cb["description"].startswith("[UNAVAILABLE on this deployment:")
    assert cb["_meta"]["hatchloop/availability"]["available"] is False
    assert cb["_meta"]["hatchloop/availability"]["unavailable_channels"] == ["voice"]
    sm = tools["send_message"]
    assert sm["_meta"]["hatchloop/availability"]["available"] is False   # nothing configured at all
    stc = tools["send_transactional_confirmation"]
    assert stc["_meta"]["hatchloop/availability"]["available"] is False


def test_tools_list_leaves_every_other_tool_untouched():
    """No availability notice on a tool that depends on no delivery channel. (Since 2026-10-03 a tool
    may carry a STATIC readiness label - find_business is beta - but never a channel notice, and the
    production-ready ones carry nothing at all.)"""
    tools = _tools()
    for name in ("find_business", "screen_sanctions", "check_quota", "preview_cost", "get_status"):
        assert "hatchloop/availability" not in tools[name].get("_meta", {}), name
        assert "UNAVAILABLE" not in tools[name]["description"]
    for name in ("screen_sanctions", "check_quota", "preview_cost", "get_status"):
        assert "_meta" not in tools[name], f"{name} is production-ready and must carry no label"


def test_a_partly_available_tool_stays_available_and_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "1")
    sm = _tools()["send_message"]
    note = sm["_meta"]["hatchloop/availability"]
    assert note["available"] is True and set(note["unavailable_channels"]) == {"sms", "voice"}
    assert "Not available on this deployment" in sm["description"]
    assert not sm["description"].startswith("[UNAVAILABLE")


def test_when_everything_is_provisioned_nothing_is_annotated(monkeypatch):
    for n, v in {"TWILIO_ACCOUNT_SID": "a", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM_NUMBER": "+1",
                 "RESEND_API_KEY": "k", "VAPI_API_KEY": "k", "VAPI_PHONE_NUMBER_ID": "p",
                 "VAPI_OUTBOUND_VERIFIED": "true",
                 "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_ID": "1"}.items():
        monkeypatch.setenv(n, v)
    tools = _tools()
    assert not any("_meta" in tools[n] for n in cs.CHANNEL_TOOLS)
    assert cs.instructions_notice() == ""


def test_the_cached_manifest_is_never_mutated_by_the_overlay():
    from agent_interface.mcp_server import _build_tool_list
    before = json.dumps(_build_tool_list(), sort_keys=True)
    _tools()
    assert json.dumps(_build_tool_list(), sort_keys=True) == before
    assert "UNAVAILABLE" not in before


def test_initialize_announces_the_down_channels_on_the_full_server_and_on_the_messaging_door():
    full = _rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
    assert "not available on this deployment right now: sms, email, voice, whatsapp" in full.lower() \
        or "sms, email, voice, whatsapp" in full
    assert "channel_unavailable" in full
    door = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        headers={"user-agent": "x"}, profile="sms-whatsapp-messaging"))["result"]["instructions"]
    assert "channel_unavailable" in door
    sanctions = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        headers={"user-agent": "x"}, profile="sanctions-screening"))["result"]["instructions"]
    assert "channel_unavailable" not in sanctions, "the sanctions door has no business announcing SMS"


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

def test_send_message_over_sms_fails_immediately_and_nothing_is_dispatched(monkeypatch):
    trip = Tripwire(monkeypatch)
    resp = _tool("send_message", SMS_ARGS)
    assert resp["result"]["isError"] is True
    body = _body(resp)
    assert body["error_code"] == "channel_unavailable"
    assert body["retriable"] is False
    assert "Nothing was sent, held or charged" in body["human_message"]
    assert "SMS is not enabled" in body["human_message"]
    assert trip.hit == [], "the gate must fire before the dispatcher, the credit hold and the compliance gate"


def test_it_fires_before_the_credit_hold(monkeypatch):
    """The audit's complaint: the first signal was a hold and a refund. No hold may be placed."""
    monkeypatch.setenv("CREDITS_ENABLED", "true")
    from billing import credits
    placed = []

    async def _hold(*a, **k):
        placed.append(a)
        raise AssertionError("a credit hold was attempted for a call that cannot deliver")
    monkeypatch.setattr(credits, "run_metered_tool", _hold, raising=False)
    monkeypatch.setattr(credits, "resolve_account", lambda h: "sub_paid_account", raising=False)
    resp = _tool("send_message", SMS_ARGS)
    assert _body(resp)["error_code"] == "channel_unavailable"
    assert placed == []


def test_it_does_not_consume_the_free_daily_quota(monkeypatch):
    from agent_interface import key_request_logic as krl
    from agent_interface.identity import issue_token, TokenRequest
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    key = issue_token(TokenRequest(agent_id="free_quota_probe", principal_id="p")).token
    before = krl.get_free_daily_remaining("free_quota_probe")
    resp = _run(handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "send_message", "arguments": SMS_ARGS}},
        headers={"x-agent-identity": key, "user-agent": "pytest"}))
    assert _body(resp)["error_code"] == "channel_unavailable"
    assert krl.get_free_daily_remaining("free_quota_probe") == before, (
        "a call that could never deliver consumed one of the caller's 100 daily operations")


def test_an_anonymous_caller_is_told_the_channel_is_down_before_being_sent_off_to_get_a_key(monkeypatch):
    import config
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    assert _body(_tool("send_message", SMS_ARGS))["error_code"] == "channel_unavailable"


def test_email_recipient_goes_through_when_email_is_configured_even_though_sms_is_not(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    trip = Tripwire(monkeypatch)
    resp = _tool("send_message", EMAIL_ARGS)
    assert trip.hit == ["send_message"], resp
    assert resp["result"]["isError"] is False


def test_a_phone_recipient_is_pointed_at_whatsapp_when_that_is_up(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "1")
    Tripwire(monkeypatch)
    # auto + phone => [sms] only => unavailable, and WhatsApp is the named way out
    body = _body(_tool("send_message", SMS_ARGS))
    assert body["error_code"] == "channel_unavailable"
    assert body["how_to_resolve"]["working_alternatives"] == ["whatsapp"]
    # and choosing it goes through
    trip = Tripwire(monkeypatch)
    args = dict(SMS_ARGS, preferred_channel="whatsapp")
    assert _tool("send_message", args)["result"]["isError"] is False
    assert trip.hit == ["send_message"]


def test_transactional_confirmation_follows_the_recipient_kind(monkeypatch):
    args_sms = {"recipient": {"phone_or_email": "+15551230000"}, "confirmation_type": "booking_confirmation",
                "data": {"business_name": "B", "date": "2026-10-02", "time": "10:00"}}
    args_mail = {"recipient": {"phone_or_email": "a@example.com"}, "confirmation_type": "booking_confirmation",
                 "data": {"business_name": "B", "date": "2026-10-02", "time": "10:00"}}
    Tripwire(monkeypatch)
    b = _body(_tool("send_transactional_confirmation", args_sms))
    assert b["error_code"] == "channel_unavailable"
    monkeypatch.setenv("RESEND_API_KEY", "k")
    b = _body(_tool("send_transactional_confirmation", args_sms))
    assert b["error_code"] == "channel_unavailable"
    assert b["how_to_resolve"]["working_alternatives"] == ["email"]
    trip = Tripwire(monkeypatch)
    assert _tool("send_transactional_confirmation", args_mail)["result"]["isError"] is False
    assert trip.hit == ["send_transactional_confirmation"]


def test_call_business_fails_early_without_voice_and_places_no_call(monkeypatch):
    trip = Tripwire(monkeypatch)
    from channels.voice_ai import vapi
    monkeypatch.setattr(vapi.VapiVoiceAdapter, "send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a call was placed")))
    resp = _tool("call_business", {"business_phone": "+15551230000", "objective": "ask the hours"})
    body = _body(resp)
    assert body["error_code"] == "channel_unavailable"
    assert "Voice calling is not provisioned" in body["human_message"]
    assert trip.hit == []


def test_call_business_with_credentials_but_an_unverified_line_fails_early_and_places_no_call(monkeypatch):
    """The state production is in on 2026-10-01: Vapi key and phone-number id present, the number a free
    vendor line (inbound-only per the vendor), no outbound call ever verified."""
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "p")
    trip = Tripwire(monkeypatch)
    from channels.voice_ai import vapi
    monkeypatch.setattr(vapi.VapiVoiceAdapter, "send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a call was placed")))
    body = _body(_tool("call_business", {"business_phone": "+15551230000", "objective": "ask the hours"}))
    assert body["error_code"] == "channel_unavailable"
    assert "not been verified" in body["human_message"]
    assert trip.hit == [], "the dispatcher (and so the credit hold) must not be reached"
    listed = _tools()["call_business"]
    assert listed["description"].startswith("[UNAVAILABLE"), listed["description"][:80]
    assert listed["_meta"]["hatchloop/availability"]["available"] is False
    assert "voice" in cs.unavailable_summary()


def test_the_core_call_business_also_refuses_an_unverified_line_without_charging(monkeypatch):
    """The REST twin (/ops/call_business) and any in-process caller reach core.call_business directly,
    not through the MCP gate; they must get the same honest answer and no call."""
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "p")
    from channels.voice_ai import vapi
    monkeypatch.setattr(vapi.VapiVoiceAdapter, "send",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a call was placed")))
    from core.call_business import handle_call_business
    from core.models import CallBusinessRequest
    receipt = _run(handle_call_business(CallBusinessRequest(business_phone="+15551230000", objective="ask the hours"),
                                 agent_id="a", trace_id="t"))
    assert receipt.reason_code == "channel_unavailable"
    assert receipt.cost.amount == 0.0 and "not been verified" in receipt.human_message


def test_call_business_reaches_the_dispatcher_when_voice_is_provisioned(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    monkeypatch.setenv("VAPI_PHONE_NUMBER_ID", "p")
    monkeypatch.setenv("VAPI_OUTBOUND_VERIFIED", "true")
    trip = Tripwire(monkeypatch)
    resp = _tool("call_business", {"business_phone": "+15551230000", "objective": "ask the hours"})
    assert trip.hit == ["call_business"] and resp["result"]["isError"] is False


def test_a_malformed_request_keeps_its_precise_validation_error(monkeypatch):
    """The gate acts only on a request the dispatcher would also accept."""
    Tripwire(monkeypatch)
    for args in (
        {"recipient_type": "nonsense", "recipient_id": "x", "content": {"body": "hi"}},
        {"recipient": "not-an-object"},
        {},
        {"recipient": {"id_type": "phone", "id_value": "+1"}, "content": {"body": "hi"},
         "preferred_channel": "carrier-pigeon"},
        {"recipient": {"id_type": "phone", "id_value": "+1"}, "message_type": "marketing_blast"},
    ):
        resp = _tool("send_message", args)
        assert "channel_unavailable" not in json.dumps(resp), args


def test_other_tools_are_never_gated():
    for name, args in (("find_business", {"vertical": "plumbing", "location": {"zip_or_city": "Atlanta"}}),
                       ("preview_cost", {"operation": "send_message", "params": {}}),
                       ("check_quota", {})):
        assert "channel_unavailable" not in json.dumps(_tool(name, args))


def test_the_outcome_log_records_the_refusal_as_a_tool_error(monkeypatch):
    from billing import usage_logger as ul
    seen = []
    monkeypatch.setattr(ul, "fire_log_outcome", lambda e: seen.append(e))
    _tool("send_message", SMS_ARGS)
    assert (seen[-1].outcome, seen[-1].error_code) == ("tool_error", "channel_unavailable")


def test_no_real_provider_is_ever_called_by_these_tests():
    """Belt and braces for the whole file: httpx must not be reachable from the gate."""
    import httpx
    real = httpx.AsyncClient.post

    async def tripwire(self, *a, **k):
        raise AssertionError("a network POST was attempted")
    httpx.AsyncClient.post = tripwire
    try:
        assert _body(_tool("send_message", SMS_ARGS))["error_code"] == "channel_unavailable"
    finally:
        httpx.AsyncClient.post = real
