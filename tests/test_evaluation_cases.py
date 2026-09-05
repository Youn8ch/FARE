from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.schemas import (
    LlmExplanationResponse,
    LlmRequestFindingsResponse,
    LlmSemanticResponse,
)
from tests.case_schema import CaseSuite, Dependencies
from tests.helpers.case_loader import (
    LLM_FIXTURE_ROOT,
    LoadedCase,
    _safe_path,
    load_all_suites,
    load_suite,
    prepare_case,
    resolve_item_symbols,
)
from tests.helpers.llm import RecordingLlmClient

TEST_ROOT = Path(__file__).resolve().parent
CASE_ROOT = TEST_ROOT / "cases" / "evaluations"
V2_SUITES = load_all_suites(CASE_ROOT)
V2_CASES = [prepare_case(case) for suite in V2_SUITES for case in suite.cases]
CORE_V2_CASES = [case for case in V2_CASES if case.case.category == "core"]


@pytest.mark.parametrize("loaded", V2_CASES, ids=lambda loaded: loaded.case.id)
def test_v2_evaluation_case(loaded: LoadedCase, settings: Settings) -> None:
    recorder = _recorder(loaded)
    case_settings = replace(
        settings,
        policy_dir=loaded.policy_dir,
        llm_request_findings_mode=(
            loaded.case.feature_flags.llm_request_findings_mode
        ),
    )
    if loaded.case.dependencies.network_plan_mode is not None:
        case_settings = replace(
            case_settings,
            network_plan_client_mode=loaded.case.dependencies.network_plan_mode,
        )
    with TestClient(create_app(case_settings)) as client:
        client.app.state.runtime.evaluator.llm_client = recorder
        response = client.post(
            "/v1/evaluations",
            json=loaded.case.request.model_dump(mode="json"),
        )

    expected = loaded.case.expected
    assert response.status_code == expected.http_status
    body = response.json()
    assert body["request_id"] == loaded.case.request.request_id
    assert body["decision"] == expected.decision
    assert len(body["items"]) == len(expected.items) <= 4

    for item, expected_item in zip(body["items"], expected.items, strict=True):
        assert item["item_id"] == loaded.item_ids[expected_item.index - 1]
        assert item["access"] == expected_item.access.model_dump(mode="json")
        assert item["decision"] == expected_item.decision
        assert item["reason_type"] == expected_item.reason_type
        assert item["reason_code"] == expected_item.reason_code
        assert [rule["id"] for rule in item["matched_rules"]] == (
            expected_item.matched_rule_ids
        )
        assert item["reason"]
        assert item["recommendation"]

    assert recorder.semantic_calls == expected.llm_calls.semantic
    assert recorder.request_finding_calls == expected.llm_calls.request_findings
    assert recorder.explanation_calls == expected.llm_calls.explanation
    if expected.llm_text:
        has_llm_text = all(
            "llm_explanation" in item and "llm_recommendation" in item
            for item in body["items"]
        )
        assert has_llm_text is (expected.llm_text.presence == "present")
        assert all(
            (item["explanation_source"] == "template")
            is expected.llm_text.fallback
            for item in body["items"]
        )


def test_v2_suite_envelope_and_global_ids_are_valid() -> None:
    assert {suite.suite for suite in V2_SUITES} == {
        "core",
        "llm_pipeline",
        "request_findings",
        "explanation",
    }


def test_v2_filename_must_match_suite(tmp_path: Path) -> None:
    document = json.loads((CASE_ROOT / "core.v2.json").read_text(encoding="utf-8"))
    path = tmp_path / "wrong.v2.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        load_suite(path)


def test_v2_schema_rejects_extra_expected_audit_id() -> None:
    document = json.loads((CASE_ROOT / "core.v2.json").read_text(encoding="utf-8"))
    document["cases"] = [document["cases"][0]]
    document["cases"][0]["expected"]["audit_id"] = "fixed-uuid"
    with pytest.raises(ValidationError):
        CaseSuite.model_validate(document)


@pytest.mark.parametrize(
    "reference",
    ["@item:0", "@item:-1", "@item:x", "@item:3", "prefix-@item:1"],
)
def test_symbolic_item_reference_rejects_invalid_forms(reference: str) -> None:
    with pytest.raises(ValueError, match="symbolic item reference"):
        resolve_item_symbols({"scope": reference}, ["request-001", "request-002"])


def test_symbol_resolution_is_field_limited_and_checks_coverage() -> None:
    value = {
        "analyzed_item_ids": ["@item:1", "@item:2"],
        "claims": [{"scope": "@item:1", "evidence": "literal @item:2"}],
    }
    assert resolve_item_symbols(value, ["request-001", "request-002"]) == {
        "analyzed_item_ids": ["request-001", "request-002"],
        "claims": [{"scope": "request-001", "evidence": "literal @item:2"}],
    }
    with pytest.raises(ValueError, match="complete item set"):
        resolve_item_symbols(
            {"analyzed_item_ids": ["@item:1"]},
            ["request-001", "request-002"],
        )


def test_loader_rejects_more_than_four_combinations() -> None:
    loaded = CORE_V2_CASES[0]
    request = loaded.case.request.model_copy(
        update={"ports": [
            {"start": number, "end": number} for number in range(1000, 1005)
        ]}
    )
    oversized = loaded.case.model_copy(update={"request": request})
    with pytest.raises(ValueError, match="four combinations"):
        prepare_case(oversized)


@pytest.mark.parametrize("relative", ["../escape.json", "missing.json"])
def test_llm_fixture_path_must_exist_inside_allowed_root(relative: str) -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        _safe_path(LLM_FIXTURE_ROOT, relative)


def test_absolute_fixture_and_policy_traversal_are_rejected() -> None:
    with pytest.raises(ValueError, match="relative"):
        _safe_path(LLM_FIXTURE_ROOT, str(CASE_ROOT.resolve()))

    escaped_policy_case = CORE_V2_CASES[0].case.model_copy(
        update={"dependencies": Dependencies(policy_dir="../policies")}
    )
    with pytest.raises(ValueError, match="relative"):
        prepare_case(escaped_policy_case)


def test_schema_rejects_noncontiguous_item_indices() -> None:
    document = json.loads((CASE_ROOT / "core.v2.json").read_text(encoding="utf-8"))
    document["cases"] = [document["cases"][0]]
    document["cases"][0]["expected"]["items"][0]["index"] = 2
    with pytest.raises(ValidationError, match="contiguous"):
        CaseSuite.model_validate(document)


def test_symbol_lists_reject_duplicates_and_empty_affected_items() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        resolve_item_symbols(
            {"affected_item_ids": ["@item:1", "@item:1"]},
            ["request-001"],
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        resolve_item_symbols({"affected_item_ids": []}, ["request-001"])


def test_symlink_fixture_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(ValueError, match="escapes"):
        _safe_path(root, "link.json")


def _recorder(loaded: LoadedCase) -> RecordingLlmClient:
    semantic = loaded.llm_fixtures.get("semantic", {}).get("semantic")
    findings = loaded.llm_fixtures.get("request_findings", {}).get(
        "request_findings"
    )
    explanation = loaded.llm_fixtures.get("explanation", {}).get("explanation")
    return RecordingLlmClient(
        semantic_response=(
            LlmSemanticResponse.model_validate(semantic) if semantic else None
        ),
        request_findings=(
            LlmRequestFindingsResponse.model_validate(findings) if findings else None
        ),
        explanation_response=(
            LlmExplanationResponse.model_validate(explanation)
            if explanation is not None
            else None
        ),
    )
