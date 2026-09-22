"""
check_compliance — free, read-only pre-flight for the outbound-messaging
compliance gate.

The problem it solves for a calling agent:
    send_message ($0.02-0.22) and call_business ($0.50) both run every outbound
    through a non-bypassable compliance gate (TCPA / GDPR / CASL / CAN-SPAM /
    10DLC across 26 jurisdictions). If the gate rejects, the agent has already
    committed to the paid call and burned a turn discovering the send was never
    legal. Before spending, an agent wants to know CHEAPLY and INSTANTLY whether
    a (recipient, channel, message_type, content) combination would pass.

This tool answers exactly that by running the SAME `compliance.pre_check`
gate the paid path runs — in preview mode, so NO message is sent and NO audit
event is recorded. It is the free/cheap top-of-funnel read that de-risks the
paid send_message / call_business path, mirroring how check_booking_link
de-risks import_booking_url -> schedule_appointment:

    check_compliance(recipient_id, channel, message_type, content)  # <- $0, instant
      -> send_message(...)          # the paid action, gate runs again at send time

Design notes / honesty guarantees:
  * Single source of truth: the legal/illegal verdict comes from the identical
    `pre_check()` function the real dispatch uses — never a re-implementation
    that could drift from the enforced gate. A `true` here means the gate will
    let the send through (barring live state changes like a fresh opt-out
    between preview and send).
  * Side-effect free: preview=True suppresses every audit-log write. A read-only
    tool must not record an OUTBOUND_DISPATCHED "allow" for a send that never
    happened — that would be the same class of lie as the old stub-SUCCESS bug.
  * A "not compliant" answer is a SUCCESSFUL check (the tool did its job and
    told you the truth), not a failure — same convention as check_booking_link
    returning supported=false. Only malformed input is a FAILURE(bad_input).
  * Scope honesty: this checks the OUTBOUND-MESSAGING gate (content, opt-out,
    marketing consent, 10DLC). It does NOT evaluate two-party voice *recording*
    consent — that is decided at call time inside the voice adapter. The result
    says so explicitly so an agent never over-trusts a voice "legal": true.
  * Evidence: every decision (permitted or blocked) carries a
    `compliance_receipt` — a self-contained, hash-bound, optionally Ed25519-
    signed record of WHICH gate code decided, under WHICH jurisdiction rules,
    over WHICH inputs, WHEN, and WHAT it returned. See core/compliance_receipt.
    The operator, not us, is the one who has to produce that record later, so
    it is handed to them and stored nowhere. It is purely additive: a caller
    who ignores the field sees the identical answer it saw before.
"""
from __future__ import annotations

import time
import uuid

from core.compliance_receipt import (
    attach_receipt,
    service_version,
    sha256_text as _sha256_text,
    source_fingerprint,
)
from core.models import (
    ComplianceViolationError,
    CostRecord,
    OperationStatus,
    OutcomeReceipt,
)
from compliance.jev_advisory import get_restricted_category_advisory

_VALID_CHANNELS = ("sms", "email", "voice")

# Human remediation copy, keyed by the rule identifier pre_check raises. Mirrors
# main._remediation_for (the public HTTP /compliance/check surface). Kept local
# so this read-only tool layer does not import the web app; the rule names are
# stable contract identifiers, so drift risk is low.
_REMEDIATION = {
    "restricted_content": "Reword the message to remove the restricted category, or seek explicit licensing for the regulated content.",
    "recipient_opted_out": "Honor the opt-out — the recipient has unsubscribed. Do not send. Add to suppression list.",
    "TCPA_marketing_consent": "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers, and pass its consent_record_id.",
    "GDPR_marketing_consent": "Obtain GDPR Article 6/7 consent before marketing email to EU/UK residents.",
    "CASL_marketing_consent": "Obtain explicit CASL consent before commercial electronic messages to Canadian recipients.",
    "10DLC_campaign_not_registered": "Register a 10DLC campaign with The Campaign Registry (TCR) before sending US A2P SMS. Required by US carriers since 2023.",
    "restricted_content_jev_advisory": (
        "The deterministic gate found nothing, but the additional jev "
        "restricted-category read flagged this content (jev catches "
        "Arabic/Cyrillic/obfuscated restricted-category phrasing the regex "
        "classifier measurably misses). Re-word to remove any restricted "
        "category, in any language or script, then re-run check_compliance. "
        "This signal is advisory, not authoritative — see result.jev_advisory."
    ),
}


