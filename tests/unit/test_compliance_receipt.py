"""A receipt is evidence, so the ways it can lie are the tests.

The compliance receipt is the one artefact a customer keeps and later produces.
Everything that makes it worth keeping is a claim ABOUT ITSELF - that it has
not been altered, that it was issued by us, that it belongs to the answer it
came with - and a claim about itself is the easiest kind to get wrong quietly.

Three failures would each be worse than shipping nothing:

  1. A receipt that says "signed" when no key was configured. The customer
     files it, relies on it, and finds out in front of someone who checked.
  2. A receipt that still verifies after someone edited it.
  3. A receipt that changes the answer, the status or the cost of the free
     read-only check it is attached to.

Plus the quieter one this whole codebase exists to prevent: a receipt that
asserts something we cannot know. The record states what OUR SYSTEM did -
which lists, which copies, which gate, when - and never that a party is clean.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

import core.compliance_receipt as CR
import core.screen_sanctions as ss
from core.check_compliance import handle_check_compliance
from core.models import OperationStatus

# Two distinct 32-byte Ed25519 seeds, hex-encoded. Test-only values; the real
# key is an environment secret and never lives in the repo.
KEY_A = "11" * 32
KEY_B = "a3" * 32


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setenv(CR._SIGNING_KEY_ENV, KEY_A)
    monkeypatch.delenv(CR._KEY_ID_ENV, raising=False)
    return CR.signing_key_status()


@pytest.fixture
def unsigned(monkeypatch):
    monkeypatch.delenv(CR._SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv(CR._KEY_ID_ENV, raising=False)
    return CR.signing_key_status()


def _a_receipt(**over):
    kw = dict(
        tool="check_compliance",
        operation_id="op-1",
        subject={"recipient_id": "+14045550100"},
        inputs={"content": "hello"},
        evidence={"decision": {"permitted": True}},
        asserts="We ran the gate.",
        does_not_assert=["It is not legal advice."],
        response_payload={"legal": True},
    )
    kw.update(over)
    return CR.build_receipt(**kw)


# ---------------------------------------------------------------------------
# Canonicalization -- the bytes everything else rests on
# ---------------------------------------------------------------------------

class TestCanonicalization:
    def test_key_order_does_not_change_the_bytes(self):
        """The receipt travels as JSON through parsers that do not promise key
        order. If the digest depended on the order it arrived in, every honest
        round trip would look like tampering."""
        assert (CR.canonical_bytes({"b": 1, "a": {"d": 2, "c": 3}})
                == CR.canonical_bytes({"a": {"c": 3, "d": 2}, "b": 1}))

    def test_the_bytes_are_pure_ascii(self):
        """ensure_ascii is what lets a verifier in another language skip the
        question of whose Unicode normalisation is correct."""
        body = CR.canonical_bytes({"name": "Zarußežneft 中"})
        assert body.decode("ascii")            # raises if a byte is >= 0x80

    def test_non_finite_numbers_are_refused_not_silently_written(self):
        """NaN is not JSON. Writing it would produce a receipt whose bytes
        depend on the reader's JSON dialect, which cannot be verified."""
        with pytest.raises(ValueError):
            CR.canonical_bytes({"score": float("nan")})

    def test_the_signed_payload_contains_no_floats(self):
        """Float formatting is the one place our canonical form is not
        trivially portable (see the module docstring). Keeping floats out of
        the signed payload confines that caveat to response_sha256."""
        rec = _a_receipt()

        def _walk(node, path="payload"):
            if isinstance(node, float):
                pytest.fail(f"float in the signed payload at {path}: {node!r}")
            if isinstance(node, dict):
                for k, v in node.items():
                    _walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    _walk(v, f"{path}[{i}]")

        _walk(rec["payload"])

    def test_content_digest_is_the_one_sha256sum_would_give(self):
        """A digest a customer cannot reproduce with the obvious tool is a
        digest they will not trust."""
        assert CR.sha256_text("hello") == (
            "sha256:" + hashlib.sha256(b"hello").hexdigest())


