"""Offline consumption of an MCP screen_sanctions result; never executes actions.

An existing MCP SDK session can pass its call_tool result to consume_screening.
The CLI reads a saved tools/call result (or JSON-RPC response), without a model,
credentials, network, or server. Keep the original input as the evidence record.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Allow `python examples/screening_client.py ...` from a repository checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compliance_receipt import verify_compliance_receipt


def _answer(call_result):
    if hasattr(call_result, "model_dump"):
        call_result = call_result.model_dump(mode="json")
    if not isinstance(call_result, dict):
        raise ValueError("Expected an MCP tools/call result")
    if "jsonrpc" in call_result:
        if call_result.get("error") or not isinstance(call_result.get("result"), dict):
            raise ValueError("JSON-RPC call did not return a tool result")
        call_result = call_result["result"]
    if call_result.get("isError"):
        raise ValueError("MCP tool reported an error")
    bodies = []
    structured = call_result.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            raise ValueError("structuredContent must be an object")
        bodies.append(structured)
    for block in call_result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            parsed = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            continue  # e.g. the existing door's plain-text auth warning
        if isinstance(parsed, dict) and "status" in parsed:
            bodies.append(parsed)
    if not bodies or any(body != bodies[0] for body in bodies[1:]):
        raise ValueError("Missing or conflicting MCP answer envelopes")
    if bodies[0].get("status") != "success":
        raise ValueError("Screening operation did not complete successfully")
    return bodies[0]


def consume_screening(call_result, *, expected_public_key_hex=None):
    """Return a review handoff, never permission to contact or transact.

    A public key must be obtained from the issuer out of band; a key embedded
    in a receipt cannot authenticate that issuer. Unknown contracts fail closed.
    """
    output = {
        "decision": "hold_for_review", "issues": [],
        "actions_are_review_tasks_only": True,
        "record_meaning": "Evidence of a bounded supported-list check only; never a clearance.",
        "guidance_response_bound": False,
        "issuer_authenticated": False,
        "does_not_assert": "Identity, KYC/AML clearance or permission to transact.",
    }
    try:
        body = _answer(call_result)
        result = body.get("result")
        if not isinstance(result, dict):
            raise ValueError("Missing screening result")
        # Preserve independent review hints even when the portable receipt is
        # absent or mismatched. They remain unverified and cannot clear a hold.
        output["review_guidance"] = result.get("review_guidance")
        output["screening_status"] = result.get("screening_status")
        output["matching_method"] = result.get("matching_method")
        receipt = result.get("compliance_receipt")
        if not isinstance(receipt, dict) or receipt.get("payload", {}).get("tool") != "screen_sanctions":
            raise ValueError("Missing screen_sanctions receipt")
        if not output["matching_method"]:
            output["matching_method"] = receipt["payload"].get("evidence", {}).get("matching_method")
        verification = verify_compliance_receipt(
            receipt, response_payload=result,
            expected_public_key_hex=expected_public_key_hex)
        output["receipt_verification"] = verification
        output["issuer_authenticated"] = verification["origin_proven"]
        # The verifier verdict alone does not express response binding.
        if (verification["hash_ok"] is not True or
                verification["response_match"] is not True or
                verification["verdict"] not in {"verified_signed", "verified_unsigned_hash_only"}):
            raise ValueError("Receipt integrity or response binding could not be verified")
        if expected_public_key_hex and verification["origin_proven"] is not True:
            raise ValueError("Pinned issuer key did not authenticate this receipt")
        output["guidance_response_bound"] = True
        guidance = result.get("review_guidance")
        if not isinstance(guidance, dict) or guidance.get("version") != "agentbroker-screening-review/1":
            raise ValueError("Missing or unsupported review guidance")
        if (guidance.get("coverage_scope") != "supported_lists_only" or
                guidance.get("coverage_status") not in {"complete", "partial", "none"} or
                type(guidance.get("review_required")) is not bool):
            raise ValueError("Invalid review guidance")
        for key in ("review_reasons", "next_actions", "lists_screened", "lists_not_screened",
                    "lists_outside_scope", "freshness_unknown_lists", "stale_copy_lists"):
            if not isinstance(guidance.get(key), list) or any(not isinstance(v, str) for v in guidance[key]):
                raise ValueError("Invalid review guidance field: " + key)
        output["review_guidance"] = guidance
        if (guidance["coverage_status"] == "complete" and
                sorted(guidance["lists_screened"]) == ["EU-CONSOLIDATED", "OFAC-SDN", "UK-SANCTIONS"] and
                guidance["review_required"] is False and
                not any(guidance[k] for k in ("review_reasons", "next_actions", "lists_not_screened",
                                             "freshness_unknown_lists", "stale_copy_lists")) and
                result.get("screening_status") == "clean" and
                result.get("matched") is False and not result.get("matches") and
                not result.get("possible_matches_unverified")):
            output["decision"] = "record_supported_list_screen"
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        output["issues"].append(str(exc))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Saved MCP JSON file, or - for stdin")
    parser.add_argument("--issuer-public-key", help="Issuer Ed25519 public key obtained out of band")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        result = consume_screening(json.loads(raw), expected_public_key_hex=args.issuer_public_key)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0  # A review handoff is a successful consumption, not a tool error.


if __name__ == "__main__":
    raise SystemExit(main())