def _remediation_for(rule: str) -> str:
    return _REMEDIATION.get(
        rule, "Review the cited rule in the jurisdiction reference at /compliance/jurisdictions."
    )


# ---------------------------------------------------------------------------
# jev-unavailable note: a CLOSED vocabulary, never jev's own free text.
# ---------------------------------------------------------------------------
# check_compliance is listed in core.untrusted.NO_THIRD_PARTY_TEXT ("result is
# our own rule ids and remediation text"), which means core.untrusted.label()
# never fences or neutralises ANY field of this tool's result - the claim is
# that there is nothing here THAT NEEDS fencing. `JevAdvisory.error` breaks
# that claim if interpolated directly: on several of its own failure paths
# (compliance/jev_advisory.py) it is built from jev's raw stdout/stderr -
# `f"exit {code}: {stderr}"`, `f"empty stdout (exit {code}): {stderr}"`,
# `f"unparseable JSON: {exc}: {stdout[:200]}"` - and jev's own error formatter
# builds ITS text from the upstream HTTP response body, which a validation
# API can construct by echoing back the very content we sent it. That makes
# `advisory.error` third-party text in exactly the sense core/untrusted.py
# exists to fence, arriving at a field this tool never fences. Reproduced
# with a mocked subprocess.run (zero real jev calls) and pinned by
# tests/unit/test_check_compliance_jev_note_no_leak.py.
#
# The fix is not to fence it (check_compliance's result shape is a flat
# OutcomeReceipt dict, not one of the receipts core/untrusted.py already
# walks, and jev's raw diagnostic has no value to a calling agent anyway -
# it is Chinese-language CLI/HTTP plumbing text, not something an operator
# acts on). Instead: classify it into ONE of a small set of sentences we
# wrote ourselves, and never let any byte of `error` reach the result.
_JEV_UNAVAILABLE_REASONS: tuple[tuple[str, str], ...] = (
    ("empty content", "no content was supplied to check"),
    ("test runner", "disabled while running under the test suite"),
    # Board row 282: this is the ONE bucket that means jev is structurally
    # missing in THIS deployment (no JEV_BIN, not on PATH, no dev fallback
    # script — see compliance/jev_advisory.py's resolve_jev_binary) rather
    # than a one-off failed call. Deliberately worded differently from every
    # other bucket below and placed before the generic "timeout"/"exit "
    # matches so it can never be swallowed by them — this is the distinction
    # that used to be invisible: "jev never even ran here" now reads
    # differently from "jev ran and the call failed".
    ("binary not found", "jev is not installed/configured on this host (set JEV_BIN or put `jev` on PATH)"),
    ("timeout", "the read timed out"),
    ("empty stdout", "the read returned no answer"),
    ("unparseable json", "the read returned an answer we could not parse"),
    ("exit ", "the read failed"),
)


def _jev_unavailable_note(error: str | None) -> str:
    """Map jev's free-text failure diagnostic to one of a small, fixed set of
    sentences we wrote. `error` may itself carry text jev's own upstream API
    echoed back from the content we sent it, so it must never be
    interpolated into a tool result verbatim - this always returns a string
    that came from this file, not from jev."""
    low = (error or "").lower()
    reason = "the read failed for an unrecognised reason"
    for needle, human in _JEV_UNAVAILABLE_REASONS:
        if needle in low:
            reason = human
            break
    return (
        "jev unavailable this call; deterministic verdict unchanged "
        f"({reason})."
    )


def _infer_channel(recipient_id: str) -> str:
    """Best-effort channel from the recipient identifier, mirroring send_message
    (an email address is reachable by email; anything else defaults to sms).
    Voice must be requested explicitly."""
    return "email" if "@" in recipient_id else "sms"


# ---------------------------------------------------------------------------
# Evidence record (see core/compliance_receipt.py)
# ---------------------------------------------------------------------------

