from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import build_runtime, create_app
from app.schemas import EvaluationRequest
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = pytest.mark.llm_pipeline


def test_model_raw_records_versions_stage_statuses_and_metrics(settings) -> None:
    runtime = build_runtime(
        replace(
            settings,
            llm_request_findings_mode="shadow",
        )
    )
    request = EvaluationRequest.model_validate(
        payload(request_id="observability-success")
    )

    result = asyncio.run(runtime.evaluator.evaluate(request))
    metadata = result.model_raw["metadata"]
    stages = result.model_raw["stages"]
    metrics = result.model_raw["metrics"]

    assert metadata["client_mode"] == "mock"
    assert metadata["model_version"]
    assert metadata["policy_version"] == result.response.policy_version
    assert set(metadata["prompt_versions"]) == {
        "semantic",
        "request_findings",
        "explanation",
    }
    assert metadata["fixture_version"] is None
    assert set(stages) == {
        "semantic",
        "request_findings",
        "explanation",
    }
    assert all(stage["status"] == "passed" for stage in stages.values())
    assert all(stage["duration_ms"] >= 0 for stage in stages.values())
    assert metrics == {
        "schema_attempt_count": 0,
        "schema_correction_count": 0,
        "schema_correction_rate": 0.0,
        "guard_rejection_count": 0,
        "model_schema_rejection_count": 0,
        "model_output_guard_rejection_count": 0,
        "model_dependency_failure_count": 0,
        "explanation_fallback_count": 0,
        "llm_added_pending_count": 0,
        "model_business_downgrade_count": 0,
        "model_observation_only_count": 0,
        "model_question_only_count": 0,
    }


def test_stage_failure_is_typed_and_explanation_fallback_is_counted(settings) -> None:
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = RecordingLlmClient(fail_stage="semantic")
    request = EvaluationRequest.model_validate(
        payload(request_id="observability-semantic-failure")
    )

    result = asyncio.run(runtime.evaluator.evaluate(request))
    stages = result.model_raw["stages"]

    assert stages["semantic"]["status"] == "rejected"
    assert stages["semantic"]["error_type"] == "LlmDependencyError"
    assert stages["explanation"]["status"] == "skipped"
    assert result.model_raw["metrics"]["guard_rejection_count"] == 1
    assert result.model_raw["metrics"]["explanation_fallback_count"] == 1


def test_secrets_are_redacted_from_response_audit_and_all_replays(
    settings, tmp_path
) -> None:
    secret_settings = replace(settings, audit_log_dir=tmp_path / "audit")
    value = payload(
        request_id="observability-redaction",
        request_description=(
            "HTTPS access password=SENTINEL_PASSWORD "
            "api_key=SENTINEL_API_KEY"
        ),
        sources=[
            {
                "address": "16.1.30.10",
                "description": "Authorization: Bearer SENTINEL_SOURCE_AUTH",
            }
        ],
    )

    with TestClient(create_app(secret_settings)) as first_client:
        first = first_client.post("/v2/evaluations", json=value)
        replay = first_client.post("/v2/evaluations", json=value)
    with TestClient(create_app(secret_settings)) as restarted_client:
        restarted = restarted_client.post("/v2/evaluations", json=value)

    assert first.status_code == replay.status_code == restarted.status_code == 200
    assert first.json() == replay.json() == restarted.json()
    assert "[REDACTED]" in first.text

    audit_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in secret_settings.audit_log_dir.glob("*.jsonl")
    )
    combined = first.text + audit_text
    for sentinel in (
        "SENTINEL_PASSWORD",
        "SENTINEL_API_KEY",
        "SENTINEL_SOURCE_AUTH",
    ):
        assert sentinel not in combined

    record = json.loads(audit_text.splitlines()[-1])
    for field in (
        "normalized_input",
        "model_raw",
        "final_response",
        "exceptions",
    ):
        assert "SENTINEL_" not in json.dumps(record[field])
