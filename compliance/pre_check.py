"""
ComplianceAgent pre-check gate.

Every outbound channel call MUST invoke `pre_check(...)` before dispatching.
This is the only authorization point for outbound communications.
Raises ComplianceViolationError if the communication is not permitted.

Architecture rule: no other module may bypass this check or call channel
adapters directly without first passing through here.
"""
from __future__ import annotations

from typing import Optional

from core.models import ComplianceViolationError
from compliance.consent_store import get_consent_store
from compliance.content_classifier import classify_content
from compliance.jurisdiction_rules import get_rules, infer_jurisdiction, consent_basis_sentence
from compliance.number_jurisdiction import resolve_jurisdiction, jurisdiction_label, could_be_us
from compliance.campaign_registry import get_campaign_registry, UseCaseType
from compliance.audit_log import AuditEventType, get_audit_log


def pre_check(
    *,
    recipient_id: str,
    channel: str,               # "sms", "email", "voice"
    message_type: str,          # "marketing", "transactional", "reminder", etc.
    content: str,
    country_code: Optional[str] = None,
    state_code: Optional[str] = None,
    agent_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    preview: bool = False,
) -> None:
    """
    Perform all compliance checks. Returns None if compliant.
    Raises ComplianceViolationError with structured detail if not.

    Checks performed (in order):
    1. Content classification (restricted categories)
    2. Opt-out list check
    3. Consent check (for marketing/promotional messages)
    4. 10DLC campaign registration check (for US SMS)
    5. Jurisdiction-specific rules

    preview=True runs the identical decision logic but suppresses ALL audit-log
    writes. It exists for the free, read-only `check_compliance` tool: a
    pre-flight must not record an OUTBOUND_DISPATCHED "allow" event (no message
    was sent — that would be the same class of lie as a stub-success receipt),
    nor inflate violation counts for a send that was never attempted. Real
    dispatch paths call with preview=False (the default) and are unchanged.
    """
    # THE RULES FOLLOW THE RECIPIENT, NOT ONLY WHAT THE CALLER TYPED (Door Reliability Run D2, 2026-10-03).
    # An Omani number with country_code "OM" was refused "under the TCPA"; a number sent with no country_code
    # was assumed American. From here on `country_code` is the country the send is judged under: the one the
    # recipient's number names when it names exactly one (the number wins over a contradicting country_code,
    # EXCEPT for a solicitation, where a contradiction is refused below), otherwise the caller's, otherwise
    # None - and None is "unknown", never "US". See compliance/number_jurisdiction.py for the rules and for
    # why a +1 or +7 number needs the caller to say.
    resolution = resolve_jurisdiction(recipient_id, country_code, message_type)
    country_code = resolution.country
    rules = infer_jurisdiction(country_code, state_code)
    consent_store = get_consent_store()
    jurisdiction = jurisdiction_label(country_code, state_code)
    is_us = rules.jurisdiction_code == "US" or rules.jurisdiction_code.startswith("US-")

    # 1. Content classification
    classification = classify_content(content)
    if classification.blocked:
        _audit_violation(
            "restricted_content",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason=classification.reason,
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="restricted_content",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=f"Message content contains restricted category ({classification.category.value}) and cannot be sent.",
        )

    # 2. Opt-out check.
    #
    # FAST PATH: the in-memory set, populated by mark_opted_out/revoke_consent
    # (synchronous, run before this call ever returns) and by a durable hit
    # this same process already confirmed below. Catches a STOP processed
    # earlier in THIS process's lifetime with no network round trip.
    if consent_store.is_opted_out(recipient_id, channel):
        _audit_violation(
            "recipient_opted_out",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason="Recipient has opted out of this channel",
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="recipient_opted_out",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=f"Recipient {recipient_id} has opted out of {channel} communications. Honor opt-out per regulatory requirement.",
        )

    # DURABLE, AUTHORITATIVE CHECK -- and FAIL CLOSED.
    #
    # The in-memory set above is not the whole picture: it is hydrated at
    # boot from consent_optouts via a bulk read that RLS silently empties for
    # the anon key this container holds (200 OK, empty array -- not an error,
    # so it never raised and never will). That is the exact bug
    # tests/compliance_tests/test_optout_enforcement.py exists to prevent,
    # reintroduced through RLS instead of the original bug it was written
    # for. An in-memory miss here does NOT mean "not opted out" -- it means
    # "not yet confirmed either way for this process" -- so every send checks
    # the durable record directly, the same way core/screen_sanctions.py's
    # "AN EMPTY INDEX IS NOT A CLEAN SCREEN" guard refuses to read an
    # unproven emptiness as a clean result.
    #
    # check_durable_optout raises OptoutCheckUnavailable for anything short of
    # a confirmed true/false answer (unreachable database, bad response,
    # wrong shape) -- and that must refuse the send, never let it through as
    # if the recipient were clear. The one case it does NOT raise for is
    # Supabase being unconfigured entirely (local dev, the test suite): see
    # its own docstring for why that is not the same failure.
    from compliance.optout_gate import check_durable_optout, OptoutCheckUnavailable
    try:
        durably_opted_out = check_durable_optout(recipient_id)
    except OptoutCheckUnavailable as exc:
        _audit_violation(
            "optout_check_unavailable",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason=f"durable opt-out check could not run: {exc}",
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="optout_check_unavailable",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=("The durable opt-out record could not be checked, so "
                     "this send is refused rather than allowed -- an empty "
                     "or unreachable result is never read as 'nobody opted "
                     "out'. Retry; if it persists this is a fault on our "
                     "side, not a rule about your message."),
        )
    if durably_opted_out:
        # Cache it so a retry in this same process takes the fast path above
        # instead of re-querying, and so mark_opted_out's own semantics
        # (widen to every channel for this contact) apply from here on.
        consent_store.mark_opted_out(recipient_id, channel)
        _audit_violation(
            "recipient_opted_out",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason="Recipient has opted out of this channel (durable record)",
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="recipient_opted_out",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=f"Recipient {recipient_id} has opted out of {channel} communications. Honor opt-out per regulatory requirement.",
        )

    # 2b. A CONTRADICTION THE GATE WILL NOT RESOLVE FOR A SOLICITATION.
    #
    # The recipient's number names one country and the caller's country_code names another. For a marketing or
    # follow-up message the answer is not "the number wins": calling hours and carrier rules follow where the
    # PERSON is, and a roaming or expatriate recipient is exactly the case where the two differ. Letting the
    # number override the caller's stated country turned 2,914 refusals into allows in the 2026-10-04 review
    # (1,154 of them US calling-hours blocks, ten of them 10DLC). So the gate says it cannot tell and stops;
    # the caller removes the contradiction. Any other message type is judged by the number's country and the
    # answer reports the override (resolve_jurisdiction leaves country None only in this case).
    if resolution.contradicts and resolution.country is None:
        _audit_violation(
            "jurisdiction_conflict",
            recipient_id, channel, "unknown", agent_id, trace_id,
            reason="the recipient number and country_code name different countries for a solicitation",
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="jurisdiction_conflict",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction="unknown",
            message=resolution.conflict or "The recipient number and country_code name different countries.",
        )

    # 3. Consent check for marketing messages
    if message_type == "marketing":
        # AN UNKNOWN JURISDICTION IS NOT "US".
        #
        # With no country_code the rules resolve to INTERNATIONAL while the
        # jurisdiction LABEL falls back to "US" - so a refusal could be audited
        # as a US decision while applying international rules, and a US-lawful
        # CAN-SPAM email could be judged under an opt-in regime. Two different
        # answers to "which law applies", inside one call.
        #
        # For transactional traffic the guess is harmless. For MARKETING it
        # decides whether opt-in is required at all, so the honest thing is to
        # refuse to guess and say so. This is a real API change: a marketing
        # send must now state where the recipient is. That is not a burden
        # invented here - you cannot apply the right law without it, and every
        # regime this gate implements is defined by the recipient's location.
        if not country_code:
            _audit_violation(
                "jurisdiction_required",
                recipient_id, channel, "unknown", agent_id, trace_id,
                reason="marketing send with no country_code - cannot determine "
                       "which consent regime applies",
                preview=preview,
            )
            raise ComplianceViolationError(
                rule="jurisdiction_required",
                recipient_id=recipient_id,
                channel=channel,
                jurisdiction="unknown",
                message=("Marketing messages need a known country so the correct "
                         "consent rules apply - opt-in regimes (GDPR, CASL) "
                         "and opt-out regimes (CAN-SPAM) reach opposite "
                         "conclusions on the same message. Pass the recipient's "
                         "two-letter country_code, or an E.164 recipient number "
                         "whose country calling code names it."
                         + (" " + resolution.conflict if resolution.conflict else "")),
            )
        # THE RULE'S NAME IS THE LAW THAT WAS APPLIED. This branch used to raise "TCPA_marketing_consent" for
        # every jurisdiction whose rule set asks for opt-in - which is all of them, because the field defaults
        # to True - so an Omani, a German or a Canadian marketing SMS was refused "under a US statute".
        # Now: the US keeps the TCPA; a GDPR or CASL jurisdiction is refused by its own correctly named rule
        # just below (it checks consent for the same channel, so skipping here loses nothing); anywhere else
        # the service's own conservative default applies and the answer says exactly that.
        if (channel == "sms" and rules.sms_marketing_requires_prior_express_written_consent
                and not (rules.gdpr_applies or rules.casl_applies)):
            if not consent_store.has_valid_consent(recipient_id, "sms", "marketing"):
                if is_us:
                    _audit_violation(
                        "TCPA_marketing_consent",
                        recipient_id, channel, jurisdiction, agent_id, trace_id,
                        reason="No TCPA prior express written consent on file",
                        preview=preview,
                    )
                    raise ComplianceViolationError(
                        rule="TCPA_marketing_consent",
                        recipient_id=recipient_id,
                        channel="sms",
                        jurisdiction=jurisdiction,
                        message=f"Recipient {recipient_id} has not opted in to marketing SMS. TCPA prior express written consent is required.",
                    )
                _audit_violation(
                    "sms_marketing_consent",
                    recipient_id, channel, jurisdiction, agent_id, trace_id,
                    reason="No opt-in consent for marketing SMS on file (conservative default; "
                           "no jurisdiction-specific statute applied)",
                    preview=preview,
                )
                raise ComplianceViolationError(
                    rule="sms_marketing_consent",
                    recipient_id=recipient_id,
                    channel="sms",
                    jurisdiction=jurisdiction,
                    message=(f"Recipient {recipient_id} has not opted in to marketing SMS. "
                             f"{consent_basis_sentence(country_code)}"),
                )

        if rules.gdpr_applies and not consent_store.has_valid_consent(recipient_id, channel, "marketing"):
            _audit_violation(
                "GDPR_marketing_consent",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
                reason="No GDPR lawful basis / opt-in on file",
                preview=preview,
            )
            raise ComplianceViolationError(
                rule="GDPR_marketing_consent",
                recipient_id=recipient_id,
                channel=channel,
                jurisdiction=jurisdiction,
                message=f"GDPR requires explicit opt-in consent for marketing messages to EU/UK recipients.",
            )

        if rules.casl_applies and not consent_store.has_valid_consent(recipient_id, channel, "marketing"):
            _audit_violation(
                "CASL_marketing_consent",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
                preview=preview,
            )
            raise ComplianceViolationError(
                rule="CASL_marketing_consent",
                recipient_id=recipient_id,
                channel=channel,
                jurisdiction=jurisdiction,
                message="CASL requires express consent for commercial electronic messages to Canadian recipients.",
            )

        # 3a. THE TWO CHANNELS NOBODY WAS GATING.
        #
        # Everything above covers: SMS anywhere (TCPA-style written consent),
        # and any channel inside the GDPR or CASL blocs. That leaves two holes
        # an external compliance review walked straight through, live:
        #
        #   * AUTODIALED VOICE MARKETING had NO consent gate in any
        #     jurisdiction. `voice_autodialed_requires_prior_express_consent`
        #     is defined for all 26 jurisdictions and was read by nothing.
        #     A marketing voice call to an Omani number returned legal=True,
        #     with quiet-hours as the only thing in its way - and quiet-hours
        #     was failing OPEN on error until the fix directly below.
        #     Autodialed telemarketing without prior express written consent is
        #     the single most-litigated TCPA category.
        #
        #   * MARKETING EMAIL outside the GDPR/CASL blocs had no gate either,
        #     so every GCC, Asian and Latin American recipient could be
        #     marketed to with no opt-in - including Oman, the founder's home
        #     market. (No Omani statute is modeled here; this is the service's
        #     own opt-in default, and the answers say so.) The
        #     INTERNATIONAL default's own docstring promises "opt-in required
        #     for marketing" and nothing enforced it off the SMS path.
        #
        # These use the rule fields that already existed and were dead. The
        # honest position is that marketing to a person who has not opted in is
        # refused on every channel, everywhere - which is also what our own
        # marketing says we do.
        if channel == "voice" and rules.voice_autodialed_requires_prior_express_consent:
            if not consent_store.has_valid_consent(recipient_id, "voice", "marketing"):
                _audit_violation(
                    "voice_marketing_consent",
                    recipient_id, channel, jurisdiction, agent_id, trace_id,
                    reason="No prior express consent on file for autodialed marketing voice",
                    preview=preview,
                )
                raise ComplianceViolationError(
                    rule="voice_marketing_consent",
                    recipient_id=recipient_id,
                    channel="voice",
                    jurisdiction=jurisdiction,
                    message=(f"Recipient {recipient_id} has not opted in to marketing "
                             f"calls. Prior express consent is required for autodialed "
                             f"or prerecorded marketing voice in {jurisdiction}."
                             + _default_policy_note(rules, country_code, "marketing calls")),
                )

        # CAN-SPAM IS AN OPT-OUT REGIME, and getting that wrong would have been
        # a false block rather than a false allow - the other kind of harm.
        #
        # My first version required opt-in for marketing email everywhere
        # outside GDPR/CASL, which would have refused every lawful US marketing
        # email. US law (15 U.S.C. 7704) permits commercial email WITHOUT prior
        # consent provided there is a working unsubscribe mechanism and an
        # honest header - which is exactly what `email_requires_unsubscribe`
        # and the unsubscribe-link machinery already enforce. An existing test
        # caught this immediately by asserting a lawful US marketing email
        # still sends.
        #
        # So the opt-in bar applies to the regimes that actually require opt-in
        # - the Gulf and every other country with no statute modeled here (the
        # service's own opt-in default), including the international default
        # whose own docstring promises it - and CAN-SPAM jurisdictions keep
        # their opt-out rule.
        if (channel == "email"
                and not (rules.gdpr_applies or rules.casl_applies
                         or rules.can_spam_applies)):
            if not consent_store.has_valid_consent(recipient_id, "email", "marketing"):
                _audit_violation(
                    "email_marketing_consent",
                    recipient_id, channel, jurisdiction, agent_id, trace_id,
                    reason="No opt-in on file for marketing email outside GDPR/CASL",
                    preview=preview,
                )
                raise ComplianceViolationError(
                    rule="email_marketing_consent",
                    recipient_id=recipient_id,
                    channel="email",
                    jurisdiction=jurisdiction,
                    message=(f"Recipient {recipient_id} has not opted in to marketing "
                             f"email. Opt-in is required for commercial email in "
                             f"{jurisdiction}."
                             + _default_policy_note(rules, country_code, "marketing email")),
                )

        # 3a'. EVERY OTHER CHANNEL - WhatsApp, and any channel added later.
        #
        # The branches above are SMS, voice and email, plus every channel inside the GDPR and CASL blocs. A
        # marketing message on WhatsApp outside those blocs had no consent branch at all: before the country was
        # read from the number it was refused only for want of a country (jurisdiction_required), and once the
        # number named Oman that was the only barrier gone - the review of 2026-10-04 sent one to an Omani
        # number with an empty consent store and the gate allowed it. The service's own policy is recorded
        # opt-in for marketing, ON THE CHANNEL THE MESSAGE GOES OVER, in every country, so a channel with no
        # branch of its own fails closed instead of open.
        if channel not in _CHANNELS_WITH_OWN_MARKETING_RULE:
            if not consent_store.has_valid_consent(recipient_id, channel, "marketing"):
                _other_rule = "whatsapp_marketing_consent" if channel == "whatsapp" else "marketing_consent"
                _label = "WhatsApp" if channel == "whatsapp" else f"the {channel} channel"
                _audit_violation(
                    _other_rule,
                    recipient_id, channel, jurisdiction, agent_id, trace_id,
                    reason=f"No opt-in on file for marketing on {channel} (the service's own default; "
                           f"no jurisdiction-specific statute applied)",
                    preview=preview,
                )
                raise ComplianceViolationError(
                    rule=_other_rule,
                    recipient_id=recipient_id,
                    channel=channel,
                    jurisdiction=jurisdiction,
                    message=(f"Recipient {recipient_id} has not opted in to marketing on {_label}. Recorded "
                             f"opt-in consent is required for marketing on every channel this gate has no "
                             f"statute-specific rule for, and an opt-in given for another channel does not "
                             f"cover this one. This is the service's own policy, not a citation of any "
                             f"country's law."),
                )

    # 3b. QUIET HOURS. TCPA restricts solicitation to 8am-9pm in the
    # RECIPIENT'S local time (47 CFR 64.1200(c)(1)), $500-$1,500 statutory
    # damages per message. Nothing in this codebase enforced a time window
    # until 2026-08-26 - demand_shaping listed it as "deferred, not
    # dropped" and it had stayed deferred, while every public surface
    # advertised "TCPA compliance built in".
    #
    # Only solicitation is gated, and only on TELEPHONE channels - email is
    # CAN-SPAM territory with no calling window.
    #
    # Runs AFTER the consent checks on purpose. Consent is a PERMANENT bar
    # and quiet hours a TEMPORARY one; reporting 'wrong hour' to a caller
    # who has no consent at all would send them back at 9am to fail again.
    # Report the permanent problem first.
    try:
        from compliance.quiet_hours import check as _qh_check
        _qh = _qh_check(message_type, country_code, state_code,
                            recipient_id=recipient_id, channel=channel)
    except Exception as exc:  # noqa: BLE001
        # FAIL CLOSED. This used to set _qh = None and fall through, and the
        # enforcement below only fires when _qh is not None - so ANY error in
        # the quiet-hours check let the message go out.
        #
        # That is the worst direction for this particular check, because
        # quiet-hours is the ONLY time-of-day protection standing in front of
        # marketing voice and SMS. A compliance product that silently allows on
        # error is not a compliance product; every other bar here (consent,
        # opt-out, content, 10DLC) already fails closed and this one was the
        # exception.
        #
        # Found by an external compliance review, 2026-08-29.
        import logging as _lg
        _lg.getLogger("smb_broker.pre_check").warning(
            "quiet_hours_check_failed err=%s", exc)
        _audit_violation(
            "quiet_hours_check_unavailable",
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason=f"quiet-hours check could not run: {type(exc).__name__}: {exc}",
            preview=preview,
        )
        raise ComplianceViolationError(
            rule="quiet_hours_check_unavailable",
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=("The quiet-hours check could not be evaluated, so this "
                     "send is refused rather than allowed. Retry; if it "
                     "persists this is a fault on our side, not a rule about "
                     "your message."),
        )
    if _qh is not None and not _qh.allowed:
        # The calling-hours rule is the TCPA's only where the TCPA applies. Anywhere else the window is
        # either one the service models (CA, GB, EU) or its own default, and the answer says which.
        _qh_rule = "TCPA_quiet_hours" if is_us else "quiet_hours"
        _audit_violation(
            _qh_rule,
            recipient_id, channel, jurisdiction, agent_id, trace_id,
            reason=f"{_qh.reason}; local {_qh.local_time}; window {_qh.window}",
            preview=preview,
        )
        if _qh.reason == "outside_permitted_hours":
            from compliance.quiet_hours import window_is_modeled as _window_is_modeled
            if is_us or _window_is_modeled(country_code, state_code):
                _window_basis = ""
            else:
                _window_basis = (f" No {country_code or 'country'}-specific hours are implemented; this is the "
                                 f"service's default window, not a citation of {country_code or 'any'} law.")
            _qh_message = (
                f"Solicitation is not permitted at this hour for {jurisdiction}. "
                f"Recipient local time {_qh.local_time or 'unknown'}; permitted "
                f"window {_qh.window}.{_window_basis} Retry in ~{(_qh.retry_after_s or 3600)//60} "
                f"minutes. Transactional messages are unaffected."
            )
        else:
            _qh_message = (
                f"Cannot determine the recipient's local time, so a marketing "
                f"send is held rather than risk an unlawful hour. Supply "
                f"country_code (and state_code for US) to enable it."
            )
        raise ComplianceViolationError(
            rule=_qh_rule,
            recipient_id=recipient_id,
            channel=channel,
            jurisdiction=jurisdiction,
            message=_qh_message,
        )

    # 4. 10DLC campaign check for US SMS. A US carrier rule: applied to a US recipient and to one the number
    # could not rule out (+1, or no number at all), never to a number that cannot be American (+44, +968, +7).
    if channel == "sms" and could_be_us(resolution):
        use_case_map = {
            "marketing": UseCaseType.MARKETING,
            "transactional": UseCaseType.ACCOUNT_NOTIFICATION,
            "reminder": UseCaseType.APPOINTMENT_REMINDER,
            "otp": UseCaseType.TWO_FACTOR_AUTHENTICATION,
        }
        use_case = use_case_map.get(message_type, UseCaseType.MIXED)
        registry = get_campaign_registry()
        if not registry.is_sms_authorized(use_case):
            _audit_violation(
                "10DLC_campaign_not_registered",
                recipient_id, channel, jurisdiction, agent_id, trace_id,
                reason=f"No registered 10DLC campaign for use case {use_case}",
                preview=preview,
            )
            raise ComplianceViolationError(
                rule="10DLC_campaign_not_registered",
                recipient_id=recipient_id,
                channel="sms",
                jurisdiction=jurisdiction,
                message=f"No registered 10DLC campaign for use_case={use_case}. A2P SMS requires carrier-registered campaign.",
            )

    # All checks passed. In a real dispatch this records an authorized-send
    # audit event; in preview mode we record nothing, because no send is
    # happening — an OUTBOUND_DISPATCHED "allow" here would claim a dispatch
    # that never occurred.
    if preview:
        return
    get_audit_log().record(
        event_type=AuditEventType.OUTBOUND_DISPATCHED,
        agent_id=agent_id,
        recipient_id=recipient_id,
        channel=channel,
        use_case=message_type,
        jurisdiction=jurisdiction,
        decision="allow",
        trace_id=trace_id,
    )


