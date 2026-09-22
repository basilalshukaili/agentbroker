"""The post-deploy gate must reject a healthy but wrong release."""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_postdeploy_live.py"
spec = importlib.util.spec_from_file_location("check_postdeploy_live", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40


def fixture_get(*, build=SHA, target="vps-agentbroker", wrong=False,
                missing=None, media_type=None):
    def get(path):
        if path == missing:
            raise OSError("unavailable")
        if path == "/health":
            body = f'{{"build_commit":"{build}","deploy_target":"{target}"}}'
            return 200, media_type or "application/json", body
        count = gate.LEGAL_COUNTS[path]
        # Numeric HTML entities reproduce the production defect that a literal
        # grep missed. NFC normalization is exercised by the checker as well.
        arabic = gate.WRONG_ARABIC_NAME if wrong else gate.CORRECT_ARABIC_NAME
        encoded = "".join(f"&#{ord(c)};" for c in arabic)
        body = (encoded + gate.LATIN_ENTITY_NAME +
                gate.COMMERCIAL_REGISTRATION) * count
        return 200, media_type or "text/html", body
    return get


def test_accepts_exact_build_target_and_all_decoded_contract_pages():
    result = gate.check(SHA, fixture_get())
    assert result["ready"] is True
    assert len(result["results"]) == 5
    assert not result["errors"]


def test_healthy_previous_build_is_not_a_postdeploy_pass():
    result = gate.check(SHA, fixture_get(build="b" * 40))
    assert result["ready"] is False
    assert result["results"][0]["build_commit"] == "b" * 40


def test_wrong_execution_target_is_not_a_postdeploy_pass():
    result = gate.check(SHA, fixture_get(target="render"))
    assert result["ready"] is False
    assert "deploy_target" in str(result["results"][0]["problems"])


def test_numeric_entity_wrong_company_name_is_rejected():
    result = gate.check(SHA, fixture_get(wrong=True))
    assert result["ready"] is False
    assert result["results"][1]["observed"]["wrong_arabic"] == 4


def test_missing_legal_page_and_wrong_media_type_fail_closed():
    assert gate.check(SHA, fixture_get(missing="/privacy"))["ready"] is False
    assert gate.check(SHA, fixture_get(media_type="text/plain"))["ready"] is False


def test_main_push_does_not_claim_postdeploy_acceptance():
    workflow = (SCRIPT.parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    postdeploy = workflow.split("  postdeploy:", 1)[1]
    assert "github.event_name == 'workflow_dispatch'" in postdeploy
    assert "continue-on-error" not in postdeploy
    assert "--expected-build" in postdeploy
