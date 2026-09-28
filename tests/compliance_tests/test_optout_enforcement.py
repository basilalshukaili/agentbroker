"""Regression tests for the STOP/opt-out enforcement leak (found + fixed 2026-08-24,
and reintroduced through RLS + fixed again 2026-09-28).

The 2026-08-24 bug: `revoke_consent` no-op'd (returned False, did nothing) when no
prior opt-in record existed - the NORMAL case, since consent records are in-memory
and empty after any restart. `is_opted_out` read ONLY the in-memory records, and the
durable `consent_optouts` table was WRITTEN by handle_inbound but never READ. Net:
after a redeploy a recorded STOP no longer blocked sends -> the "non-bypassable"
compliance gate leaked to opted-out recipients. The tests above this comment lock
that fix in, but every one of them runs against `pre_check` with the in-memory
ConsentStore pre-seeded directly (`mark_opted_out`) - a BYPASSING path, never the
one production actually takes.

THE 2026-09-28 REGRESSION, through a different door. `consent_optouts` got RLS
enabled with zero policies. This container holds only the anon key, which does
not bypass RLS, so main.py's boot-time bulk hydration of that table
(`select_rows_strict` -> `hydrate_opted_out`) gets HTTP 200 with an EMPTY ARRAY -
not an error - and silently loads ZERO durable opt-outs on every boot, forever,
regardless of how many people actually opted out. The tests above never caught
it because none of them exercise a fresh, unhydrated ConsentStore talking to the
durable store the way `pre_check` really does in production; they all seed the
in-memory set by hand and never touch `compliance/optout_gate.py`.

The tests below close that gap: they run `pre_check` against a store that has
NOT been told about the opt-out (simulating exactly the "hydration silently
loaded 0 rows" production state) and rely on `compliance.optout_gate.check_durable_optout`
- the SECURITY DEFINER RPC membership check
(sql/agentbroker/006_consent_optouts_membership_rpc.sql) - to be the thing that
actually catches it, the way it does in production. All contacts below are
synthetic (the `+1555…` / `example.com` convention this file already used) -
never a value from the live table.
"""
import pytest

import compliance.optout_gate as optout_gate
import storage.supabase_client as sb
from compliance.consent_store import ConsentStore, get_consent_store
from compliance.pre_check import pre_check
from core.models import ComplianceViolationError


def test_revoke_with_no_prior_consent_registers_optout():
    """A STOP from a recipient who never opted in must still register (was the no-op)."""
    store = ConsentStore()
    assert store.is_opted_out("+15551234567", "sms") is False
    ok = store.revoke_consent("+15551234567", "sms", "marketing", "keyword_STOP")
    assert ok is True
    assert store.is_opted_out("+15551234567", "sms") is True


def test_mark_opted_out_email_channel():
    """Email opt-outs (recipient_id is an email) are honored, not just phone."""
    store = ConsentStore()
    store.mark_opted_out("user@example.com", "email")
    assert store.is_opted_out("user@example.com", "email") is True


def test_an_optout_covers_the_person_across_every_channel():
    """A STOP suppresses the CONTACT, not the transport it arrived on.

    This file used to assert the opposite - that a different channel for the
    same recipient was still sendable. That assertion described an impossible
    situation (its example opted an EMAIL ADDRESS out of email, then checked
    SMS to the same email address, which nothing can send) while permitting a
    very possible one: a phone number texts STOP, and we place an autodialed
    marketing call to it the next day.

    That is not what STOP means to the person who sent it, and it is the most
    likely way this gate produces a genuinely angry customer - someone who took
    the trouble to opt out and got phoned anyway.

    Widening the check can only ever SUPPRESS more messages, never release one,
    so it is safe in the only direction that matters here.
    """
    store = ConsentStore()
    store.mark_opted_out("+15551234567", "sms")
    assert store.is_opted_out("+15551234567", "sms") is True
    assert store.is_opted_out("+15551234567", "voice") is True, (
        "a number that texted STOP can still be autodialed")
    assert store.is_opted_out("+15551234567", "whatsapp") is True, (
        "a number that texted STOP can still be WhatsApped")


def test_an_optout_does_not_leak_to_other_people():
    """Identity-scoped, not global. The widening must not suppress anybody
    else's messages."""
    store = ConsentStore()
    store.mark_opted_out("+15551234567", "sms")
    assert store.is_opted_out("+15559999999", "sms") is False
    assert store.is_opted_out("someone@example.com", "email") is False


def test_optout_survives_restart_via_hydration():
    """Simulated restart: a fresh (empty) store hydrated from the durable table blocks."""
    fresh = ConsentStore()
    assert fresh.is_opted_out("+15559999999", "sms") is False
    loaded = fresh.hydrate_opted_out([("+15559999999", "sms"), (None, "sms"), ("+1", "")])
    assert loaded == 1  # the two malformed pairs are skipped
    assert fresh.is_opted_out("+15559999999", "sms") is True