# WHAT THIS RECEIPT REFUSES TO CLAIM, carried inside the record itself.
#
# A limit that lives in our documentation is a limit the auditor reading the
# customer's evidence file will never see. Every one of these is a claim a
# reader could otherwise reasonably infer from "AgentBroker checked this and
# said it was legal", and not one of them is ours to make.
_DOES_NOT_ASSERT = [
    "This is a PREVIEW decision. It does not assert that any message was sent, "
    "nor that the send was still permitted when it happened - the gate re-runs "
    "at dispatch, and an opt-out or consent change between the two produces a "
    "different answer.",
    "It does not assert where the recipient actually is. The jurisdiction "
    "recorded here was supplied by the caller or defaulted when none was "
    "given; we did not verify the recipient's location.",
    "It does not assert that the recipient identifier belongs to the person "
    "the caller believes it belongs to.",
    "It does not cover two-party voice RECORDING consent, which is decided at "
    "call time inside the voice adapter and is outside this gate.",
    "It does not assert compliance with any obligation this gate does not "
    "implement, and it is not legal advice or a determination by any regulator.",
    "When result.rule is 'restricted_content_jev_advisory', the block came "
    "from an ADDITIONAL, best-effort jev read layered on top of the "
    "deterministic gate in THIS preview tool only - it is not part of the "
    "gate compliance.pre_check/send_message enforce, and it is not stable "
    "near its own decision threshold (a byte-identical rerun can disagree "
    "roughly 1 time in 8). It can only ever ADD a caution here, never "
    "remove one.",
]

_ASSERTS = (
    "AgentBroker ran its outbound-messaging compliance gate "
    "(compliance.pre_check - the same code path send_message and call_business "
    "run before dispatching) in preview mode, over the inputs whose digest is "
    "recorded here, at the instant recorded here, and returned the decision "
    "recorded here. No message was sent and no state changed."
)


def _ruleset_evidence(country_code, state_code) -> dict:
    """Which rules decided, identified by content rather than by a label.

    THERE IS NO RULESET VERSION NUMBER TO QUOTE, so this does not invent one.
    A hand-maintained version constant is only correct until the first person
    who edits the rules forgets to bump it, and the whole value of this field
    to an auditor is that it cannot be wrong. Source fingerprints cannot drift
    from the code that ran: identical fingerprints mean identical decision
    logic. They are conservative in the safe direction - a comment edit changes
    them, so they can over-report a change and never under-report one.

    `rules_applied` is the concrete part: the actual jurisdiction rule values
    that governed THIS decision, as data, so a reader does not need our source
    to see what was enforced.
    """
    import dataclasses

    import compliance.jurisdiction_rules as _jr
    import compliance.pre_check as _pc

    rules = _jr.infer_jurisdiction(country_code, state_code)
    return {
        "gate": "compliance.pre_check (preview mode: decision only, no send, "
                "no audit-log write)",
        "gate_source_sha256": source_fingerprint(_pc),
        "jurisdiction_rules_source_sha256": source_fingerprint(_jr),
        "jurisdiction_applied": rules.jurisdiction_code,
        # An unknown jurisdiction is NOT the same fact as a stated one, and the
        # gate treats them differently (a marketing send with no country_code
        # is refused outright). The receipt has to record which of the two
        # produced this decision.
        "jurisdiction_supplied_by_caller": bool(country_code),
        "supported_jurisdictions": len(_jr.list_supported_jurisdictions()),
        "rules_applied": dataclasses.asdict(rules),
    }


