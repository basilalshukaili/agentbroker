"""
Unit tests for compliance.jev_advisory and its union wiring into
core.check_compliance.

These tests are ALWAYS ON (no live network, no real jev calls, no cost) —
they pin the fallback contract with mocks so the suite never depends on a
live subprocess. The live, real-jev proof (10/10 adversarial catch, the
regex floor, forced failures, determinism spread) lives in
tests/compliance_tests/test_jev_union_adversarial.py, gated behind
HATCHLOOP_ALLOW_LIVE=1 so routine `pytest` runs never make a real, billed
call — see compliance/jev_advisory.py's `_under_test` guard, which is what
makes THIS file safe to run unconditionally even though it imports the same
module the live tests do.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

import compliance.jev_advisory as jev_advisory
from compliance.jev_advisory import JevAdvisory, get_restricted_category_advisory
from core.check_compliance import handle_check_compliance
from core.models import OperationStatus


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The under-test guard itself: this is what stops the existing
# tests/unit/test_check_compliance.py suite (and anyone else running
# ordinary pytest) from silently making real jev calls the moment
# check_compliance started importing this module.
# ---------------------------------------------------------------------------

class TestUnderTestGuard:
    def test_refuses_a_real_call_while_running_under_pytest(self):
        # No HATCHLOOP_ALLOW_LIVE set (conftest/CI default) -> must not shell
        # out at all, regardless of content.
        result = get_restricted_category_advisory("join our casino and place bets")
        assert result.available is False
        assert "test runner" in (result.error or "")
        assert result.blocked is None

    def test_allow_live_env_var_is_the_only_bypass(self, monkeypatch):
        monkeypatch.setenv("HATCHLOOP_ALLOW_LIVE", "1")
        # Board row 282: binary resolution is now a real step that runs
        # before subprocess.run is ever called (see resolve_jev_binary in
        # compliance/jev_advisory.py), so this test needs an explicit,
        # portable JEV_BIN or its outcome would depend on whether THIS
        # machine happens to have `jev` on PATH — exactly the
        # host-dependency this test is designed to avoid (see the comment
        # below: "a zero-cost, offline assertion about the GATE, not a live
        # call"). Any existing file resolves; it is never executed before
        # the mocked subprocess.run below.
        monkeypatch.setenv("JEV_BIN", __file__)
        # Still must not actually hit the network in this unit test: patch
        # subprocess.run so this stays a zero-cost, offline assertion about
        # the GATE, not a live call.
        called = {}

        def _fake_run(cmd, **kwargs):
            called["cmd"] = cmd
            class _P:
                returncode = 1
                stdout = '{"model": "x", "answer": {"type": "noul", "noul": 0.1, "yes": false}}'
                stderr = ""
            return _P()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        result = get_restricted_category_advisory("ordinary receipt text")
        assert called, "the gate should have let the (mocked) call through"
        assert result.available is True
        assert result.blocked is False


# ---------------------------------------------------------------------------
# Failure-mode handling inside get_restricted_category_advisory itself —
# every one of these must return available=False and MUST NOT raise.
# ---------------------------------------------------------------------------

class TestFailureModesNeverRaiseAndNeverGuess:
    """Every case here forces HATCHLOOP_ALLOW_LIVE=1 (to get past the test
    guard above) and then mocks subprocess.run directly, so no real call is
    ever made — only our own error handling is exercised."""

    def _patched(self, monkeypatch, fake_run):
        monkeypatch.setenv("HATCHLOOP_ALLOW_LIVE", "1")
        # Board row 282: see the identical comment in
        # test_allow_live_env_var_is_the_only_bypass above — resolution now
        # runs for real before the mocked subprocess.run, so it needs a
        # portable JEV_BIN to stay host-independent.
        monkeypatch.setenv("JEV_BIN", __file__)
        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_timeout_expired_is_no_verdict_not_false(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
        self._patched(monkeypatch, _fake_run)
        r = get_restricted_category_advisory("some content")
        assert r.available is False
        assert r.blocked is None
        assert "timeout" in (r.error or "").lower()

    def test_exit_2_with_empty_stdout_is_no_verdict_not_false(self, monkeypatch):
        class _P:
            returncode = 2
            stdout = ""
            stderr = "jev: API 错误 (HTTP 400): Unknown model: jev-1.13"
        self._patched(monkeypatch, lambda cmd, **kw: _P())
        r = get_restricted_category_advisory("some content")
        assert r.available is False
        assert r.blocked is None
        assert "exit 2" in (r.error or "")

    def test_unexpected_exit_code_is_no_verdict(self, monkeypatch):
        class _P:
            returncode = 130
            stdout = ""
            stderr = "interrupted"
        self._patched(monkeypatch, lambda cmd, **kw: _P())
        r = get_restricted_category_advisory("some content")
        assert r.available is False
        assert r.blocked is None

    def test_exit_0_but_empty_stdout_is_no_verdict(self, monkeypatch):
        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        self._patched(monkeypatch, lambda cmd, **kw: _P())
        r = get_restricted_category_advisory("some content")
        assert r.available is False

    def test_unparseable_json_is_no_verdict(self, monkeypatch):
        class _P:
            returncode = 0
            stdout = "not json at all {{{"
            stderr = ""
        self._patched(monkeypatch, lambda cmd, **kw: _P())
        r = get_restricted_category_advisory("some content")
        assert r.available is False
        assert "unparseable" in (r.error or "")

    def test_arbitrary_exception_from_subprocess_never_raises(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            raise OSError("pretend the OS ran out of handles")
        self._patched(monkeypatch, _fake_run)
        r = get_restricted_category_advisory("some content")  # must not raise
        assert r.available is False
        assert "OSError" in (r.error or "")

    def test_empty_content_short_circuits_before_any_subprocess_call(self, monkeypatch):
        monkeypatch.setenv("HATCHLOOP_ALLOW_LIVE", "1")
        def _fake_run(cmd, **kwargs):
            raise AssertionError("must not be called for empty content")
        monkeypatch.setattr(subprocess, "run", _fake_run)
        r = get_restricted_category_advisory("   ")
        assert r.available is False

    def test_well_formed_success_is_parsed(self, monkeypatch):
        class _P:
            returncode = 0
            stdout = '{"model": "jev-1.13.0", "answer": {"type": "noul", "noul": 0.93, "yes": true}}'
            stderr = ""
        self._patched(monkeypatch, lambda cmd, **kw: _P())
        r = get_restricted_category_advisory("join our casino")
        assert r.available is True
        assert r.blocked is True
        assert r.probability == pytest.approx(0.93)


# ---------------------------------------------------------------------------
# Union wiring inside handle_check_compliance: jev can only ADD a block, and
# is never even consulted once the deterministic gate has already blocked.
# ---------------------------------------------------------------------------

class TestUnionWiringInCheckCompliance:
    def test_jev_block_flips_an_otherwise_compliant_preview(self, monkeypatch):
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(available=True, blocked=True,
                                         probability=0.81, error=None),
        )
        r = run(handle_check_compliance(
            recipient_id="jane@example.com",
            content="نص عادي بدون أي كلمات محظورة بالإنجليزية",
            message_type="transactional",
            country_code="US",
        ))
        assert r.result["legal"] is False
        assert r.result["rule"] == "restricted_content_jev_advisory"
        assert r.result["jev_advisory"]["checked"] is True
        assert r.result["jev_advisory"]["blocked"] is True
        assert r.reason_code == "not_compliant"
        assert r.status == OperationStatus.SUCCESS  # a truthful "no" is a successful check

    def test_jev_unavailable_leaves_compliant_verdict_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(available=False, blocked=None,
                                         probability=None, error="forced failure"),
        )
        r = run(handle_check_compliance(
            recipient_id="jane@example.com",
            content="Your appointment is confirmed for Tuesday 10:30am.",
            message_type="transactional",
            country_code="US",
        ))
        assert r.result["legal"] is True
        assert r.result["rule"] is None
        assert r.result["jev_advisory"]["checked"] is False

    def test_jev_says_clean_leaves_compliant_verdict_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(available=True, blocked=False,
                                         probability=0.03, error=None),
        )
        r = run(handle_check_compliance(
            recipient_id="jane@example.com",
            content="Your appointment is confirmed for Tuesday 10:30am.",
            message_type="transactional",
            country_code="US",
        ))
        assert r.result["legal"] is True
        assert r.result["jev_advisory"]["checked"] is True
        assert r.result["jev_advisory"]["blocked"] is False

    def test_jev_is_never_consulted_when_regex_already_blocks(self, monkeypatch):
        """The union rule 'regex BLOCK -> BLOCK regardless of jev' is
        structural, not incidental: check_compliance must not even call jev
        once compliance.pre_check has already raised for restricted_content
        - both because there is nothing left for jev to add, and because
        every call has a real (if tiny) cost."""
        def _must_not_be_called(content):
            raise AssertionError("jev must not be consulted when regex already blocked")
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            _must_not_be_called,
        )
        r = run(handle_check_compliance(
            recipient_id="+14045550100",
            content="Join our casino! Place your bets and win big money",
            channel="sms",
            message_type="transactional",
            country_code="US",
        ))
        assert r.result["legal"] is False
        assert r.result["rule"] == "restricted_content"  # the DETERMINISTIC rule, not jev's
        assert "jev_advisory" not in r.result  # never reached that branch at all

    def test_jev_advisory_never_downgrades_a_block_to_compliant(self, monkeypatch):
        """There is no code path that reads advisory.blocked is False and
        clears an existing block - confirmed structurally above (jev is
        skipped whenever the gate already blocked), and here for the
        remaining case: a non-content rule (e.g. opt-out) blocks first."""
        from compliance.consent_store import get_consent_store
        get_consent_store().mark_opted_out("+14045559999", "sms")
        try:
            def _must_not_be_called(content):
                raise AssertionError("jev must not run once ANY block already fired")
            monkeypatch.setattr(
                "core.check_compliance.get_restricted_category_advisory",
                _must_not_be_called,
            )
            r = run(handle_check_compliance(
                recipient_id="+14045559999",
                content="Reminder: your appointment is tomorrow.",
                channel="sms",
                message_type="transactional",
                country_code="US",
            ))
            assert r.result["legal"] is False
            assert r.result["rule"] == "recipient_opted_out"
        finally:
            pass  # opt-out store is process-local test state; no teardown API exposed


# ---------------------------------------------------------------------------
# Board row 282: binary resolution (JEV_BIN -> PATH -> dev fallback).
#
# The original code hardcoded a laptop-only absolute path as the ONLY way to
# invoke jev, so the advisory was permanently inert on any other host with no
# visible sign of it (every failure, including "file does not exist",
# collapsed into the same fail-safe available=False). These tests pin the
# fix: resolve_jev_binary() is pure path/env logic (no subprocess, no
# network, never raises), so it is exercised directly here with no
# HATCHLOOP_ALLOW_LIVE needed at all.
# ---------------------------------------------------------------------------

class TestResolveJevBinary:
    def test_nothing_configured_is_binary_not_found(self, monkeypatch):
        monkeypatch.delenv("JEV_BIN", raising=False)
        monkeypatch.setattr(jev_advisory.shutil, "which", lambda name: None)
        monkeypatch.setattr(jev_advisory, "_DEV_FALLBACK_SCRIPT",
                             "Z:/does/not/exist/jev-nope")
        argv, reason = jev_advisory.resolve_jev_binary()
        assert argv is None
        assert reason is not None
        assert "binary not found" in reason

    def test_jev_bin_pointing_at_nothing_is_binary_not_found_with_reason(self, monkeypatch):
        monkeypatch.setenv("JEV_BIN", "C:/definitely/not/a/real/path/jev.exe")
        argv, reason = jev_advisory.resolve_jev_binary()
        assert argv is None
        assert "binary not found" in reason
        assert "JEV_BIN" in reason

    def test_jev_bin_pointing_at_a_real_file_resolves(self, monkeypatch):
        monkeypatch.setenv("JEV_BIN", __file__)
        argv, reason = jev_advisory.resolve_jev_binary()
        assert reason is None
        assert argv == [__file__]

    def test_path_resolution_used_when_no_jev_bin_set(self, monkeypatch):
        monkeypatch.delenv("JEV_BIN", raising=False)
        monkeypatch.setattr(
            jev_advisory.shutil, "which",
            lambda name: "/fake/path/to/jev" if name == "jev" else None,
        )
        argv, reason = jev_advisory.resolve_jev_binary()
        assert reason is None
        assert argv == ["/fake/path/to/jev"]

    def test_dev_fallback_used_only_when_env_and_path_both_fail(self, monkeypatch):
        monkeypatch.delenv("JEV_BIN", raising=False)
        monkeypatch.setattr(jev_advisory.shutil, "which", lambda name: None)
        # The dev-fallback script is a laptop-only file
        # (compliance/jev_advisory.py's _DEV_FALLBACK_SCRIPT, board row 282's
        # last-resort path) that does not exist in CI or in production - it
        # is not a real dependency of this test suite, it is the fixture.
        # When it is genuinely absent, resolve_jev_binary() correctly
        # reports "binary not found" (see TestBinaryNotFoundIsVisibleAndHarmless
        # for that contract, pinned independently of this file's presence).
        # That is "dependency unavailable", not "our resolution logic is
        # broken" - so skip with a reason instead of failing, the same
        # distinction the rest of this module draws between "jev did not
        # run" and "jev ran and failed".
        if not os.path.isfile(jev_advisory._DEV_FALLBACK_SCRIPT):
            pytest.skip(
                "dev fallback script not present on this host "
                f"({jev_advisory._DEV_FALLBACK_SCRIPT!r}) - the jev-binary "
                "dev-fallback resolution path is untested in this "
                "environment (this is a missing local dependency, not a "
                "regression in resolve_jev_binary())"
            )
        # The real dev-fallback file happens to exist on THIS laptop; this
        # test proves it is used only as the LAST resort, not stubbed.
        argv, reason = jev_advisory.resolve_jev_binary()
        assert reason is None
        assert argv == ["python", jev_advisory._DEV_FALLBACK_SCRIPT]

    def test_jev_bin_takes_priority_over_path(self, monkeypatch):
        """JEV_BIN is an explicit operator override - it must win even when
        PATH resolution would also have succeeded, so an operator can pin a
        specific binary without fighting whatever else is on PATH."""
        monkeypatch.setenv("JEV_BIN", __file__)
        monkeypatch.setattr(
            jev_advisory.shutil, "which",
            lambda name: "/should/not/be/used" if name == "jev" else None,
        )
        argv, reason = jev_advisory.resolve_jev_binary()
        assert reason is None
        assert argv == [__file__]


# ---------------------------------------------------------------------------
# Board row 282: the proof requested in the board task itself.
#
#   1. With JEV_BIN pointing at something nonexistent, the advisory reports
#      unavailable WITH a reason, and the deterministic gate result is
#      byte-identical to the jev-available case.
#   2. Availability is reported truthfully when the binary resolves (see
#      TestResolveJevBinary above + TestUnionWiringInCheckCompliance's
#      existing "jev block flips" / "jev unavailable leaves unchanged"
#      tests, which already cover the available=True path end to end).
#   3. "Did not run" and "ran, found nothing" are now DISTINGUISHABLE. Before
#      this fix, a hardcoded path that does not exist on a given host fell
#      through to the exact same generic failure bucket
#      (core.check_compliance._jev_unavailable_note's "exit "/unmatched
#      catch-all -> "the read failed") as any other transient subprocess
#      failure. There was no way for a caller, a log reader, or a health
#      check to tell "this will never work on this host" from "hiccup, try
#      again". These tests prove the note text now differs.
# ---------------------------------------------------------------------------

class TestBinaryNotFoundIsVisibleAndHarmless:
    def test_jev_bin_nonexistent_reports_unavailable_with_a_reason_no_subprocess(self, monkeypatch):
        """Exercises the REAL resolve_jev_binary() (not mocked) through the
        REAL get_restricted_category_advisory, past the test-runner gate via
        HATCHLOOP_ALLOW_LIVE=1. subprocess.run is patched only to prove it is
        NEVER reached - resolution must fail before any process is spawned."""
        monkeypatch.setenv("HATCHLOOP_ALLOW_LIVE", "1")
        monkeypatch.setenv("JEV_BIN", "C:/definitely/not/a/real/path/jev.exe")

        def _must_not_be_called(cmd, **kwargs):
            raise AssertionError(
                "resolution failed - subprocess.run must never be attempted")
        monkeypatch.setattr(subprocess, "run", _must_not_be_called)

        r = get_restricted_category_advisory("some ordinary content")
        assert r.available is False
        assert r.blocked is None
        assert r.error is not None
        assert "binary not found" in r.error
        assert "JEV_BIN" in r.error

    def test_deterministic_gate_result_is_identical_whether_jev_resolves_or_not(self, monkeypatch):
        """Compare handle_check_compliance's DETERMINISTIC fields between a
        run where jev IS available and says clean, and a run where the
        binary cannot be resolved at all - proving the deterministic gate
        genuinely does not care which happened; only result.jev_advisory
        differs."""
        content = "Your appointment is confirmed for Tuesday 10:30am."
        kwargs = dict(recipient_id="jane@example.com", content=content,
                       message_type="transactional", country_code="US")

        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(available=True, blocked=False,
                                         probability=0.02, error=None),
        )
        r_available = run(handle_check_compliance(**kwargs))

        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(
                available=False, blocked=None, probability=None,
                error="jev binary not found: JEV_BIN='nope' does not exist "
                      "and is not resolvable on PATH"),
        )
        r_unresolved = run(handle_check_compliance(**kwargs))

        for field in ("legal", "rule", "jurisdiction", "channel", "message_type"):
            assert r_available.result[field] == r_unresolved.result[field], (
                f"deterministic field {field!r} differs between jev-available "
                f"and jev-unresolved: {r_available.result[field]!r} vs "
                f"{r_unresolved.result[field]!r}")
        assert r_available.status == r_unresolved.status
        assert r_available.reason_code == r_unresolved.reason_code
        assert r_available.human_message == r_unresolved.human_message

        # The one place they're SUPPOSED to differ: the advisory says so.
        assert r_available.result["jev_advisory"]["checked"] is True
        assert r_unresolved.result["jev_advisory"]["checked"] is False

    def test_binary_not_found_note_differs_from_a_generic_transient_failure(self, monkeypatch):
        """THE distinguishability proof: 'jev never even ran here' (binary
        not found) must produce a DIFFERENT note than 'jev ran and the call
        itself failed' (a transient exit-code/timeout/parse failure) - both
        used to fall into the same catch-all bucket."""
        content = "Your appointment is confirmed for Tuesday 10:30am."
        kwargs = dict(recipient_id="jane@example.com", content=content,
                       message_type="transactional", country_code="US")

        # State A: jev did not run at all (binary not found).
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(
                available=False, blocked=None, probability=None,
                error="jev binary not found: JEV_BIN='nope' does not exist "
                      "and is not resolvable on PATH"),
        )
        r_not_found = run(handle_check_compliance(**kwargs))

        # State B: jev ran, but the call itself failed (a real transient
        # failure shape - the exit-code branch in jev_advisory.py).
        monkeypatch.setattr(
            "core.check_compliance.get_restricted_category_advisory",
            lambda content: JevAdvisory(
                available=False, blocked=None, probability=None,
                error="exit 2: jev: API error (HTTP 503): upstream unavailable"),
        )
        r_transient = run(handle_check_compliance(**kwargs))

        note_not_found = r_not_found.result["jev_advisory"]["note"]
        note_transient = r_transient.result["jev_advisory"]["note"]

        assert note_not_found != note_transient, (
            "binary-not-found and a transient call failure produced the SAME "
            "note - the two states are indistinguishable")
        assert "not installed/configured on this host" in note_not_found
        assert "not installed/configured on this host" not in note_transient

        # Both still leave the deterministic verdict alone - "did not run"
        # is never misreported as "ran, found nothing" (checked=True).
        assert r_not_found.result["jev_advisory"]["checked"] is False
        assert r_transient.result["jev_advisory"]["checked"] is False
        assert r_not_found.result["legal"] is True
        assert r_transient.result["legal"] is True