def test_pre_check_blocks_opted_out_recipient_end_to_end():
    """The gate itself (pre_check) must raise for an opted-out recipient."""
    recipient = "+15550001111"
    get_consent_store().mark_opted_out(recipient, "sms")
    with pytest.raises(ComplianceViolationError) as excinfo:
        pre_check(
            recipient_id=recipient,
            channel="sms",
            message_type="transactional",
            content="Your appointment is confirmed for Saturday.",
            country_code="US",
            state_code="CA",
        )
    assert excinfo.value.rule == "recipient_opted_out"


# ---------------------------------------------------------------------------
# THE PRODUCTION PATH (2026-09-28). Every test above seeds the in-memory
# ConsentStore directly - a bypassing role, in the same sense the task that
# opened this file's rewrite described: it proves pre_check's LOGIC is
# correct once the store already knows about the opt-out, but it never
# proves the store actually LEARNS about a durable opt-out in production,
# which is exactly where the regression lived. These tests run pre_check
# against a store that hydrated ZERO rows - the real, current, permanent
# state under RLS + the anon key - and rely on
# compliance.optout_gate.check_durable_optout (the SECURITY DEFINER RPC in
# sql/agentbroker/006_consent_optouts_membership_rpc.sql) to be the thing
# that catches it, exactly as compliance/pre_check.py does today.
# ---------------------------------------------------------------------------
import compliance.consent_store as cs_module


def _pretend_supabase_is_configured(monkeypatch):
    """check_durable_optout takes a deliberate shortcut (return False, no
    network call) when Supabase is entirely unconfigured - correct for local
    dev and this test suite, but it means every test below must first
    pretend the container IS configured (as production always is) or it
    would never reach the RPC call these tests exist to exercise."""
    monkeypatch.setattr(sb, "_get_config", lambda: ("https://example.test", "fake-anon-key"))


def test_durable_rpc_catches_an_optout_that_boot_hydration_missed(monkeypatch):
    """THE REGRESSION ITSELF, reproduced and closed.

    A fresh ConsentStore hydrated from an empty list - precisely what
    main.py's real boot hydration returns today, and will keep returning
    forever, because RLS-zero-policy + the anon key means that bulk read can
    never see a row. Before this fix, pre_check's opt-out check was ONLY
    `consent_store.is_opted_out(...)`, which reads that same empty set - so a
    contact whose opt-out lives solely in the durable table sailed through as
    if nobody had ever opted out. The durable RPC check must now catch it.
    """
    recipient = "+15557778888"  # synthetic - never a real contact
    fresh = ConsentStore()
    loaded = fresh.hydrate_opted_out([])  # exactly what an RLS-emptied boot hydration returns
    assert loaded == 0
    assert fresh.is_opted_out(recipient, "sms") is False  # confirms the in-memory miss

    original = cs_module._store
    cs_module._store = fresh
    try:
        _pretend_supabase_is_configured(monkeypatch)
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: True)
        with pytest.raises(ComplianceViolationError) as excinfo:
            pre_check(
                recipient_id=recipient,
                channel="sms",
                message_type="transactional",
                content="Your appointment is confirmed for Saturday.",
                country_code="US",
                state_code="CA",
            )
        assert excinfo.value.rule == "recipient_opted_out"
    finally:
        cs_module._store = original


def test_pre_check_allows_a_genuinely_clear_contact_via_durable_check(monkeypatch):
    """The other direction: the durable check must not over-block. A contact
    the RPC genuinely confirms as NOT opted out must still be sendable."""
    recipient = "+15557778891"  # synthetic
    fresh = ConsentStore()
    original = cs_module._store
    cs_module._store = fresh
    try:
        _pretend_supabase_is_configured(monkeypatch)
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: False)
        pre_check(
            recipient_id=recipient,
            channel="email",
            message_type="transactional",
            content="Your booking is confirmed.",
            country_code="US",
        )  # must not raise
    finally:
        cs_module._store = original


def test_pre_check_fails_closed_when_durable_check_is_unreachable(monkeypatch):
    """MUTATION-PROVEN (1 of 3): 'could not check' must never be read as
    'clear'. When the RPC call itself fails (network, non-200, ...),
    pre_check must refuse the send under its own dedicated rule, never fall
    through to 'not opted out'.

    Mutation applied to prove this test is real: in compliance/pre_check.py,
    change `except OptoutCheckUnavailable as exc:` to swallow the exception
    and treat it as `durably_opted_out = False` instead of raising. Ran this
    test alone -> FAILED (pre_check returned normally instead of raising).
    Reverted -> green again. See the session report for the exact diff and
    the pytest output of both runs.
    """
    recipient = "+15557778889"  # synthetic
    fresh = ConsentStore()
    original = cs_module._store
    cs_module._store = fresh
    try:
        _pretend_supabase_is_configured(monkeypatch)

        def _boom(fn, payload):
            raise RuntimeError("simulated: rpc_sync transport error")

        monkeypatch.setattr(sb, "rpc_sync", _boom)
        with pytest.raises(ComplianceViolationError) as excinfo:
            pre_check(
                recipient_id=recipient,
                channel="email",
                message_type="transactional",
                content="Your booking is confirmed.",
                country_code="US",
            )
        assert excinfo.value.rule == "optout_check_unavailable"
    finally:
        cs_module._store = original


