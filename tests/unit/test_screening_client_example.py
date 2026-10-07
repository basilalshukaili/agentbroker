"""Synthetic local screens and saved MCP answers; no network or production."""
import asyncio
import json
from pathlib import Path
import socket

import pytest

from core.compliance_receipt import sha256_of, verify_compliance_receipt
from examples.screening_client import consume_screening, main

FIXTURES = Path(__file__).parents[1] / "fixtures" / "screening_client"
CASES = {
    "complete": ({}, "clean", "complete", []),
    "partial": ({"missing": True}, "partial", "partial", ["incomplete_coverage"]),
    "candidate_partial": ({"candidate": True, "missing": True}, "candidates", "partial",
                          ["unverified_candidates", "incomplete_coverage"]),
    "stale_unknown": ({"stale": True, "unknown": True}, "clean", "complete",
                      ["unknown_list_freshness", "stale_list_copy"]),
    "arabic": ({"arabic": True}, None, "complete", ["lossy_transliteration"]),
    "hit": ({"hit": True}, "hit", "complete", ["confirmed_name_match"]),
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline example attempted a network connection")
    # Windows asyncio creates a local socket pair while constructing its loop.
    # Block external client construction and DNS without breaking that pipe.
    import httpx
    monkeypatch.setattr(httpx, "Client", deny)
    monkeypatch.setattr(httpx, "AsyncClient", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.delenv("COMPLIANCE_RECEIPT_SIGNING_KEY", raising=False)


def synthetic_call(monkeypatch, flags):
    """Run the real handler with synthetic list adapters and no external sources."""
    import core.screen_sanctions as ss
    import core.compliance_receipt as cr
    # Do not inherit a configured issuer key, even under a different env name.
    monkeypatch.setattr(cr, "_load_key", lambda: {
        "mode": "unsigned", "reason": "no_signing_key_configured",
        "private": None, "public_hex": None, "key_id": None})

    async def ofac(name):
        matches = []
        if flags.get("candidate") or flags.get("hit"):
            matches = [{"name": "Example Export" if flags.get("hit") else "Example Export Group",
                        "list": "OFAC-SDN", "match_score": 0.8,
                        "_matcher": "local_word_overlap", "source_url": "https://example.invalid/list"}]
        return matches, ["OFAC"], []

    async def database(name, code, *args):
        return [], [code], (["UK unavailable"] if code == "UK" and flags.get("missing") else [])

    async def provenance(ofac_ok, eu_ok, uk_ok):
        return [
            {"list": "OFAC-SDN", "screened_on_this_call": ofac_ok,
             "refresh_state": "stale_copy_after_failed_refresh" if flags.get("stale") else "refreshed_within_ttl"},
            {"list": "EU-CONSOLIDATED", "screened_on_this_call": eu_ok,
             "within_freshness_limit": None if flags.get("unknown") else True},
            {"list": "UK-SANCTIONS", "screened_on_this_call": uk_ok, "within_freshness_limit": True},
            {"list": "UN-CONSOLIDATED (UN Security Council)", "screened_on_this_call": False,
             "reason_not_screened": "Excluded"},
        ]

    monkeypatch.setattr(ss, "_call_ofac_sdn", ofac)
    monkeypatch.setattr(ss, "_screen_list_db", database)
    monkeypatch.setattr(ss, "_data_provenance", provenance)
    name = "محمد عبدالله أحمد" if flags.get("arabic") else "Example Export"
    # Run the real dispatch/label seam; only its operation adapter is replaced.
    from agent_interface import mcp_server
    async def dispatch(tool, arguments, headers, skip_auth=False):
        assert tool == "screen_sanctions"
        return (await ss.handle_screen_sanctions(arguments["name"])).model_dump(mode="json")
    monkeypatch.setattr(mcp_server, "_dispatch_operation", dispatch)
    body = asyncio.run(mcp_server._dispatch_and_label("screen_sanctions", {"name": name}, {}))
    # Standard /mcp free path serializes OutcomeReceipt into content[0].text.
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": False}


def saved(case):
    return json.loads((FIXTURES / (case + ".json")).read_text(encoding="utf-8"))


def body(call):
    return json.loads(call["content"][0]["text"])


@pytest.mark.parametrize("case", CASES)
def test_synthetic_handler_and_saved_fixture_contract(case, monkeypatch):
    flags, status, coverage, reasons = CASES[case]
    fresh = synthetic_call(monkeypatch, flags)
    for call in (fresh, saved(case)):
        output = consume_screening(call)
        result = body(call)["result"]
        binding_ok = verify_compliance_receipt(
            result["compliance_receipt"], response_payload=result)["response_match"]
        assert bool(output["issues"]) is (not binding_ok)
        if status:
            assert output["screening_status"] == status
        assert output["review_guidance"]["coverage_status"] == coverage
        assert output["review_guidance"]["review_reasons"] == reasons
        assert output["receipt_verification"]["origin_proven"] is False
        assert output["receipt_verification"]["hash_ok"] is True
        assert output["receipt_verification"]["response_match"] is binding_ok
        assert output["guidance_response_bound"] is binding_ok
        assert output["decision"] == ("record_supported_list_screen" if case == "complete" else "hold_for_review")
    assert body(fresh)["result"]["review_guidance"] == body(saved(case))["result"]["review_guidance"]


def test_sdk_and_jsonrpc_wrappers():
    call = saved("complete")
    call["structuredContent"] = body(call)
    class SDKResult:
        def model_dump(self, mode):
            assert mode == "json"
            return call
    assert consume_screening(SDKResult())["decision"] == "record_supported_list_screen"
    assert consume_screening({"jsonrpc": "2.0", "id": 1, "result": call})["issues"] == []
    call["structuredContent"]["status"] = "failure"
    assert consume_screening(call)["decision"] == "hold_for_review"
    assert "conflicting" in consume_screening(call)["issues"][0]


@pytest.mark.parametrize("case,field", [("candidate_partial", "possible_matches_unverified"), ("hit", "matches")])
def test_dispatch_fencing_preserves_review_and_checks_binding(case, field, monkeypatch):
    call = synthetic_call(monkeypatch, CASES[case][0])
    result = body(call)["result"]
    assert result[field][0]["name"].startswith("[UNTRUSTED]")
    verification = verify_compliance_receipt(result["compliance_receipt"], response_payload=result)
    assert verification["hash_ok"] is True  # receipt payload was not modified
    # The released seam currently leaves a mismatch. A future server repair
    # may bind the delivered result; either way candidates/hits require review.
    binding_ok = verification["response_match"]
    output = consume_screening(call)
    assert output["decision"] == "hold_for_review"
    assert output["guidance_response_bound"] is binding_ok
    assert bool(output["issues"]) is (not binding_ok)
    assert output["review_guidance"]["review_reasons"] == CASES[case][3]


@pytest.mark.parametrize("change", ["guidance", "receipt", "missing", "unknown_version"])
def test_broken_or_unknown_evidence_holds(change):
    call = saved("complete")
    envelope = body(call)
    result = envelope["result"]
    if change == "guidance":
        result["review_guidance"]["review_required"] = True
    elif change == "receipt":
        result["compliance_receipt"]["payload"]["asserts"] = "edited"
    elif change == "missing":
        result.pop("compliance_receipt")
    else:
        result["review_guidance"]["version"] = "future/2"
        # Rebind so this exercises the guidance check rather than the hash check.
        receipt = result["compliance_receipt"]
        receipt["payload"]["response_sha256"] = sha256_of({k: v for k, v in result.items() if k != "compliance_receipt"})
        receipt["integrity"]["payload_sha256"] = sha256_of(receipt["payload"])
    call["content"][0]["text"] = json.dumps(envelope)
    output = consume_screening(call)
    assert output["decision"] == "hold_for_review"
    assert output["issues"]


@pytest.mark.parametrize("call", [{"isError": True}, {"jsonrpc": "2.0", "error": {"code": -32602}},
                                  {"content": [{"type": "text", "text": '{"status":"failure","result":{}}'}]}])
def test_tool_failures(call):
    assert consume_screening(call)["issues"]


def test_unsigned_hash_is_not_pinned_issuer_authentication():
    output = consume_screening(saved("complete"), expected_public_key_hex="00" * 32)
    assert output["decision"] == "hold_for_review"
    assert output["receipt_verification"]["origin_proven"] is False
    assert output["issues"]


@pytest.mark.parametrize("lists", [[], ["OFAC-SDN"], ["OFAC-SDN", "EU-CONSOLIDATED", "UN"],
                                   ["OFAC-SDN", "EU-CONSOLIDATED", "UK-SANCTIONS", "UK-SANCTIONS"]])
def test_inconsistent_complete_list_set_never_records(lists):
    call = saved("complete")
    envelope = body(call)
    result = envelope["result"]
    result["review_guidance"]["lists_screened"] = lists
    receipt = result["compliance_receipt"]
    receipt["payload"]["response_sha256"] = sha256_of({k: v for k, v in result.items() if k != "compliance_receipt"})
    receipt["integrity"]["payload_sha256"] = sha256_of(receipt["payload"])
    call["content"][0]["text"] = json.dumps(envelope)
    output = consume_screening(call)
    assert output["guidance_response_bound"] is True
    assert output["decision"] == "hold_for_review"


def test_signed_receipt_requires_out_of_band_key_for_origin():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from core.compliance_receipt import canonical_bytes
    call = saved("complete")
    envelope = body(call)
    receipt = envelope["result"]["compliance_receipt"]
    key = Ed25519PrivateKey.generate()  # isolated ephemeral fixture signer
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    receipt["integrity"].update(signature_status="signed", signature_algorithm="ed25519",
                                signature=key.sign(canonical_bytes(receipt["payload"])).hex(),
                                public_key_ed25519_hex=public)
    call["content"][0]["text"] = json.dumps(envelope)
    assert consume_screening(call)["issuer_authenticated"] is False
    pinned = consume_screening(call, expected_public_key_hex=public)
    assert pinned["issuer_authenticated"] is True
    assert pinned["decision"] == "record_supported_list_screen"
    wrong = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    assert consume_screening(call, expected_public_key_hex=wrong)["decision"] == "hold_for_review"


def test_no_commerce_door_omits_receipt():
    from agent_interface.no_commerce import tool_result
    output = consume_screening(tool_result(body(saved("complete"))))
    assert output["decision"] == "hold_for_review"
    assert output["issues"] == ["Missing screen_sanctions receipt"]


def test_cli_fixture_and_stdin(capsys, monkeypatch):
    import io
    assert main([str(FIXTURES / "candidate_partial.json")]) == 0
    assert json.loads(capsys.readouterr().out)["review_guidance"]["coverage_status"] == "partial"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(saved("complete"))))
    assert main(["-"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "record_supported_list_screen"
