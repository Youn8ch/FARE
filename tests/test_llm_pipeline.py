from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.main import build_runtime
from app.schemas import EvaluationRequest, LlmSemanticResponse
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = pytest.mark.llm_pipeline


@pytest.mark.parametrize(
    ("deterministic_pending", "semantic_mode", "reason_code", "explanation_calls"),
    [
        (False, "clean", None, 1),
        (False, "policy_gap", "SEMANTIC_POLICY_GAP", 1),
        (False, "failure", "LLM_SEMANTIC_ANALYSIS_FAILURE", 0),
        (True, "clean", "OBJECT-001", 1),
        (True, "policy_gap", "OBJECT-001", 1),
        (True, "failure", "OBJECT-001", 0),
    ],
)
def test_one_way_decision_matrix(
    settings,
    deterministic_pending: bool,
    semantic_mode: str,
    reason_code: str | None,
    explanation_calls: int,
) -> None:
    request_id = f"decision-{int(deterministic_pending)}-{semantic_mode}"
    value = payload(request_id=request_id)
    if deterministic_pending:
        value.update(
            sources=[{"address": "20.1.10.10", "description": "办公终端"}],
            destinations=[
                {"address": "16.1.20.20", "description": "生产数据库"}
            ],
        )
    semantic = None
    if semantic_mode == "policy_gap":
        semantic = LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": [f"{request_id}-001"],
                "policy_gaps": [
                    {
                        "gap_id": "gap-matrix-001",
                        "scope": f"{request_id}-001",
                        "description": "合成规则覆盖缺口",
                        "evidence": [value["request_description"]],
                    }
                ],
            }
        )
    recorder = RecordingLlmClient(
        fail_stage="semantic" if semantic_mode == "failure" else None,
        semantic_response=semantic,
    )
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = recorder

    result = asyncio.run(
        runtime.evaluator.evaluate(EvaluationRequest.model_validate(value))
    )
    item = result.response.items[0]

    assert item.decision == ("待定" if reason_code else "合规")
    assert item.reason_code == reason_code
    assert recorder.semantic_calls == 1
    assert recorder.explanation_calls == explanation_calls
    assert recorder.acl_candidate_calls == 0
    assert recorder.request_finding_calls == 0


def test_all_shadow_stages_are_single_batch_and_do_not_upgrade_pending(settings) -> None:
    recorder = RecordingLlmClient()
    runtime = build_runtime(
        replace(
            settings,
            llm_acl_candidate_mode="shadow",
            llm_request_findings_mode="shadow",
        )
    )
    runtime.evaluator.llm_client = recorder
    request = EvaluationRequest.model_validate(
        payload(
            request_id="pipeline-all-shadow",
            sources=[
                {"address": "20.1.10.10", "description": "办公终端 A"},
                {"address": "20.1.10.11", "description": "办公终端 B"},
            ],
            destinations=[
                {"address": "16.1.20.20", "description": "生产数据库 A"},
                {"address": "16.1.20.21", "description": "生产数据库 B"},
            ],
        )
    )

    result = asyncio.run(runtime.evaluator.evaluate(request))

    assert len(result.response.items) == 4
    assert all(item.decision == "待定" for item in result.response.items)
    assert all(item.reason_code == "OBJECT-001" for item in result.response.items)
    assert recorder.semantic_calls == 1
    assert recorder.acl_candidate_calls == 1
    assert recorder.request_finding_calls == 1
    assert recorder.explanation_calls == 1
    assert recorder.semantic_item_ids[0] == recorder.acl_candidate_item_ids[0]
    assert recorder.semantic_item_ids[0] == recorder.request_finding_item_ids[0]
    assert recorder.semantic_item_ids[0] == recorder.explanation_item_ids[0]
