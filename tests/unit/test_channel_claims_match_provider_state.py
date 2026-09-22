"""
send_message and call_business must not claim a channel works when its
provider is unconfigured on this deployment.

Context (2026-09-22): `GET /healthz/external` on the live server reports
twilio: fail "not_configured" and vapi: fail "not_configured" (calcom and
resend are ok). Yet send_message's published tools/list description said
"WhatsApp ... SMS, email, or voice" with no disclosure that two of those
four channels do not work here, and call_business's description did not
disclose that its ENTIRE purpose (a Vapi call) cannot execute on this
deployment at all. Both failures are already honest at the CODE level --
channels/stub_policy.not_configured() and core/call_business.py's own
env-var guard both refuse cleanly, charge nothing, and never fabricate a
stub success outside ALLOW_STUB_CHANNELS -- the defect was that the tool's
own advertised description said nothing about it.

This file proves two things are true TOGETHER, not separately:
  1. With the provider's credentials absent, the real adapter/handler code
     really does refuse honestly (never a live network call: both adapters
     check `os.getenv(...)` and return before touching httpx/twilio at all
     when unconfigured -- see channels/stub_policy.py and
     core/call_business.py).
  2. The tool's raw manifest description says so, in the first quarter of
     the text (`_EARLY_CUTOFF` below) -- not buried after a truncatable
     tail -- reusing the exact phrase registered in
     tests/unit/test_truncation_protects_disclosures.py's
     PROTECTED_DISCLOSURES so the two tests cannot drift apart silently.

`test_early_disclosure_check_is_not_vacuous` embeds this repo's OWN
pre-fix wording (2026-09-22, before this file existed) and proves the same
assertion turns red against it -- so a green result above is a test that
can fail, not one that happens to pass.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_truncation_protects_disclosures import (  # noqa: E402
    PROTECTED_DISCLOSURES,
)

from channels.adapter_interface import ChannelRequest  # noqa: E402
from channels.sms_email.twilio_sms import TwilioSMSAdapter  # noqa: E402
from channels.voice_ai.vapi import VapiVoiceAdapter  # noqa: E402

with open(os.path.join(ROOT, "manifest", "manifest.json"), encoding="utf-8") as fh:
    MANIFEST = json.load(fh)
OPS = {o["name"]: o for o in MANIFEST.get("operations", [])}

# The description must disclose an unconfigured channel before this many
# characters -- comfortably inside "the first sentence or two", never in a
# tail that this surface's own truncation (_MAX_DESC_CHARS=450) is free to
# eat first. Both registered phrases below currently start at 89 and 81.
_EARLY_CUTOFF = 250


def _req(channel: str, recipient="+96894639405"):
    return ChannelRequest(
        recipient_id=recipient, channel=channel, message_type="transactional",
        content="Test content — not actually sent (no credentials).",
        country_code="OM",
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The code really does refuse honestly with no credentials -- no network
#    call is possible: both adapters check os.getenv(...) and return before
#    ever importing/calling the provider SDK or httpx.
# ---------------------------------------------------------------------------

class TestSMSChannelFailsHonestlyWhenTwilioUnconfigured:
    def test_no_credentials_refuses_without_a_network_call(self, monkeypatch):
        for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                    "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
                    "TWILIO_MESSAGING_SERVICE_SID", "TWILIO_FROM_NUMBER",
                    "ALLOW_STUB_CHANNELS"):
            monkeypatch.delenv(var, raising=False)
        adapter = TwilioSMSAdapter()
        assert adapter._auth_mode() == "none"
        resp = _run(adapter.send(_req("sms")))
        assert resp.success is False
        assert resp.error_code == "channel_not_configured"
        # Never a fabricated delivery id.
        assert not (resp.provider_message_id or "").startswith("SM_STUB_")


class TestVoiceChannelFailsHonestlyWhenVapiUnconfigured:
    def test_no_credentials_refuses_without_a_network_call(self, monkeypatch):
        monkeypatch.delenv("VAPI_API_KEY", raising=False)
        monkeypatch.delenv("ALLOW_STUB_CHANNELS", raising=False)
        adapter = VapiVoiceAdapter()
        resp = _run(adapter.send(_req("voice")))
        assert resp.success is False
        assert resp.error_code == "channel_not_configured"
        assert not (resp.provider_message_id or "").startswith("VAPI_STUB_")


class TestCallBusinessFailsHonestlyWhenVapiUnconfigured:
    def test_no_credentials_refuses_before_dialling_and_is_uncharged(self, monkeypatch):
        monkeypatch.delenv("VAPI_API_KEY", raising=False)
        monkeypatch.delenv("VAPI_PHONE_NUMBER_ID", raising=False)
        from core.call_business import handle_call_business
        from core.models import CallBusinessRequest, OperationStatus

        req = CallBusinessRequest(
            business_phone="+14045550123",
            objective="Ask if they can come Tuesday.",
        )
        receipt = _run(handle_call_business(req))
        assert receipt.status == OperationStatus.FAILURE
        assert receipt.reason_code == "voice_not_provisioned"
        assert receipt.cost.amount == 0.0


# ---------------------------------------------------------------------------
# 2. The tool's own manifest description discloses this, EARLY.
# ---------------------------------------------------------------------------

def _assert_early_disclosure(name: str, raw: str) -> None:
    phrase = PROTECTED_DISCLOSURES[name]
    start = raw.find(phrase)
    assert start != -1, (
        f"{name}: the honest-failure disclosure registered in "
        f"PROTECTED_DISCLOSURES is not present verbatim in this tool's "
        f"raw manifest description:\n  expected: {phrase!r}\n  actual: {raw!r}"
    )
    assert start < _EARLY_CUTOFF, (
        f"{name}: the disclosure exists but starts at character {start}, "
        f"past the {_EARLY_CUTOFF}-char early-disclosure cutoff this test "
        f"enforces. A disclosure this deep risks landing inside or after "
        f"_format_description_for_llm's end-truncation the moment an "
        f"unrelated edit lengthens the sentences ahead of it -- move it "
        f"earlier, right after the opening clause."
    )


def test_send_message_discloses_sms_and_voice_are_unwired_early():
    _assert_early_disclosure("send_message", OPS["send_message"]["description"])


def test_call_business_discloses_it_is_unwired_early():
    _assert_early_disclosure("call_business", OPS["call_business"]["description"])


# ---------------------------------------------------------------------------
# 3. Proven red: the same check, against this repo's actual PRE-FIX wording.
# ---------------------------------------------------------------------------

# Verbatim manifest/manifest.json text for these two tools before this file
# and its PROTECTED_DISCLOSURES entries existed (2026-09-22). Kept here, not
# re-derived, so this stays a fixed historical fixture even if the current
# description is edited again later.
_PRE_FIX_SEND_MESSAGE_DESCRIPTION = (
    "Send a message on behalf of an agent's user or an SMB across WhatsApp "
    "(free during launch), SMS, email, or voice, sent immediately with no "
    "scheduling. Every send routes through a non-bypassable gate (TCPA, "
    "GDPR, CASL, PDPL across 26 jurisdictions): marketing without recorded "
    "consent is rejected at runtime with a structured compliance_violation "
    "receipt."
)
_PRE_FIX_CALL_BUSINESS_DESCRIPTION = (
    "Place a conversational voice-AI phone call to a business on a "
    "consumer's behalf; give a plain-language objective and it navigates "
    "the call, extracting the answer. Business-directed (B2B), far less "
    "restricted than calling consumers — but the compliance gate "
    "still enforces recording consent per jurisdiction."
)


def test_early_disclosure_check_is_not_vacuous():
    """`_assert_early_disclosure` must actually be able to fail.

    Run it against the exact wording this repo shipped before the fix --
    which never mentioned that SMS/voice/call_business fail here at all --
    and confirm it raises. A check that cannot fail proves nothing about
    the passing case above.
    """
    import pytest

    with pytest.raises(AssertionError):
        _assert_early_disclosure("send_message", _PRE_FIX_SEND_MESSAGE_DESCRIPTION)
    with pytest.raises(AssertionError):
        _assert_early_disclosure("call_business", _PRE_FIX_CALL_BUSINESS_DESCRIPTION)