def _attach(result: dict, operation_id: str, subject: dict, inputs: dict,
            country_code, state_code, channel: str, decision: dict) -> None:
    """Put the evidence record into `result`. Never raises, never charges.

    Called on both decision branches and on NEITHER failure branch: a
    bad_input receipt would be a record of a check that never ran, and an
    evidence artefact whose subject is "nothing happened" is noise in the file
    an auditor has to read.
    """
    attach_receipt(
        result,
        tool="check_compliance",
        operation_id=operation_id,
        service_version=service_version(),
        asserts=_ASSERTS,
        does_not_assert=_DOES_NOT_ASSERT,
        subject=subject,
        inputs=inputs,
        evidence={
            "mode": "preview",
            "decision": decision,
            "ruleset": _ruleset_evidence(country_code, state_code),
            "content_digest_note": (
                "subject.content_sha256 is sha256 over the raw UTF-8 bytes of "
                "the message body. The body itself is not reproduced here; "
                "hash the copy you kept to prove it is the text that was "
                "checked."),
            "scope": (
                "Outbound-messaging gate only: restricted content, opt-out, "
                "marketing consent, quiet hours and 10DLC campaign "
                "registration. Two-party voice RECORDING consent is decided "
                "at call time in the voice adapter and is NOT covered."
                if channel == "voice" else
                "Outbound-messaging gate only: restricted content, opt-out, "
                "marketing consent, quiet hours and 10DLC campaign "
                "registration."),
        },
    )


