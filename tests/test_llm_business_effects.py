from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from app.main import build_runtime
from app.schemas import EvaluationRequest, LlmSemanticResponse
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = pytest.mark.llm_pipeline


def _evidence(item_id: str, quote: str) -> dict[str, str]:
    return {
        "item_id": item_id,
        "source": "request_description",
        "quote": quote,
    }


def _gap(
    item_id: str,
    *,
    gap_type: str,
    evidence: list[str],
    affected_fields: list[str],
    suggested_effect: str = "review_required",
) -> dict[str, Any]:
    return {
        "gap_id": f"gap-{gap_type}",
        "scope": item_id,
        "gap_type": gap_type,
        "description": "模型发现了有逐字证据支持的业务语义风险。",
        "evidence": [_evidence(item_id, quote) for quote in evidence],
        "affected_fields": affected_fields,
        "question_for_requester": "请确认申请描述中的冲突信息。",
        "suggested_effect": suggested_effect,
    }


def _claim(item_id: str, field: str, quote: str) -> dict[str, Any]:
    return {
        "claim_id": f"claim-{field}",
        "scope": item_id,
        "claim_type": field,
        "value": quote,
        "source": "request_description",
        "evidence": quote,
        "confidence": 1,
    }


def _run(settings, request: dict[str, Any], semantic: dict[str, Any]):
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = RecordingLlmClient(
        semantic_response=LlmSemanticResponse.model_validate(semantic)
    )
    return asyncio.run(
        runtime.evaluator.evaluate(EvaluationRequest.model_validate(request))
    )


@pytest.mark.parametrize(
    ("request_id", "description", "gap_type", "quotes", "affected_fields"),
    [
        (
            "semantic-business-duration-conflict",
            "供应商临时维护生产服务，需要永久开放 TCP/443。",
            "temporary_permanent_conflict",
            ["临时维护", "永久开放"],
            ["temporary_access", "requested_duration"],
        ),
        (
            "semantic-business-purpose-mismatch",
            "仅用于办公网页浏览，目标为生产服务接口。",
            "purpose_target_mismatch",
            ["办公网页浏览", "生产服务接口"],
            ["access_purpose", "system_role"],
        ),
    ],
)
def test_approved_business_gap_downgrades_deterministic_compliant(
    settings,
    request_id: str,
    description: str,
    gap_type: str,
    quotes: list[str],
    affected_fields: list[str],
) -> None:
    item_id = f"{request_id}-001"
    request = payload(request_id=request_id, request_description=description)
    result = _run(
        settings,
        request,
        {
            "analyzed_item_ids": [item_id],
            "claims": [
                _claim(item_id, field, quote)
                for field, quote in zip(affected_fields, quotes, strict=True)
            ],
            "policy_gaps": [
                _gap(
                    item_id,
                    gap_type=gap_type,
                    evidence=quotes,
                    affected_fields=affected_fields,
                )
            ],
        },
    )

    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_code == "SEMANTIC_POLICY_GAP"
    assert item.decision_trace is not None
    assert item.decision_trace.deterministic_decision == "合规"
    assert item.decision_trace.semantic_effect == "downgraded"
    assert item.decision_trace.final_decision == "待定"
    assert item.decision_trace.semantic_finding_ids == [f"gap-{gap_type}"]
    assert result.model_raw["metrics"]["model_business_downgrade_count"] == 1


def test_missing_approval_information_only_asks_question(settings) -> None:
    request_id = "semantic-business-missing-approval"
    item_id = f"{request_id}-001"
    result = _run(
        settings,
        payload(
            request_id=request_id,
            request_description="供应商临时维护生产服务。",
        ),
        {
            "analyzed_item_ids": [item_id],
            "missing_information": [
                {
                    "missing_id": "missing-approval-001",
                    "item_id": item_id,
                    "field": "approval_reference",
                    "question": "请补充有效审批单号。",
                    "impact": "question_only",
                }
            ],
        },
    )

    item = result.response.items[0]
    assert item.decision == "合规"
    assert item.reason_code is None
    assert item.decision_trace is not None
    assert item.decision_trace.semantic_effect == "question_only"
    assert item.decision_trace.semantic_finding_ids == ["missing-approval-001"]
    assert result.model_raw["metrics"]["model_question_only_count"] == 1