def test_pre_check_fails_closed_when_rpc_returns_rows_not_a_boolean(monkeypatch):
    """MUTATION-PROVEN (2 of 3): the RPC's contract is a bare boolean - never
    rows, a count, or anything else. If it ever comes back shaped like
    anything else (schema drift, a bad deploy, a future edit that turns the
    membership test back into a listing), the caller must refuse rather than
    guess at what the shape means. An empty list is the most dangerous wrong
    shape (bool([]) is False, so a naive cast would read it as "not opted
    out" and let the send through) - this test uses exactly that shape.

    Mutation applied to prove this test is real: in compliance/optout_gate.py,
    change `if isinstance(result, bool): return result` to
    `return bool(result)` (accept any shape, cast it). Ran this test alone ->
    FAILED (pre_check let a non-boolean RPC response allow the send). Reverted
    -> green again. See the session report for the exact diff and the pytest
    output of both runs.
    """
    recipient = "+15557778890"  # synthetic
    fresh = ConsentStore()
    original = cs_module._store
    cs_module._store = fresh
    try:
        _pretend_supabase_is_configured(monkeypatch)
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: [])
        with pytest.raises(ComplianceViolationError) as excinfo:
            pre_check(
                recipient_id=recipient,
                channel="email",
                message_type="transactional",
                content="Your booking is confirmed.",
                country_code="US",
            )
        assert excinfo.value.rule == "optout_check_unavailable"
    finally:
        cs_module._store = original


def test_pre_check_treats_hydration_emptiness_as_unproven_not_as_clear(monkeypatch):
    """MUTATION-PROVEN (3 of 3): the guard that gates the durable RPC call
    must not itself be bypassable by making hydration LOOK complete. Even a
    ConsentStore that hydrated a (wrong) non-zero count for OTHER contacts
    must still consult the durable record for THIS recipient rather than
    trusting the in-memory miss.

    Mutation applied to prove this test is real: in compliance/pre_check.py,
    change the durable-check block to be skipped whenever
    `consent_store._opted_out` is non-empty (i.e. "hydration loaded
    something, so trust it from here on"). Ran this test alone -> FAILED
    (pre_check let the send through because an unrelated contact's presence
    in the set was mistaken for proof this recipient was checked). Reverted
    -> green again. See the session report for the exact diff and the pytest
    output of both runs.
    """
    recipient = "+15557778893"  # synthetic - never told to the in-memory store
    fresh = ConsentStore()
    fresh.mark_opted_out("+15550000000", "sms")  # an unrelated contact, hydrated fine
    original = cs_module._store
    cs_module._store = fresh
    try:
        _pretend_supabase_is_configured(monkeypatch)
        monkeypatch.setattr(sb, "rpc_sync", lambda fn, payload: True)
        with pytest.raises(ComplianceViolationError) as excinfo:
            pre_check(
                recipient_id=recipient,
                channel="sms",
                message_type="transactional",
                content="Your appointment is confirmed for Saturday.",
                country_code="US",
                state_code="CA",
            )
        assert excinfo.value.rule == "recipient_opted_out"
    finally:
        cs_module._store = original


def test_check_durable_optout_skips_only_when_supabase_is_unconfigured(monkeypatch):
    """The one deliberate exception, isolated: no SUPABASE_URL/key at all
    (local dev, this test suite) must not be treated as a failure - that is
    'agent_interface/unsubscribe.py's NOT CONFIGURED IS NOT THE SAME AS DOWN'
    posture, not a loophole for production (which always has credentials)."""
    monkeypatch.setattr(sb, "_get_config", lambda: ("", ""))
    assert optout_gate.check_durable_optout("+15557778894") is False


def test_check_durable_optout_calls_the_rpc_with_only_the_recipient(monkeypatch):
    """Confirms the call shape: one parameter, no channel - matching
    ConsentStore.is_opted_out()'s widened, contact-scoped rule, and never
    passing anything beyond what the RPC's signature accepts."""
    captured = {}

    def _fake_rpc(fn, payload):
        captured["fn"] = fn
        captured["payload"] = payload
        return False

    _pretend_supabase_is_configured(monkeypatch)
    monkeypatch.setattr(sb, "rpc_sync", _fake_rpc)
    assert optout_gate.check_durable_optout("+15557778895") is False
    assert captured["fn"] == "consent_optouts_is_opted_out"
    assert captured["payload"] == {"p_recipient_id": "+15557778895"}
