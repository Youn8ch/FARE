from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from app.main import build_runtime
from app.schemas import (
    Access,
    EvaluationItem,
    EvaluationRequest,
    LlmExplanationResponse,
    MatchedRule,
    PortRange,
)
from app.services.explanation_guard import (
    ExplanationGuardError,
    guard_explanation_output,
)
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = [pytest.mark.llm_guard, pytest.mark.llm_pipeline]


def _item(
    item_id: str = "explanation-001",
    *,
    pending: bool = False,
) -> EvaluationItem:
    matched_rules = (
        [MatchedRule(id="OBJECT-001", name="对象隔离", category="object_relation")]
        if pending
        else []
    )
    return EvaluationItem(
        item_id=item_id,
        access=Access(
            source="16.1.30.10/32",
            destination="16.1.30.20/32",
            protocol="tcp",
            port=PortRange(start=443, end=443),
        ),
        decision="待定" if pending else "合规",
        reason_type="policy_violation" if pending else None,
        reason_code="OBJECT-001" if pending else None,
        matched_rules=matched_rules,
        reason="服务端规范原因。",
        recommendation="服务端规范建议。",
    )


def _response(item_ids: list[str], **updates: Any) -> LlmExplanationResponse:
    items = [
        {
            "item_id": item_id,
            "explanation": "基于锁定的结构化事实生成说明。",
            "recommendation": "请按服务端给出的规范建议处理。",
            "referenced_rule_ids": [],
        }
        for item_id in item_ids
    ]
    for item in items:
        item.update(updates)
    return LlmExplanationResponse.model_validate({"items": items})


def _guard(output: LlmExplanationResponse | dict[str, Any], items=None):
    return guard_explanation_output(
        output,
        items=items or [_item()],
        valid_rule_ids={"OBJECT-001", "PORT-001"},
    )


def test_valid_explanation_is_trimmed_and_kept_separate() -> None:
    output = _response(
        ["explanation-001"],
        explanation="  基于锁定的结构化事实生成说明。  ",
        recommendation="  请按服务端给出的规范建议处理。  ",
    )

    guarded = _guard(output)

    assert guarded["explanation-001"].explanation == "基于锁定的结构化事实生成说明。"
    assert guarded["explanation-001"].recommendation == (
        "请按服务端给出的规范建议处理。"
    )


@pytest.mark.parametrize(
    "item_ids",
    [[], ["explanation-001", "explanation-001"], ["explanation-001", "extra"]],
    ids=["missing", "duplicate", "extra"],
)
def test_item_coverage_must_be_complete_and_unique(item_ids: list[str]) -> None:
    with pytest.raises(ExplanationGuardError, match="item set"):
        _guard(_response(item_ids))


@pytest.mark.parametrize(
    "updates",
    [
        {"explanation": ""},
        {"recommendation": "   "},
    ],
    ids=["empty-explanation", "blank-recommendation"],
)
def test_empty_text_is_rejected(updates: dict[str, str]) -> None:
    with pytest.raises(ExplanationGuardError, match="must not be blank"):
        _guard(_response(["explanation-001"], **updates))


def test_text_length_boundary_is_enforced() -> None:
    valid = _response(["explanation-001"], explanation="x" * 4000)
    assert _guard(valid)

    with pytest.raises(ValidationError):
        _response(["explanation-001"], explanation="x" * 4001)


def test_matched_rule_reference_is_allowed() -> None:
    output = _response(
        ["explanation-001"],
        explanation="该组合命中 OBJECT-001。",
        referenced_rule_ids=["OBJECT-001"],
    )
    guarded = _guard(output, [_item(pending=True)])

    assert guarded["explanation-001"].referenced_rule_ids == ["OBJECT-001"]


@pytest.mark.parametrize(
    ("rule_id", "message"),
    [("FABRICATED-001", "nonexistent"), ("PORT-001", "unmatched")],
)
def test_fabricated_and_unmatched_rules_are_rejected(rule_id: str, message: str) -> None:
    output = _response(
        ["explanation-001"],
        explanation=f"引用规则 {rule_id}。",
        referenced_rule_ids=[rule_id],
    )
    with pytest.raises(ExplanationGuardError, match=message):
        _guard(output, [_item(pending=True)])


@pytest.mark.parametrize(
    "text",
    [
        "现网已经放通该访问。",
        "该申请已经审批。",
        "路由已经确认可达。",
        "NAT 已配置并生效。",
        "Approval granted for this request.",
    ],
)
def test_unauthorized_live_state_assertions_are_rejected(text: str) -> None:
    with pytest.raises(ExplanationGuardError, match="unauthorized assertion"):
        _guard(_response(["explanation-001"], explanation=text))


@pytest.mark.parametrize(
    ("item", "text"),
    [
        (_item(pending=True), "结论为合规，可以直接放行。"),
        (_item(), "结论为待定，必须拒绝。"),
    ],
)
def test_text_cannot_conflict_with_locked_decision(
    item: EvaluationItem, text: str
) -> None:
    with pytest.raises(ExplanationGuardError, match="locked decision"):
        _guard(_response([item.item_id], explanation=text), [item])


def test_mapping_with_forbidden_decision_field_is_rejected() -> None:
    document = _response(["explanation-001"]).model_dump(mode="json")
    document["items"][0]["decision"] = "合规"

    with pytest.raises(ExplanationGuardError, match="schema validation"):
        _guard(document)


def _evaluate(settings, recorder: RecordingLlmClient, request_payload: dict[str, Any]):
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = recorder
    request = EvaluationRequest.model_validate(request_payload)
    return asyncio.run(runtime.evaluator.evaluate(request))


def _canonical_snapshot(item: EvaluationItem) -> dict[str, Any]:
    return {
        "decision": item.decision,
        "reason_type": item.reason_type,
        "reason_code": item.reason_code,
        "matched_rules": [rule.model_dump(mode="json") for rule in item.matched_rules],
        "evidence": item.evidence,
        "reason": item.reason,
        "recommendation": item.recommendation,
    }


def test_evaluator_writes_only_optional_llm_text_fields(settings) -> None:
    request_payload = payload(request_id="explanation-e2e-valid")
    baseline = _evaluate(settings, RecordingLlmClient(), request_payload)
    custom = _response(
        ["explanation-e2e-valid-001"],
        explanation="独立的模型说明。",
        recommendation="独立的模型整改建议。",
    )
    result = _evaluate(
        settings,
        RecordingLlmClient(explanation_response=custom),
        request_payload,
    )

    assert _canonical_snapshot(result.response.items[0]) == _canonical_snapshot(
        baseline.response.items[0]
    )
    assert result.response.items[0].llm_explanation == "独立的模型说明。"
    assert result.response.items[0].llm_recommendation == "独立的模型整改建议。"
    assert result.response.items[0].explanation_source == "llm"


def test_evaluator_guard_failure_keeps_template_and_decision(settings) -> None:
    request_payload = payload(request_id="explanation-e2e-fallback")
    baseline = _evaluate(settings, RecordingLlmClient(), request_payload)
    invalid = _response(
        ["explanation-e2e-fallback-001"],
        explanation="现网已经放通该访问。",
    )
    result = _evaluate(
        settings,
        RecordingLlmClient(explanation_response=invalid),
        request_payload,
    )

    item = result.response.items[0]
    assert _canonical_snapshot(item) == _canonical_snapshot(baseline.response.items[0])
    assert item.explanation_source == "template"
    assert item.llm_explanation is None
    assert item.llm_recommendation is None
    assert result.response.decision == baseline.response.decision
    assert result.model_raw["explanation"]["template_fallback"] is True