async def handle_check_compliance(
    recipient_id: str,
    content: str,
    channel: str | None = None,
    message_type: str = "transactional",
    country_code: str | None = None,
    state_code: str | None = None,
    agent_id: str | None = None,
    trace_id: str | None = None,
) -> OutcomeReceipt:
    t0 = time.monotonic()
    # ONE id for the call, so the evidence receipt and the OutcomeReceipt name
    # the same operation. Two uuid4()s in the same call would have produced a
    # record the caller could not tie back to the answer it came with.
    operation_id = str(uuid.uuid4())

    def _bad_input(msg: str) -> OutcomeReceipt:
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.FAILURE,
            reason_code="bad_input",
            human_message=msg,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
        )

    # --- input validation -------------------------------------------------
    if not recipient_id or not isinstance(recipient_id, str) or not recipient_id.strip():
        return _bad_input(
            "recipient_id is required — the phone number (E.164, e.g. '+14045550100') "
            "or email address the message would go to."
        )
    if not content or not isinstance(content, str) or not content.strip():
        return _bad_input(
            "content is required — the message body you intend to send. The gate "
            "classifies the actual text, so a preview needs the real content."
        )

    recipient_id = recipient_id.strip()
    channel = (channel or _infer_channel(recipient_id)).strip().lower()
    if channel not in _VALID_CHANNELS:
        return _bad_input(
            f"channel must be one of {list(_VALID_CHANNELS)} (got '{channel}'). "
            "Omit it to auto-infer sms/email from the recipient_id."
        )

    # --- run the identical gate, in preview mode (no send, no audit write) --
    from compliance.pre_check import pre_check

    base_result = {
        "channel": channel,
        "message_type": message_type,
        "jurisdiction": (
            f"{country_code}-{state_code}" if state_code else (country_code or "US")
        ),
        "recording_consent_note": (
            "This is the outbound-messaging gate only. Two-party voice recording "
            "consent is evaluated separately at call time."
            if channel == "voice" else None
        ),
        "checked_live": False,
    }

    # The subject and inputs the evidence record will name. `content` is
    # DIGESTED, NOT COPIED: the caller already holds the message body, the
    # receipt may be filed and forwarded, and a record that reproduces the
    # message text turns an evidence artefact into a second copy of the
    # customer's data. The digest still lets them prove which exact text was
    # checked, which is the only thing the evidence has to support.
    _subject = {
        "recipient_id": recipient_id,
        "channel": channel,
        "message_type": message_type,
        "country_code": country_code,
        "state_code": state_code,
        "content_sha256": _sha256_text(content),
        "content_length_chars": len(content),
    }
    _inputs = {
        "recipient_id": recipient_id,
        "channel": channel,
        "message_type": message_type,
        "country_code": country_code,
        "state_code": state_code,
        "content": content,
    }

    try:
        pre_check(
            recipient_id=recipient_id,
            channel=channel,
            message_type=message_type,
            content=content,
            country_code=country_code,
            state_code=state_code,
            agent_id=agent_id,
            trace_id=trace_id,
            preview=True,
        )
    except ComplianceViolationError as cve:
        result = {
            **base_result,
            "legal": False,
            "rule": cve.rule,
            "jurisdiction": cve.jurisdiction,
            "human_message": cve.message,
            "remediation": _remediation_for(cve.rule),
        }
        # A BLOCKED send is worth MORE evidence than a permitted one, not less:
        # "we ran the gate and it refused, here is the rule and the ruleset" is
        # the record that shows an operator's system stopped an unlawful send.
        _attach(result, operation_id, _subject, _inputs,
                country_code, state_code, channel,
                decision={"permitted": False,
                          "rule": cve.rule,
                          "jurisdiction": cve.jurisdiction})
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.SUCCESS,        # a truthful "no" is a successful check
            reason_code="not_compliant",
            human_message=(
                f"Send would be BLOCKED by the compliance gate ({cve.rule}). "
                f"{cve.message} No message was sent — this was a preview."
            ),
            result=result,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
            next_actions=[
                _remediation_for(cve.rule),
                "Re-run check_compliance once the blocker is resolved, then call send_message.",
            ],
        )

    # --- compliant branch -------------------------------------------------
    result = {**base_result, "legal": True, "rule": None}

    # UNION MODE (adopted 2026-09-21, 36-agent audit — see
    # compliance/jev_advisory.py for the full design and invocation
    # details). jev is an ADDITIONAL, best-effort restricted-category read
    # layered on top of the deterministic gate, in THIS preview tool ONLY.
    # It runs ONLY here, on the branch where the deterministic gate found
    # nothing to block — there is nothing for it to add when the gate has
    # already said BLOCK, and skipping the call there also saves the cost.
    # It can only ever move "legal": True down to False; on any jev failure
    # (bad exit, timeout, empty stdout, unparseable JSON) it is a pure
    # no-op and the deterministic "legal": True stands unchanged. It never
    # touches compliance.pre_check or core.send_message — those remain
    # 100% deterministic, unchanged by this file.
    advisory = get_restricted_category_advisory(content)
    result["jev_advisory"] = {
        "checked": advisory.available,
        "blocked": advisory.blocked,
        "probability": advisory.probability,
        "note": (
            "Additional restricted-category read (jev-1.13), advisory only — "
            "not part of the gate send_message enforces, and not stable near "
            "its own threshold. Present because the deterministic gate found "
            "nothing to block on its own."
            if advisory.available else
            _jev_unavailable_note(advisory.error)
        ),
    }

    if advisory.available and advisory.blocked:
        result["legal"] = False
        result["rule"] = "restricted_content_jev_advisory"
        _attach(result, operation_id, _subject, _inputs,
                country_code, state_code, channel,
                decision={"permitted": False,
                          "rule": "restricted_content_jev_advisory",
                          "jurisdiction": result["jurisdiction"]})
        return OutcomeReceipt(
            operation_id=operation_id,
            status=OperationStatus.SUCCESS,   # a truthful "no" is a successful check
            reason_code="not_compliant",
            human_message=(
                "The deterministic gate found nothing, but an additional jev "
                "restricted-category read flagged this content (p="
                f"{advisory.probability:.2f}). No message was sent — this was "
                "a preview. This signal is advisory, not authoritative: "
                "re-check before trusting it on content near the line."
            ),
            result=result,
            cost=CostRecord(amount=0.0, currency="USD", basis="free"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            retriable=False,
            trace_id=trace_id,
            next_actions=[
                _remediation_for("restricted_content_jev_advisory"),
                "Re-run check_compliance once the blocker is resolved, then call send_message.",
            ],
        )

    _attach(result, operation_id, _subject, _inputs,
            country_code, state_code, channel,
            decision={"permitted": True,
                      "rule": None,
                      "jurisdiction": result["jurisdiction"]})
    return OutcomeReceipt(
        operation_id=operation_id,
        status=OperationStatus.SUCCESS,
        reason_code="compliant",
        human_message=(
            f"Send is permitted under the {result['jurisdiction']} rule set for a "
            f"{message_type} {channel} message. The gate runs again at send time, "
            "so honor any opt-out that lands between now and the send."
        ),
        result=result,
        cost=CostRecord(amount=0.0, currency="USD", basis="free"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        retriable=False,
        trace_id=trace_id,
        next_actions=[
            f"Call send_message(recipient={{id_value:'{recipient_id}'}}, "
            f"message_type='{message_type}', content=...).",
        ],
    )
