"""AC-05 acceptance: the evaluator is an orchestrator over named stages.

The stage order plan/network/acl/rules/semantic/reduce/explain/assemble is
observed with recording doubles; failure paths terminate early with explicit
assertions (plan 9.4/9.5).
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.main import build_runtime
from app.schemas import EvaluationRequest
from app.services.decision_reducer import DecisionReducer
from tests.helpers.llm import RecordingLlmClient
from tests.test_architecture_baseline import _mock_chain, _payload

EXPECTED_STAGES = [
    "plan",
    "network",
    "acl",
    "rules",
    "semantic",
    "reduce",
    "explain",
    "assemble",
]


class RecordingAclClient:
    """Wrapper that records per-item ACL client calls."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def analyze(self, combination):
        self.calls.append(combination.source_text)
        return await self.inner.analyze(combination)

    async def aclose(self) -> None:
        closer = getattr(self.inner, "aclose", None)
        if callable(closer):
            result = closer()
            if hasattr(result, "__await__"):
                await result


class RecordingDecisionReducer:
    """Wrapper that records reduce invocations per item."""

    def __init__(self) -> None:
        self.inner = DecisionReducer()
        self.calls: list[int] = []

    def reduce(self, findings, *, matched_rules=()):
        self.calls.append(len(list(findings)))
        return self.inner.reduce(findings, matched_rules=matched_rules)


class StageRecorder:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def __call__(self, name: str) -> None:
        self.stages.append(name)


def _runtime(settings: Settings):
    return build_runtime(_mock_chain(settings))


def _request(payload: dict) -> EvaluationRequest:
    return EvaluationRequest.model_validate(payload)


def test_case01_stage_order_and_call_counts(settings: Settings) -> None:
    recorder = StageRecorder()
    runtime = _runtime(settings)
    try:
        llm = RecordingLlmClient()
        runtime.evaluator.llm_client = llm
        acl = RecordingAclClient(runtime.evaluator.acl_client)
        runtime.evaluator.acl_client = acl
        reducer = RecordingDecisionReducer()
        runtime.evaluator.decision_reducer = reducer
        runtime.evaluator._stage_observer = recorder

        result = asyncio.run(
            runtime.evaluator.evaluate(_request(_payload("ac05-case-01")))
        )

        assert recorder.stages == EXPECTED_STAGES
        # network: 1 batch resolve, 2 unique /24 lookups
        assert len(runtime.network_plan_resolver.client.calls) == 2
        # acl: at most one call per applicable item
        assert len(acl.calls) == len(result.response.items) == 1
        # semantic: one batch; explanation: one batch after success
        assert llm.semantic_calls == 1
        assert llm.explanation_calls == 1
        assert llm.semantic_item_ids == [[result.response.items[0].item_id]]
        # reduce：最终裁决阶段每 item 恰好一次合并裁决；合计 reduce() 调用为
        # 每 item 2 次（rules 阶段 1 次确定性裁决 + reduce 阶段 1 次最终裁决），
        # 两次均经由唯一 DecisionReducer（decision_trace 的
        # deterministic_decision -> final_decision 轨迹要求两者都存在）。
        assert len(reducer.calls) == 2 * len(result.response.items)
        assert result.response.decision == "合规"
    finally:
        asyncio.run(runtime.aclose())


def test_case02_network_failure_skips_acl_calls_but_keeps_stages(
    settings: Settings,
) -> None:
    recorder = StageRecorder()
    runtime = _runtime(settings)
    try:
        llm = RecordingLlmClient()
        runtime.evaluator.llm_client = llm
        acl = RecordingAclClient(runtime.evaluator.acl_client)
        runtime.evaluator.acl_client = acl
        runtime.evaluator._stage_observer = recorder

        result = asyncio.run(
            runtime.evaluator.evaluate(
                _request(
                    _payload(
                        "ac05-case-02",
                        sources=[{"address": "16.201.3.10", "description": "应用"}],
                    )
                )
            )
        )
        assert recorder.stages == EXPECTED_STAGES
        assert acl.calls == []
        assert result.response.items[0].acl_verification_status == "skipped"
        assert llm.semantic_calls == 1
    finally:
        asyncio.run(runtime.aclose())


def test_case04_item_limit_terminates_at_plan_stage(settings: Settings) -> None:
    recorder = StageRecorder()
    runtime = build_runtime(_mock_chain(settings, max_evaluation_items=3))
    try:
        llm = RecordingLlmClient()
        runtime.evaluator.llm_client = llm
        acl = RecordingAclClient(runtime.evaluator.acl_client)
        runtime.evaluator.acl_client = acl
        runtime.evaluator._stage_observer = recorder
        payload = _payload("ac05-case-04")
        payload["sources"].append({"address": "16.201.1.20", "description": "应用 2"})
        payload["destinations"].append(
            {"address": "16.220.16.30", "description": "数据库 2"}
        )
        try:
            asyncio.run(runtime.evaluator.evaluate(_request(payload)))
        except Exception as exc:
            assert exc.__class__.__name__ == "EvaluationItemLimitError"
        assert recorder.stages == ["plan"]
        assert "acl" not in recorder.stages
        assert "semantic" not in recorder.stages
        assert acl.calls == []
        assert llm.semantic_calls == 0
    finally:
        asyncio.run(runtime.aclose())


def test_case03_query_limit_rejects_before_any_stage(settings: Settings) -> None:
    from app.main import EvaluationServiceError

    runtime = build_runtime(
        _mock_chain(settings, network_plan_max_subnets_per_request=1)
    )
    try:
        recorder = StageRecorder()
        runtime.evaluator._stage_observer = recorder
        try:
            asyncio.run(
                runtime.evaluate_request(
                    _request(_payload("ac05-case-03"))
                )
            )
        except EvaluationServiceError as exc:
            assert exc.code == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
        else:
            raise AssertionError("query limit must fail closed")
        assert recorder.stages == []
        assert runtime.network_plan_resolver.client.calls == []
    finally:
        asyncio.run(runtime.aclose())


def test_stage_observer_is_not_wired_in_production_runtime(settings: Settings) -> None:
    runtime = _runtime(settings)
    try:
        assert runtime.evaluator._stage_observer is None
    finally:
        asyncio.run(runtime.aclose())