# ---------------------------------------------------------------------------
# The unsigned degradation -- the failure mode that would be worst
# ---------------------------------------------------------------------------

class TestUnsignedIsLabelledHonestly:
    def test_no_key_means_no_signature_and_it_says_so(self, unsigned):
        rec = _a_receipt()
        integ = rec["integrity"]
        assert integ["signature"] is None
        assert integ["signature_algorithm"] is None
        assert integ["signature_status"] == "unsigned"
        assert integ["unsigned_reason"] == "no_signing_key_configured"
        assert "NOT SIGNED" in integ["what_this_proves"]
        assert "does NOT prove" in integ["what_this_proves"]

    def test_the_verifier_never_calls_an_unsigned_receipt_verified_signed(
            self, unsigned):
        v = CR.verify_compliance_receipt(_a_receipt())
        assert v["verdict"] == "verified_unsigned_hash_only"
        assert v["signature_status"] == "absent"
        assert v["origin_proven"] is False
        assert v["tamper_evident"] is False, (
            "an unsigned receipt's hash is recomputable by whoever edited it; "
            "calling it tamper-evident is the lie this feature exists to stop")
        assert "UNSIGNED" in v["human_message"]

    def test_a_misconfigured_key_downgrades_loudly_and_never_claims_signed(
            self, monkeypatch):
        """A typo'd secret must not silently produce receipts that look fine,
        and must not take a free read-only tool off the air either."""
        monkeypatch.setenv(CR._SIGNING_KEY_ENV, "not-a-key")
        integ = _a_receipt()["integrity"]
        assert integ["signature_status"] == "unsigned"
        assert integ["signature"] is None
        assert integ["unsigned_reason"] == "signing_key_misconfigured"
        assert CR.signing_key_status()["reason"] == "signing_key_misconfigured"

    def test_a_wrong_length_key_is_a_misconfiguration_not_a_signature(
            self, monkeypatch):
        monkeypatch.setenv(CR._SIGNING_KEY_ENV, "ab" * 16)   # 16 bytes, not 32
        assert _a_receipt()["integrity"]["signature_status"] == "unsigned"

    def test_a_receipt_claiming_signed_with_no_signature_is_refused(
            self, unsigned):
        """The forgery this whole design is pointed at: flip the label, ship
        nothing to check. The verifier must reject rather than 'downgrade'."""
        rec = _a_receipt()
        rec["integrity"]["signature_status"] = "signed"
        v = CR.verify_compliance_receipt(rec)
        assert v["verdict"] == "malformed"
        assert any("no signature is present" in r for r in v["reasons"])


# ---------------------------------------------------------------------------
# The signed path, and what a signature does and does not prove
# ---------------------------------------------------------------------------

class TestSignedReceipts:
    def test_a_signed_receipt_verifies(self, signed):
        v = CR.verify_compliance_receipt(_a_receipt())
        assert v["verdict"] == "verified_signed"
        assert v["signature_status"] == "valid"
        assert v["tamper_evident"] is True

    def test_the_key_id_is_derived_from_the_key_not_invented(self, signed):
        rec = _a_receipt()
        pub = rec["integrity"]["public_key_ed25519_hex"]
        assert rec["integrity"]["key_id"] == (
            "ed25519-" + hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16])

    def test_origin_is_not_proven_by_the_key_the_receipt_carries(self, signed):
        """A forger can sign anything with their own keypair and embed the
        matching public key. Self-consistency is not provenance, and the two
        must not collapse into one boolean."""
        v = CR.verify_compliance_receipt(_a_receipt())
        assert v["signature_status"] == "valid"
        assert v["origin_proven"] is False
        assert any("out of band" in r for r in v["reasons"])

    def test_origin_is_proven_when_the_customer_pins_the_key(self, signed):
        rec = _a_receipt()
        pub = rec["integrity"]["public_key_ed25519_hex"]
        v = CR.verify_compliance_receipt(rec, expected_public_key_hex=pub)
        assert v["origin_proven"] is True
        assert v["verdict"] == "verified_signed"

    def test_a_receipt_signed_by_someone_else_fails_the_pin(self, monkeypatch):
        """The attack the pin defends against: a receipt that is internally
        perfect and simply was not issued by us."""
        monkeypatch.setenv(CR._SIGNING_KEY_ENV, KEY_B)
        forged = _a_receipt()
        monkeypatch.setenv(CR._SIGNING_KEY_ENV, KEY_A)
        ours = CR.signing_key_status()["public_key_ed25519_hex"]

        assert CR.verify_compliance_receipt(forged)["verdict"] == "verified_signed"
        v = CR.verify_compliance_receipt(forged, expected_public_key_hex=ours)
        assert v["verdict"] == "signature_invalid"
        assert v["origin_proven"] is False


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------

