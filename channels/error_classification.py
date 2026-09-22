"""Classifies a channel adapter's failure into ONE of a small, closed set of
sentences we wrote ourselves — never the adapter's own `error_message`, and
never a bare `str(exc)`.

WHY THIS EXISTS
===============
core.send_message and core.send_transactional_confirmation are both listed in
core.untrusted.NO_THIRD_PARTY_TEXT ("result carries only a provider message
id..." / "result carries only a provider message id"), which means
core.untrusted.label() never fences or neutralises ANY field either tool
returns — the claim on file is that nothing here needs fencing. Both broke
that claim the same way: on a delivery failure they interpolated a channel
adapter's own `ChannelResponse.error_message` (or a bare `str(exc)`) straight
into `human_message`.

That text is not ours. Every adapter's exception path sets `error_message`
from `str(exc)`:

  * channels/sms_email/twilio_sms.py    — `str(exc)` from the Twilio SDK,
    which itself formats its own message from Twilio's API error body.
  * channels/sms_email/resend_email.py,
    channels/sms_email/sendgrid_email.py — `str(exc)` from httpx, and
    `httpx.HTTPStatusError.__str__` includes the response body.
  * channels/voice_ai/vapi.py           — same httpx shape.
  * channels/whatsapp/cloud_api.py      — `err.get("message")` is Meta's own
    Graph API error text, truncated but not fenced; the exception path is
    `str(exc)[:200]`, the identical `stdout[:200]`-shaped slice-of-raw-output
    pattern flagged in compliance/jev_advisory.py.

Every one of those upstreams is a provider API that can be made to echo back
content it was sent (a malformed "To" number, an over-long body, a rejected
template) — the same shape that broke check_compliance's jev_advisory.note.
See core/check_compliance.py for that incident and its fix, and
tests/unit/test_check_compliance_jev_note_no_leak.py for the reproduction this
file's own tests mirror.

The fix is the same one applied there: classify into a closed vocabulary,
never interpolate. `error_code` is a value OUR adapters assign (not upstream
free text), so it drives the mapping; `error_message` / the exception text is
inspected ONLY to pick a closer-fitting sentence — never returned.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("smb_broker.channels.error_classification")

# Keyed by ChannelResponse.error_code — a value OUR adapters assign
# (twilio_sms.py, resend_email.py, sendgrid_email.py, cloud_api.py, vapi.py),
# not upstream free text. `whatsapp_unreachable` and the `whatsapp_{code}`
# prefix carry a variable suffix (an HTTP status or Meta's numeric error
# code) — always a number, never prose.
_CODE_REASONS: tuple[tuple[str, str], ...] = (
    ("not_configured",
     "this channel is not configured on this server"),
    ("invalid_recipient",
     "the recipient identifier is not valid for this channel"),
    ("whatsapp_token_expired",
     "the WhatsApp access token has expired or is invalid"),
    ("needs_template",
     "the recipient is outside the messaging window and needs an approved "
     "template"),
    ("whatsapp_unreachable",
     "the upstream channel could not be reached"),
    ("upstream_failure",
     "the upstream channel provider rejected or failed to process the "
     "message"),
)

# Substrings looked for ONLY to choose a closer-fitting sentence on the
# generic exception path — the matched text is never itself returned. Order
# matters: more specific reasons first.
_EXC_REASONS: tuple[tuple[str, str], ...] = (
    ("timeout", "the send timed out"),
    ("timed out", "the send timed out"),
    ("connect", "the upstream channel could not be reached"),
    ("connection", "the upstream channel could not be reached"),
    ("ssl", "a secure connection to the upstream channel could not be "
            "established"),
    ("dns", "the upstream channel's address could not be resolved"),
    ("resolve", "the upstream channel's address could not be resolved"),
    (" 401", "the channel's credentials were rejected"),
    (" 403", "the channel's credentials were rejected"),
    ("unauthorized", "the channel's credentials were rejected"),
    ("forbidden", "the channel's credentials were rejected"),
    (" 429", "the upstream channel is rate-limiting this account"),
    ("rate limit", "the upstream channel is rate-limiting this account"),
)

_DEFAULT_REASON = "the channel failed for an unspecified reason"
_DEFAULT_EXC_REASON = "the send failed with an unexpected local error"


def classify_channel_failure(
    channel_name: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    exc: BaseException | None = None,
) -> str:
    """Return ONE of our own sentences describing why a channel send failed.

    Never returns `error_message` or `str(exc)` — both are inspected (when
    present) only to choose a closer-fitting sentence from a fixed set. The
    raw values are LOGGED here (not returned), so the diagnostic is not lost —
    it is only kept off the wire, exactly like check_compliance's
    `_jev_unavailable_note`.
    """
    reason = _DEFAULT_REASON
    matched = False

    if error_code:
        low_code = error_code.lower()
        for needle, human in _CODE_REASONS:
            if low_code == needle or low_code.startswith(needle):
                reason = human
                matched = True
                break
        if not matched and low_code.startswith("whatsapp_"):
            # e.g. whatsapp_400, whatsapp_131047 — a status/error NUMBER Meta
            # assigned, never prose. Still an upstream-shaped failure.
            reason = "the upstream channel provider rejected or failed to " \
                      "process the message"
            matched = True

    if exc is not None:
        low_exc = f"{type(exc).__name__}: {exc}".lower()
        exc_matched = False
        for needle, human in _EXC_REASONS:
            if needle in low_exc:
                reason = human
                exc_matched = True
                break
        if not exc_matched and not matched:
            reason = _DEFAULT_EXC_REASON

    logger.warning(
        "channel_failure channel=%s error_code=%s error_message=%r "
        "exc_type=%s exc=%r classified_as=%r",
        channel_name, error_code, error_message,
        type(exc).__name__ if exc is not None else None,
        str(exc) if exc is not None else None,
        reason,
    )
    return reason
