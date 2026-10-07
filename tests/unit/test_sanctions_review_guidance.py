"""Offline agent branching: a candidate must not conceal incomplete coverage."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

import core.screen_sanctions as ss
from core.compliance_receipt import verify_compliance_receipt


@pytest.fixture
def screen(monkeypatch):
    real_provenance = ss._data_provenance
    state = {"unavailable": {"UK"}, "matches": [{
        "name": "Example Export Group", "list": "OFAC-SDN",
        "match_score": 0.8, "_matcher": "local_word_overlap",
        "source_url": "https://example.invalid/list",
    }]}

    async def ofac(name):
        return state["matches"], ["OFAC"], (["OFAC unavailable"]
                                             if "OFAC" in state["unavailable"] else [])

    async def database(name, code, *args):
        return [], [code], ([code + " unavailable"]
                            if code in state["unavailable"] else [])

    async def provenance(ofac_ok, eu_ok, uk_ok):
        if state.get("real_provenance"):
            return await real_provenance(ofac_ok, eu_ok, uk_ok)
        return [
            {"list": "OFAC-SDN", "screened_on_this_call": ofac_ok,
             "refresh_state": state.get("ofac_freshness", "refreshed_within_ttl")},
            {"list": "EU-CONSOLIDATED", "screened_on_this_call": eu_ok,
             "within_freshness_limit": state.get("eu_freshness", True)},
            {"list": "UK-SANCTIONS", "screened_on_this_call": uk_ok,
             "within_freshness_limit": True},
            {"list": "UN-CONSOLIDATED (UN Security Council)",
             "screened_on_this_call": False, "reason_not_screened": "Excluded"},
        ]

    def deny_network(*args, **kwargs):
        raise AssertionError("Offline contract test attempted network")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", deny_network)
    monkeypatch.setattr(httpx, "Client", deny_network)
    monkeypatch.setattr(ss, "_call_ofac_sdn", ofac)
    monkeypatch.setattr(ss, "_screen_list_db", database)
    monkeypatch.setattr(ss, "_data_provenance", provenance)
    return state


def test_candidate_and_missing_list_are_independent(screen):
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    assert result["screening_status"] == "candidates"  # existing contract preserved
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == "partial"
    assert guidance["review_required"] is True
    assert guidance["review_reasons"] == ["unverified_candidates", "incomplete_coverage"]
    assert guidance["lists_not_screened"] == ["UK-SANCTIONS"]
    assert "review_candidates_against_official_source" in guidance["next_actions"]
    assert "retry_unavailable_lists" in guidance["next_actions"]


@pytest.mark.parametrize("unavailable,coverage", [
    (set(), "complete"), ({"UK"}, "partial"),
    ({"OFAC", "EU", "UK"}, "none"),
])
def test_no_hits_with_zero_partial_or_all_source_failures(screen, unavailable, coverage):
    screen.update(matches=[], unavailable=unavailable)
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == coverage
    assert guidance["review_required"] is bool(unavailable)
    assert guidance["lists_outside_scope"] == ["UN-CONSOLIDATED (UN Security Council)"]
    assert guidance["coverage_scope"] == "supported_lists_only"
    assert result["matched"] is False
    if not unavailable:
        assert result["screening_status"] == "clean"
        assert guidance["next_actions"] == []


def test_confirmed_name_hit_does_not_claim_identity(screen):
    screen.update(unavailable=set())
    screen["matches"][0]["name"] = "Example Export"
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    assert result["screening_status"] == "hit"
    assert result["review_guidance"]["review_reasons"] == ["confirmed_name_match"]
    assert result["review_guidance"]["next_actions"] == ["verify_identity_against_official_source"]


def test_confirmed_hit_and_missing_list_both_require_review(screen):
    screen["matches"][0]["name"] = "Example Export"
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    assert result["screening_status"] == "hit"
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == "partial"
    assert guidance["review_reasons"] == ["confirmed_name_match", "incomplete_coverage"]
    assert guidance["next_actions"] == ["verify_identity_against_official_source", "retry_unavailable_lists"]


@pytest.mark.parametrize("changes,reason,lists", [
    ({"eu_freshness": None}, "unknown_list_freshness", ["EU-CONSOLIDATED"]),
    ({"ofac_freshness": "unknown"}, "unknown_list_freshness", ["OFAC-SDN"]),
    ({"eu_freshness": False}, "stale_list_copy", ["EU-CONSOLIDATED"]),
    ({"ofac_freshness": "stale_copy_after_failed_refresh"}, "stale_list_copy", ["OFAC-SDN"]),
])
def test_freshness_review_survives_no_hit(screen, changes, reason, lists):
    screen.update(matches=[], unavailable=set(), **changes)
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    guidance = result["review_guidance"]
    assert reason in guidance["review_reasons"]
    assert guidance["review_required"] is True
    assert "verify_list_freshness" in guidance["next_actions"]
    field = "freshness_unknown_lists" if reason == "unknown_list_freshness" else "stale_copy_lists"
    assert guidance[field] == lists


@pytest.mark.parametrize("name", ["Universal", "東京銀行", "\x00\x01"])
def test_unscreenable_name_requires_better_input_not_retry(screen, name):
    screen.update(matches=[], unavailable=set())
    result = asyncio.run(ss.handle_screen_sanctions(name)).result
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == "none"
    assert "name_not_fully_screenable" in guidance["review_reasons"]
    assert "provide_screenable_name" in guidance["next_actions"]
    assert "retry_unavailable_lists" not in guidance["next_actions"]


def test_arabic_no_hit_keeps_lossy_matching_review(screen):
    screen.update(matches=[], unavailable=set())
    result = asyncio.run(ss.handle_screen_sanctions("محمد عبدالله أحمد")).result
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == "complete"
    assert "lossy_transliteration" in guidance["review_reasons"]
    assert "review_original_script_and_aliases" in guidance["next_actions"]
    assert result["screening_status"] != "clean"


def test_receipt_binds_guidance_and_preserves_provenance(screen):
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    receipt = result["compliance_receipt"]
    assert verify_compliance_receipt(receipt, response_payload=result)["response_match"] is True
    edited = copy.deepcopy(result)
    edited["review_guidance"]["review_required"] = False
    assert verify_compliance_receipt(receipt, response_payload=edited)["response_match"] is False


def test_invalid_input_keeps_existing_failure_contract(screen):
    result = asyncio.run(ss.handle_screen_sanctions(" "))
    assert result.reason_code == "bad_input"
    assert not result.result


def test_real_provenance_unknown_and_stale_signals(screen, monkeypatch):
    screen.update(real_provenance=True, matches=[], unavailable={"EU", "UK"})
    monkeypatch.setattr(ss, "list_cache_age_s", lambda url: 9 * 3600)
    monkeypatch.setattr(ss, "_stale_ages", {ss._OFAC_SDN_CSV_URL: 9 * 3600})

    async def refreshed(code):
        return None if code == "EU" else "2020-01-01"

    monkeypatch.setattr(ss, "_refreshed_on", refreshed)
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    guidance = result["review_guidance"]
    assert guidance["coverage_status"] == "partial"
    assert guidance["freshness_unknown_lists"] == ["EU-CONSOLIDATED"]
    assert guidance["stale_copy_lists"] == ["OFAC-SDN", "UK-SANCTIONS"]
    assert {"incomplete_coverage", "unknown_list_freshness", "stale_list_copy"} <= set(guidance["review_reasons"])


def test_discoverable_output_schema_accepts_actual_guidance(screen):
    import jsonschema
    from agent_interface.no_commerce import output_schema

    manifest = json.loads((Path(__file__).parents[2] / "manifest" / "manifest.json").read_text(encoding="utf-8"))
    operation = next(op for op in manifest["operations"] if op["name"] == "screen_sanctions")
    schema = operation["output_schema"]["properties"]["review_guidance"]
    result = asyncio.run(ss.handle_screen_sanctions("Example Export")).result
    jsonschema.validate(result["review_guidance"], schema)
    # The consumer profile exports this contract to MCP clients.
    assert "review_guidance" in json.dumps(output_schema(operation))