class TestTampering:
    def test_editing_a_field_breaks_the_hash(self, signed):
        rec = _a_receipt()
        bad = copy.deepcopy(rec)
        bad["payload"]["evidence"]["decision"]["permitted"] = False
        v = CR.verify_compliance_receipt(bad)
        assert v["verdict"] == "tampered"
        assert v["hash_ok"] is False

    def test_editing_and_recomputing_the_hash_still_breaks_the_signature(
            self, signed):
        """The obvious next move after failing the hash check. The hash is
        recomputable; the signature is not."""
        bad = copy.deepcopy(_a_receipt())
        bad["payload"]["subject"]["recipient_id"] = "+19998887777"
        bad["integrity"]["payload_sha256"] = "sha256:" + hashlib.sha256(
            CR.canonical_bytes(bad["payload"])).hexdigest()
        v = CR.verify_compliance_receipt(bad)
        assert v["hash_ok"] is True
        assert v["verdict"] == "signature_invalid"
        assert v["signature_status"] == "invalid"

    def test_an_unsigned_receipt_survives_edit_plus_rehash_and_says_why(
            self, unsigned):
        """Not a bug - the honest limit of a hash without a key, and the reason
        the unsigned block spells it out. The test pins that we never present
        this state as verified evidence of origin."""
        bad = copy.deepcopy(_a_receipt())
        bad["payload"]["subject"]["recipient_id"] = "+19998887777"
        bad["integrity"]["payload_sha256"] = "sha256:" + hashlib.sha256(
            CR.canonical_bytes(bad["payload"])).hexdigest()
        v = CR.verify_compliance_receipt(bad)
        assert v["verdict"] == "verified_unsigned_hash_only"
        assert v["origin_proven"] is False
        assert v["tamper_evident"] is False

    def test_editing_only_the_declared_hash_is_still_tampering(self, signed):
        """The signature covers `payload`, not the integrity block. A valid
        signature with a wrong declared hash means the record's own integrity
        block was edited - reporting that as verified_signed would put a
        headline on top of a field that contradicts it."""
        bad = copy.deepcopy(_a_receipt())
        bad["integrity"]["payload_sha256"] = "sha256:" + "0" * 64
        v = CR.verify_compliance_receipt(bad)
        assert v["verdict"] == "tampered"
        assert v["hash_ok"] is False
        assert v["signature_status"] == "valid"
        assert v["origin_proven"] is False

    def test_pinning_a_key_against_an_unsigned_receipt_says_so(self, unsigned):
        """A pin that is silently ignored is worse than a pin that fails."""
        v = CR.verify_compliance_receipt(_a_receipt(),
                                         expected_public_key_hex="ab" * 32)
        assert v["origin_proven"] is False
        assert any("no signature to check against it" in r
                   for r in v["reasons"])

    def test_truncation_is_caught(self, signed):
        bad = copy.deepcopy(_a_receipt())
        bad["payload"].pop("does_not_assert")
        assert CR.verify_compliance_receipt(bad)["verdict"] == "tampered"

    @pytest.mark.parametrize("junk", [None, "receipt", 42, [], {},
                                      {"payload": {}},
                                      {"payload": {}, "integrity": {}}])
    def test_junk_is_malformed_never_verified(self, junk):
        assert CR.verify_compliance_receipt(junk)["verdict"] == "malformed"