def test_missing_approval_cannot_be_repackaged_as_reviewable_gap(settings) -> None:
    request_id = "semantic-business-missing-approval-overreach"
    item_id = f"{request_id}-001"
    description = "供应商临时维护生产服务。"
    result = _run(
        settings,
        payload(request_id=request_id, request_description=description),
        {
            "analyzed_item_ids": [item_id],
            "claims": [_claim(item_id, "temporary_access", "临时维护")],
            "policy_gaps": [
                _gap(
                    item_id,
                    gap_type="approval_scope_mismatch",
                    evidence=["供应商临时维护生产服务", "临时维护"],
                    affected_fields=["approval_reference", "temporary_access"],
                )
            ],
            "missing_information": [
                {
                    "missing_id": "missing-approval-overreach-001",
                    "item_id": item_id,
                    "field": "approval_reference",
                    "question": "请补充有效审批单号。",
                    "impact": "question_only",
                }
            ],
        },
    )

    item = result.response.items[0]
    gap = result.response.semantic_analysis.policy_gaps[0]
    assert gap.status == "rejected"
    assert gap.applied_effect == "observe_only"
    assert item.decision == "合规"
    assert item.decision_trace.semantic_effect == "question_only"
    assert item.decision_trace.semantic_finding_ids == [
        "missing-approval-overreach-001"
    ]


def test_normal_business_description_remains_compliant(settings) -> None:
    request_id = "semantic-business-normal"
    result = _run(
        settings,
        payload(
            request_id=request_id,
            request_description="生产应用调用同区域生产服务的 HTTPS 接口。",
        ),
        {"analyzed_item_ids": [f"{request_id}-001"]},
    )

    item = result.response.items[0]
    assert item.decision == "合规"
    assert item.decision_trace is not None
    assert item.decision_trace.semantic_effect == "unchanged"
    assert item.decision_trace.semantic_finding_ids == []


def test_server_effect_policy_overrides_model_suggestion(settings) -> None:
    request_id = "semantic-business-observe-only"
    item_id = f"{request_id}-001"
    description = "管理员直接登录生产服务执行受控运维。"
    observe_settings = replace(
        settings,
        semantic_effects={
            **settings.semantic_effects,
            "unclassified_privileged_access": "observe_only",
        },
    )
    result = _run(
        observe_settings,
        payload(request_id=request_id, request_description=description),
        {
            "analyzed_item_ids": [item_id],
            "claims": [
                _claim(
                    item_id,
                    "maintenance_method",
                    "管理员直接登录生产服务",
                )
            ],
            "policy_gaps": [
                _gap(
                    item_id,
                    gap_type="unclassified_privileged_access",
                    evidence=["管理员直接登录生产服务"],
                    affected_fields=["maintenance_method"],
                    suggested_effect="review_required",
                )
            ],
        },
    )

    item = result.response.items[0]
    assert item.decision == "合规"
    assert item.decision_trace is not None
    assert item.decision_trace.semantic_effect == "observation_only"
    assert result.response.semantic_analysis.policy_gaps[0].suggested_effect == (
        "review_required"
    )
    assert result.response.semantic_analysis.policy_gaps[0].applied_effect == (
        "observe_only"
    )
    assert result.model_raw["metrics"]["model_observation_only_count"] == 1


def test_unapproved_gap_type_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError, match="gap_type"):
        LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": ["item-001"],
                "policy_gaps": [
                    _gap(
                        "item-001",
                        gap_type="generic_risk",
                        evidence=["风险"],
                        affected_fields=["request_context"],
                    )
                ],
            }
        )


def test_reviewable_gap_with_one_evidence_is_recorded_but_cannot_downgrade(
    settings,
) -> None:
    request_id = "semantic-business-weak-gap"
    item_id = f"{request_id}-001"
    result = _run(
        settings,
        payload(
            request_id=request_id,
            request_description="临时维护需要永久开放。",
        ),
        {
            "analyzed_item_ids": [item_id],
            "claims": [
                _claim(item_id, "temporary_access", "临时维护"),
                _claim(item_id, "requested_duration", "永久开放"),
            ],
            "policy_gaps": [
                _gap(
                    item_id,
                    gap_type="temporary_permanent_conflict",
                    evidence=["临时维护"],
                    affected_fields=["temporary_access", "requested_duration"],
                )
            ],
        },
    )

    assert result.response.items[0].decision == "合规"
    assert result.response.semantic_analysis.policy_gaps[0].status == "rejected"
    assert result.response.items[0].decision_trace.semantic_effect == "unchanged"
