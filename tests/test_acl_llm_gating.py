"""AC-06 acceptance: ACL and LLM responsibility boundaries, fixed by tests.

Decisions recorded here (plan 10.2/10.3):
- ACL-01: network-plan failure blocks ACL calls (call count 0).
- ACL-02: any-address items are governed by ``acl.deterministic_pending_mode``
  — the production default profile (``skip``) makes no ACL call because
  LEAST-ANY-001 already decides 待定; ``analyze`` keeps one call for
  candidate observation. Both modes are pinned below.
- LLM-01: verified authoritative conflicts affect the decision (合规 -> 待定).
- LLM-02 (FROZEN): the policy_gap target state ("affects_decision = false")
  requires written business confirmation (plan C8 / CASE-11); until then the
  configured effect (default ``review_required``) downgrades 合规 -> 待定.
  Do NOT change this behavior without that confirmation.
- LLM-03: fabricated rule ids fail the semantic batch closed.
- Call counts: off -> 0 shadow calls; shadow -> exactly 1 batch per stage and
  no decision impact. The LLM can never upgrade 待定 -> 合规.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, create_app
from app.schemas import EvaluationRequest
from tests.helpers.llm import RecordingLlmClient
from tests.test_architecture_baseline import _mock_chain, _payload


def _runtime(settings: Settings, **changes) -> tuple[object, RecordingLlmClient, object]:
    runtime = build_runtime(_mock_chain(settings, **changes))
    recorder = RecordingLlmClient()
    runtime.evaluator.llm_client = recorder
    return runtime, recorder, runtime.evaluator.acl_client


def _evaluate(runtime, payload: dict):
    return asyncio.run(runtime.evaluator.evaluate(EvaluationRequest.model_validate(payload)))


# ---------------------------------------------------------------------------
# ACL gating
# ---------------------------------------------------------------------------


def test_acl01_network_plan_failure_means_zero_acl_calls(settings: Settings) -> None:
    runtime, recorder, acl_client = _runtime(settings)
    try:
        result = _evaluate(
            runtime,
            _payload(
                "ac06-acl-01",
                sources=[{"address": "16.201.3.10", "description": "应用"}],
            ),
        )
        item = result.response.items[0]
        assert item.reason_code == "NETWORK_PLAN_NOT_FOUND"
        assert item.acl_verification_status == "skipped"
        assert acl_client.calls == []
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_acl02_any_address_call_count_is_mode_governed(settings: Settings) -> None:
    # analyze：any 命中 LEAST-ANY-001 后仍收集一次 ACL 候选观测
    runtime, _, acl_client = _runtime(settings)
    try:
        result = _evaluate(runtime, _payload("ac06-acl-02-analyze",
                                             sources=[{"address": "any", "description": "全来源"}]))
        assert result.response.items[0].reason_code == "LEAST-ANY-001"
        assert len(acl_client.calls) == 1
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())

    # skip（生产 profile 默认）：确定性待定不再调用 ACL
    runtime, _, acl_client = _runtime(settings, acl_deterministic_pending_mode="skip")
    try:
        result = _evaluate(runtime, _payload("ac06-acl-02-skip",
                                             sources=[{"address": "any", "description": "全来源"}]))
        assert result.response.items[0].reason_code == "LEAST-ANY-001"
        assert acl_client.calls == []
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


# ---------------------------------------------------------------------------
# LLM responsibilities
# ---------------------------------------------------------------------------


def test_llm01_conflict_is_decision_relevant(settings: Settings) -> None:
    from app.schemas import LlmSemanticResponse

    item_id = "ac06-llm-01-001"
    conflict = LlmSemanticResponse.model_validate(
        {
            "analyzed_item_ids": [item_id],
            "claims": [
                {
                    "claim_id": "ac06-conflict-001",
                    "scope": item_id,
                    "claim_type": "source_zone",
                    "value": "核心生产区",
                    "source": "request_description",
                    "evidence": "HTTPS 访问",
                    "confidence": 0.9,
                }
            ],
        }
    )
    runtime, _, _ = _runtime(settings)
    try:
        recorder = RecordingLlmClient(semantic_response=conflict)
        runtime.evaluator.llm_client = recorder
        result = _evaluate(runtime, _payload("ac06-llm-01"))
        item = result.response.items[0]
        assert item.decision_trace.deterministic_decision == "合规"
        assert item.decision == "待定"
        assert item.reason_code == "SEMANTIC_FACT_CONFLICT"
        assert recorder.semantic_calls == 1
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_llm02_policy_gap_target_state_is_frozen(settings: Settings) -> None:
    """冻结项（计划 C8/CASE-11）：policy_gap 目标态（不影响裁决）必须等待业务
    方书面确认。当前按配置默认 review_required 降级 —— 本测试固定现状，若业务
    批准改变行为，必须同步修改本测试与 evaluation.semantic_effects 默认值。"""

    from app.schemas import LlmSemanticResponse, SemanticEvidence

    item_id = "ac06-llm-02-001"
    gap_response = LlmSemanticResponse.model_validate(
        {
            "analyzed_item_ids": [item_id],
            "claims": [
                {
                    "claim_id": "ac06-gap-claim-001",
                    "scope": item_id,
                    "claim_type": "temporary_access",
                    "value": "临时开通",
                    "source": "request_description",
                    "evidence": "临时开通",
                    "confidence": 0.9,
                },
                {
                    "claim_id": "ac06-gap-claim-002",
                    "scope": item_id,
                    "claim_type": "requested_duration",
                    "value": "30 天",
                    "source": "request_description",
                    "evidence": "30 天",
                    "confidence": 0.9,
                },
            ],
            "policy_gaps": [
                {
                    "gap_id": "ac06-gap-001",
                    "scope": item_id,
                    "gap_type": "temporary_permanent_conflict",
                    "description": "临时开通缺少到期时间",
                    "evidence": [
                        SemanticEvidence.model_validate(
                            {
                                "item_id": item_id,
                                "source": "request_description",
                                "quote": "临时开通",
                            }
                        ),
                        SemanticEvidence.model_validate(
                            {
                                "item_id": item_id,
                                "source": "request_description",
                                "quote": "30 天",
                            }
                        ),
                    ],
                    "affected_fields": ["temporary_access", "requested_duration"],
                    "question_for_requester": "请确认到期时间。",
                    "suggested_effect": "review_required",
                }
            ],
        }
    )
    runtime, _, _ = _runtime(settings)
    try:
        runtime.evaluator.llm_client = RecordingLlmClient(semantic_response=gap_response)
        result = _evaluate(
            runtime,
            _payload("ac06-llm-02", request_description="临时开通 30 天的 HTTPS 访问"),
        )
        item = result.response.items[0]
        # 现状（冻结）：review_required 缺口将 合规 -> 待定
        assert item.decision_trace.deterministic_decision == "合规"
        assert item.decision == "待定"
        assert item.reason_code == "SEMANTIC_POLICY_GAP"
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_llm03_fabricated_rule_rejects_semantic_batch(settings: Settings) -> None:
    from app.schemas import LlmSemanticResponse

    item_id = "ac06-llm-03-001"
    fabricated = LlmSemanticResponse.model_validate(
        {
            "analyzed_item_ids": [item_id],
            "candidate_rule_ids": ["NON-EXISTENT-999"],
        }
    )
    runtime, _, _ = _runtime(settings)
    try:
        runtime.evaluator.llm_client = RecordingLlmClient(semantic_response=fabricated)
        result = _evaluate(runtime, _payload("ac06-llm-03"))
        item = result.response.items[0]
        assert item.decision == "待定"
        assert item.reason_code == "LLM_SEMANTIC_ANALYSIS_FAILURE"
        assert item.reason_type == "dependency_failure"
        # 虚构规则不得进入 matched_rules 或候选规则
        assert all(rule.id != "NON-EXISTENT-999" for rule in item.matched_rules)
        assert "NON-EXISTENT-999" not in result.response.semantic_analysis.candidate_rule_ids
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_llm_never_upgrades_pending_to_compliant(settings: Settings) -> None:
    runtime, _, _ = _runtime(settings)
    try:
        runtime.evaluator.llm_client = RecordingLlmClient()
        result = _evaluate(
            runtime,
            _payload(
                "ac06-llm-pending",
                destinations=[{"address": "16.220.16.20", "description": "设备"}],
                ports=[{"start": 23, "end": 23}],
                request_description="Telnet 管理",
            ),
        )
        item = result.response.items[0]
        assert item.decision_trace.deterministic_decision == "待定"
        assert item.decision == "待定"
        assert item.reason_code == "PORT-001"
        assert item.decision_trace.semantic_effect == "unchanged"
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


# ---------------------------------------------------------------------------
# Shadow stage call counts (plan 10.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["llm_acl_candidate_mode", "llm_request_findings_mode"])
def test_shadow_off_means_zero_calls_and_absent_field(
    settings: Settings, flag: str
) -> None:
    runtime, recorder, _ = _runtime(settings)
    try:
        result = _evaluate(runtime, _payload(f"ac06-off-{flag}"))
        assert result.response.decision == "合规"
        if flag == "llm_acl_candidate_mode":
            assert recorder.acl_candidate_calls == 0
            assert result.response.acl_candidate_analysis is None
        else:
            assert recorder.request_finding_calls == 0
            assert result.response.request_findings is None
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_shadow_modes_call_once_and_keep_decisions(settings: Settings) -> None:
    runtime, recorder, _ = _runtime(
        settings, llm_acl_candidate_mode="shadow", llm_request_findings_mode="shadow"
    )
    try:
        result = _evaluate(
            runtime,
            _payload(
                "ac06-shadow-both",
                destinations=[{"address": "16.220.16.20", "description": "设备"}],
                ports=[{"start": 23, "end": 23}],
                request_description="Telnet 管理",
            ),
        )
        item = result.response.items[0]
        assert item.reason_code == "PORT-001"
        assert item.decision == "待定"
        assert recorder.acl_candidate_calls == 1
        assert recorder.request_finding_calls == 1
        assert result.response.acl_candidate_analysis is not None
        assert result.response.request_findings is not None
        # shadow 不改变确定性结论
        assert item.decision_trace.semantic_effect == "unchanged"
    finally:
        import asyncio as _asyncio

        _asyncio.run(runtime.aclose())


def test_api_baseline_after_gating_review(settings: Settings) -> None:
    with TestClient(create_app(_mock_chain(settings))) as client:
        ok = client.post("/v1/evaluations", json=_payload("ac06-api-01"))
    assert ok.status_code == 200
    assert ok.json()["decision"] == "合规"