class TestOfflineVerifierEntryPoint:
    """The verification story only works if the customer can actually run it
    on the file they saved, with nothing from us."""

    def _write(self, tmp_path, doc, name="receipt.json"):
        p = tmp_path / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return str(p)

    def test_a_saved_tool_response_verifies_from_the_command_line(
            self, signed, tmp_path, capsys):
        payload = {"legal": True}
        CR.attach_receipt(payload, tool="check_compliance",
                          operation_id="op-1", subject={}, inputs={},
                          evidence={}, asserts="x", does_not_assert=[])
        rc = CR._main([self._write(tmp_path, payload)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["verdict"] == "verified_signed"
        assert out["response_match"] is True, (
            "the CLI was handed the whole saved response, so it must check the "
            "receipt against it and not just against itself")

    def test_a_tampered_file_exits_nonzero(self, signed, tmp_path, capsys):
        rec = _a_receipt()
        rec["payload"]["subject"]["recipient_id"] = "+10000000000"
        rc = CR._main([self._write(tmp_path, rec)])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == "tampered"

    def test_pinning_the_public_key_from_the_command_line(self, signed,
                                                          tmp_path, capsys):
        rec = _a_receipt()
        rc = CR._main([self._write(tmp_path, rec),
                       rec["integrity"]["public_key_ed25519_hex"]])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["origin_proven"] is True


class TestSourceFingerprint:
    def test_an_unreadable_source_is_not_cached_as_unavailable(self):
        """A transient read failure must not pin 'unavailable' into every
        later receipt for the life of the process."""
        class _Fake:
            __name__ = "not_a_real_module_for_fingerprinting"

        assert CR.source_fingerprint(_Fake()) == "unavailable"
        assert "not_a_real_module_for_fingerprinting" not in \
            CR._source_fingerprints

    def test_a_real_module_fingerprints_stably(self):
        import compliance.pre_check as pc
        first = CR.source_fingerprint(pc)
        assert first.startswith("sha256:")
        assert CR.source_fingerprint(pc) == first


class TestResponseBinding:
    def test_the_receipt_binds_to_the_answer_it_came_with(self, signed):
        rec = _a_receipt(response_payload={"legal": True, "rule": None})
        v = CR.verify_compliance_receipt(
            rec, response_payload={"legal": True, "rule": None})
        assert v["response_match"] is True

    def test_a_different_answer_does_not_match(self, signed):
        """Stops a real receipt being filed next to a different (or edited)
        result and passing as its proof."""
        rec = _a_receipt(response_payload={"legal": True, "rule": None})
        v = CR.verify_compliance_receipt(
            rec, response_payload={"legal": False, "rule": "TCPA"})
        assert v["response_match"] is False

    def test_the_receipt_key_is_ignored_when_binding(self, signed):
        """The receipt lives inside the payload it hashes, so the binding is
        defined over the payload WITHOUT it. Round-tripping the response the
        customer actually stored has to work."""
        payload = {"legal": True}
        CR.attach_receipt(payload, tool="check_compliance", operation_id="op-1",
                          subject={}, inputs={}, evidence={}, asserts="x",
                          does_not_assert=[])
        v = CR.verify_compliance_receipt(payload[CR.RECEIPT_FIELD],
                                         response_payload=payload)
        assert v["response_match"] is True


# ---------------------------------------------------------------------------
# check_compliance wiring
# ---------------------------------------------------------------------------

def _compliant():
    return _run(handle_check_compliance(
        recipient_id="jane@example.com",
        content="Your appointment at Cuts & Co. is confirmed for Tuesday 10:30am.",
        message_type="transactional", country_code="US"))


def _blocked():
    return _run(handle_check_compliance(
        recipient_id="+14045550200", content="20% off this week only!",
        channel="sms", message_type="marketing", country_code="US"))


class TestCheckComplianceWiring:
    def test_both_decisions_carry_a_receipt(self, unsigned):
        for r in (_compliant(), _blocked()):
            rec = r.result[CR.RECEIPT_FIELD]
            assert rec["payload"]["tool"] == "check_compliance"
            assert CR.verify_compliance_receipt(
                rec, response_payload=r.result)["hash_ok"] is True

    def test_a_blocked_send_records_the_rule_that_blocked_it(self, unsigned):
        r = _blocked()
        decision = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["decision"]
        assert decision["permitted"] is False
        assert decision["rule"] == r.result["rule"] == "TCPA_marketing_consent"

    def test_the_receipt_agrees_with_the_answer_it_is_attached_to(self, unsigned):
        for r in (_compliant(), _blocked()):
            ev = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]
            assert ev["decision"]["permitted"] is r.result["legal"]

    def test_the_operation_id_ties_the_receipt_to_the_outcome(self, unsigned):
        r = _compliant()
        assert r.result[CR.RECEIPT_FIELD]["payload"]["operation_id"] == \
            r.operation_id

    def test_the_message_body_is_digested_not_copied(self, unsigned):
        """The caller already holds the body. A receipt that reproduces it is a
        second copy of the customer's data in a file they will forward."""
        body = "Your appointment at Cuts & Co. is confirmed for Tuesday 10:30am."
        r = _compliant()
        blob = json.dumps(r.result[CR.RECEIPT_FIELD])
        assert body not in blob
        assert r.result[CR.RECEIPT_FIELD]["payload"]["subject"][
            "content_sha256"] == CR.sha256_text(body)

    def test_bad_input_gets_no_receipt(self, unsigned):
        """A record of a check that never ran is noise in an evidence file."""
        r = _run(handle_check_compliance(recipient_id="", content="hi"))
        assert r.status == OperationStatus.FAILURE
        assert CR.RECEIPT_FIELD not in (r.result or {})

    def test_the_ruleset_is_identified_by_something_that_cannot_drift(
            self, unsigned):
        r = _compliant()
        rs = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["ruleset"]
        assert rs["gate_source_sha256"].startswith("sha256:")
        assert rs["jurisdiction_rules_source_sha256"].startswith("sha256:")
        assert rs["jurisdiction_applied"] == "US"
        assert rs["jurisdiction_supplied_by_caller"] is True
        # The concrete rules that governed the decision, as data.
        assert rs["rules_applied"]["can_spam_applies"] is True

    def test_a_defaulted_jurisdiction_is_recorded_as_defaulted(self, unsigned):
        """"US because the caller said US" and "US because we had to pick
        something" are different facts about the same decision."""
        r = _run(handle_check_compliance(
            recipient_id="jane@example.com", content="Your booking is confirmed.",
            message_type="transactional"))
        rs = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["ruleset"]
        assert rs["jurisdiction_supplied_by_caller"] is False

    def test_the_receipt_states_what_it_does_not_assert(self, unsigned):
        p = _compliant().result[CR.RECEIPT_FIELD]["payload"]
        text = " ".join(p["does_not_assert"]).lower()
        assert "preview" in text
        assert "not legal advice" in text
        assert "recording" in text, (
            "the voice-recording carve-out is in the tool's own answer and has "
            "to travel with the evidence too")


class TestUnderlyingBehaviourIsUnchanged:
    """The receipt is an addition. Nothing about the answer may move."""

    def _without_receipts(self, monkeypatch, fn):
        # PATCH THE NAME THE HANDLER READS, not the definition site.
        # check_compliance does `from core.compliance_receipt import
        # attach_receipt`, so patching core.compliance_receipt would leave the
        # real function bound in the handler and this test would pass by
        # comparing two identical receipt-carrying runs.
        import core.check_compliance as CC
        monkeypatch.setattr(CC, "attach_receipt", lambda payload, **kw: None)
        return fn()

    @pytest.mark.parametrize("fn", [_compliant, _blocked])
    def test_the_answer_is_byte_identical_once_the_receipt_is_removed(
            self, monkeypatch, unsigned, fn):
        with_r = fn()
        without = self._without_receipts(monkeypatch, fn)

        assert CR.RECEIPT_FIELD not in without.result
        stripped = {k: v for k, v in with_r.result.items()
                    if k != CR.RECEIPT_FIELD}
        assert stripped == without.result
        assert with_r.status == without.status
        assert with_r.reason_code == without.reason_code
        assert with_r.human_message == without.human_message
        assert with_r.next_actions == without.next_actions
        assert with_r.cost.amount == without.cost.amount == 0.0
        assert with_r.cost.basis == without.cost.basis == "free"

    def test_a_broken_receipt_builder_does_not_break_the_check(
            self, monkeypatch, unsigned):
        """Evidence generation must not add a failure path to a free tool. If
        it cannot be built the receipt is simply absent, and absence asserts
        nothing."""
        def _boom(*a, **kw):
            raise RuntimeError("signer exploded")

        monkeypatch.setattr(CR, "build_receipt", _boom)
        r = _compliant()
        assert r.status == OperationStatus.SUCCESS
        assert r.reason_code == "compliant"
        assert r.result["legal"] is True
        assert r.cost.amount == 0.0
        assert CR.RECEIPT_FIELD not in r.result

    def test_the_receipt_never_appears_in_the_human_message(self, unsigned):
        """An LLM caller summarises human_message. The receipt is for the file,
        not for the sentence."""
        for r in (_compliant(), _blocked()):
            assert "compliance_receipt" not in (r.human_message or "")


# ---------------------------------------------------------------------------
# screen_sanctions wiring
# ---------------------------------------------------------------------------

class _Rows:
    """Stand in for the index so these tests do not touch the network."""

    def __init__(self, by_list: dict):
        self.by_list = by_list

    async def select_rows_strict(self, table, filters=None, limit=None, **kw):
        filters = filters or {}
        code = filters.get("list_code")
        if "name_key" in filters:
            return [r for r in self.by_list.get(code, [])
                    if r["name_key"] == filters["name_key"]]
        return []

    async def select_rows(self, table, filters=None, limit=None, **kw):
        code = (filters or {}).get("list_code")
        return self.by_list.get(code, [])[:1]


@pytest.fixture
def _index(monkeypatch):
    """A fresh EU/UK index and a reachable-but-empty OFAC. No network."""
    rows = _Rows({
        "EU": [{"name_key": "rosneft", "tokens": ["rosneft"],
                "display_name": "ROSNEFT", "programme": "UKR",
                "etype": "ENTITY", "countries": ["RU"]}],
        # A row nothing in these tests matches. It has to be NON-EMPTY: an
        # empty index is treated as "not loaded" and moves UK into
        # sources_unavailable, which would make every screen here `partial`
        # and quietly test the outage path instead of the clean one.
        "UK": [{"name_key": "unrelated holdings", "tokens": ["unrelated",
                                                            "holdings"],
                "display_name": "UNRELATED HOLDINGS", "programme": "X",
                "etype": "ENTITY", "countries": ["GB"]}],
    })
    import storage.supabase_client as sb
    monkeypatch.setattr(sb, "select_rows_strict", rows.select_rows_strict)
    monkeypatch.setattr(sb, "select_rows", rows.select_rows)

    async def _fresh(code):
        # DATE ONLY, exactly as _list_refreshed_at returns it in production.
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    monkeypatch.setattr(ss, "_list_refreshed_at", _fresh)

    async def _no_ofac(name):
        return [], ["OFAC-SDN"], []

    monkeypatch.setattr(ss, "_call_ofac_sdn", _no_ofac)
    return rows


def _prov(receipt, list_name):
    for e in receipt["payload"]["evidence"]["data_provenance"]:
        if e["list"].startswith(list_name):
            return e
    raise AssertionError(f"{list_name} missing from data_provenance")


class TestScreenSanctionsWiring:
    def test_a_screen_carries_a_verifiable_receipt(self, unsigned, _index):
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        rec = r.result[CR.RECEIPT_FIELD]
        assert rec["payload"]["tool"] == "screen_sanctions"
        v = CR.verify_compliance_receipt(rec, response_payload=r.result)
        assert v["hash_ok"] is True and v["response_match"] is True

    def test_the_freshness_of_every_index_is_on_the_record(self, unsigned,
                                                           _index):
        """The field an auditor asks about first: not what it said, but what it
        was screened against and how old that copy was."""
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        rec = r.result[CR.RECEIPT_FIELD]
        eu = _prov(rec, "EU-CONSOLIDATED")
        assert eu["index_refreshed_on"] == datetime.now(
            timezone.utc).strftime("%Y-%m-%d")
        assert eu["index_age_days"] == 0
        assert eu["max_index_age_days"] == ss._STALE_AFTER_DAYS == 7
        assert eu["within_freshness_limit"] is True
        assert eu["screened_on_this_call"] is True
        assert eu["publisher"] and eu["licence"]

    def test_a_stale_index_is_recorded_as_not_screened(self, unsigned, _index,
                                                       monkeypatch):
        """screen_sanctions refuses to answer from an index older than 7 days.
        The receipt has to say the list did not run, not merely how old it was
        - a reader who skims 'index_age_days: 40' and nothing else would take
        the screen as covering the EU."""
        async def _ancient(code):
            return "2020-01-01"

        monkeypatch.setattr(ss, "_list_refreshed_at", _ancient)
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        eu = _prov(r.result[CR.RECEIPT_FIELD], "EU-CONSOLIDATED")
        assert eu["screened_on_this_call"] is False
        assert eu["within_freshness_limit"] is False
        assert eu["index_age_days"] > 7

    def test_an_unreadable_age_is_unknown_not_fresh(self, unsigned, _index,
                                                    monkeypatch):
        """"We could not read the age" and "it is within the limit" are
        different facts. None, not False, and not screened."""
        async def _unknown(code):
            return None

        monkeypatch.setattr(ss, "_list_refreshed_at", _unknown)
        ss._age_cache.pop("EU", None)
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        eu = _prov(r.result[CR.RECEIPT_FIELD], "EU-CONSOLIDATED")
        assert eu["index_refreshed_on"] is None
        assert eu["within_freshness_limit"] is None
        assert eu["screened_on_this_call"] is False

    def test_the_list_we_do_not_screen_is_named_in_the_evidence(self, unsigned,
                                                                _index):
        """A reader deciding whether this receipt covers their obligation needs
        the boundary as plainly as the coverage."""
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        un = _prov(r.result[CR.RECEIPT_FIELD], "UN-CONSOLIDATED")
        assert un["screened_on_this_call"] is False
        assert "licence" in un["reason_not_screened"]

    def test_the_receipt_never_says_the_party_is_clean(self, unsigned, _index):
        """The single claim this record must never make. It reports what was
        screened; whether the party is sanctioned is not ours to state."""
        r = _run(ss.handle_screen_sanctions("Zzzq Nonexistent Holdings Ltd"))
        p = r.result[CR.RECEIPT_FIELD]["payload"]
        assert r.result["screening_status"] == "clean"
        assert "does NOT assert that the subject is not sanctioned" in \
            " ".join(p["does_not_assert"])
        # Scan everything EXCEPT does_not_assert, which is where these phrases
        # are supposed to appear - as refusals.
        blob = json.dumps({k: v for k, v in p.items()
                           if k != "does_not_assert"}).lower()
        for lie in ("is not sanctioned", "not on any sanctions",
                    "cleared for", "no sanctions exposure", "is clean",
                    "no sanctions found"):
            assert lie not in blob, f"the receipt asserts '{lie}'"

    def test_the_outcome_mirrors_the_answer_not_a_second_opinion(self, unsigned,
                                                                 _index):
        r = _run(ss.handle_screen_sanctions("Rosneft"))
        out = r.result[CR.RECEIPT_FIELD]["payload"]["evidence"]["outcome"]
        assert out["screening_status"] == r.result["screening_status"] == \
            "candidates"
        assert out["confirmed_matches"] == len(r.result["matches"]) == 0
        assert out["unverified_candidates"] == len(
            r.result["possible_matches_unverified"]) == 1

    def test_candidates_are_counted_but_not_named(self, unsigned, _index):
        """A candidate is a name coincidence from an uncalibrated matcher.
        Writing those names into an evidence file is how a coincidence becomes
        an allegation about a real company."""
        r = _run(ss.handle_screen_sanctions("Rosneft"))
        p = r.result[CR.RECEIPT_FIELD]["payload"]
        assert p["evidence"]["outcome"]["unverified_candidates"] == 1
        assert "ROSNEFT" not in json.dumps(p["evidence"]["outcome"])

    def test_a_screen_that_did_not_happen_is_not_recorded_as_one(
            self, unsigned, _index):
        """The weak-name path reduces the screen to exact whole-name lookups
        and sets screening_status=not_screened. No list may be marked as
        covered on that call."""
        r = _run(ss.handle_screen_sanctions("GRU"))
        assert r.result["screening_status"] == "not_screened"
        rec = r.result[CR.RECEIPT_FIELD]
        assert all(not e["screened_on_this_call"]
                   for e in rec["payload"]["evidence"]["data_provenance"])

    def test_the_screen_is_unchanged_once_the_receipt_is_removed(
            self, monkeypatch, unsigned, _index):
        with_r = _run(ss.handle_screen_sanctions("Rosneft"))
        # PATCH THE NAME THE HANDLER ACTUALLY READS. screen_sanctions does
        # `from core.compliance_receipt import attach_receipt` at module level,
        # so patching the definition site would leave the real function bound
        # here and the "without" run would be identical to the "with" run for
        # the wrong reason - a green test proving nothing.
        monkeypatch.setattr(ss, "attach_receipt", lambda payload, **kw: None)
        without = _run(ss.handle_screen_sanctions("Rosneft"))

        assert CR.RECEIPT_FIELD not in without.result
        stripped = {k: v for k, v in with_r.result.items()
                    if k not in (CR.RECEIPT_FIELD, "screened_at")}
        assert stripped == {k: v for k, v in without.result.items()
                            if k != "screened_at"}
        assert with_r.reason_code == without.reason_code
        assert with_r.human_message == without.human_message
        assert with_r.cost.amount == without.cost.amount == 0.0

    def test_a_broken_receipt_builder_does_not_break_the_screen(
            self, monkeypatch, unsigned, _index):
        def _boom(*a, **kw):
            raise RuntimeError("signer exploded")

        monkeypatch.setattr(CR, "build_receipt", _boom)
        r = _run(ss.handle_screen_sanctions("Rosneft"))
        assert r.status == OperationStatus.SUCCESS
        assert r.result["screening_status"] == "candidates"
        assert r.cost.amount == 0.0
        assert CR.RECEIPT_FIELD not in r.result

    def test_signing_covers_the_screening_tool_too(self, signed, _index):
        r = _run(ss.handle_screen_sanctions("Rosneft"))
        rec = r.result[CR.RECEIPT_FIELD]
        pub = rec["integrity"]["public_key_ed25519_hex"]
        v = CR.verify_compliance_receipt(rec, expected_public_key_hex=pub,
                                         response_payload=r.result)
        assert v["verdict"] == "verified_signed"
        assert v["origin_proven"] is True
        assert v["response_match"] is True