# Channels whose marketing consent is decided by the branches above (SMS, voice, email, and every channel inside
# the GDPR and CASL blocs). Any other channel falls to the service's own opt-in default.
_CHANNELS_WITH_OWN_MARKETING_RULE = frozenset({"sms", "voice", "email"})


def _default_policy_note(rules, country_code, what: str) -> str:
    """A leading space and the sentence saying a refusal is the service's own policy, when no statute of the
    country is modeled; nothing at all where one is (so a US voice refusal does not call itself a default)."""
    return "" if rules.statutes else " " + consent_basis_sentence(country_code, what)


def _audit_violation(
    rule: str,
    recipient_id: str,
    channel: str,
    jurisdiction: str,
    agent_id: Optional[str],
    trace_id: Optional[str],
    reason: str = "",
    preview: bool = False,
) -> None:
    # A preview (check_compliance) must be side-effect free — do not record a
    # violation for a send that was never attempted.
    if preview:
        return
    get_audit_log().record(
        event_type=AuditEventType.COMPLIANCE_VIOLATION,
        agent_id=agent_id,
        recipient_id=recipient_id,
        channel=channel,
        jurisdiction=jurisdiction,
        decision="deny",
        reason=reason or rule,
        trace_id=trace_id,
        metadata={"rule": rule},
    )
