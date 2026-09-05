"""V4-P8: the centralized main-chain acceptance matrix (plan §15, V3-01..33).

Every case runs on simulated dependencies only (mock providers / recording
LLM) with precise assertions on decisions, reason codes, findings, decision
traces, and dependency call counts. The ACL chain cases (V3-09..V3-14) were
retired with the ACL capability; their migration evidence now lives in the
realistic network request suite (RN-029..RN-035).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, create_app
from app.schemas import EvaluationRequest, LlmSemanticResponse
from tests.helpers.llm import LlmDependencyError, RecordingLlmClient
from tests.test_architecture_baseline import _mock_chain, _payload
from tests.test_evaluator_orchestration import RecordingDecisionReducer
from tests.test_v4_characterization import RecordingPolicyBundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NET_DEPENDENCY_FAILURE = (
    PROJECT_ROOT / "tests/fixtures/network_plan/dependency_failure.v1.json"
)
NET_SUBNET_MISMATCH = (
    PROJECT_ROOT / "tests/fixtures/network_plan/subnet_mismatch.v1.json"
)


class _Calls(SimpleNamespace):
    provider: list
    rule_match: list
    reduce: int
    semantic: int


def _evaluate(settings: Settings, payload: dict, *, llm=None, **changes):
    """Direct-evaluator harness with recording doubles and call accounting."""

    runtime = build_runtime(_mock_chain(settings, **changes))
    reducer = RecordingDecisionReducer()
    runtime.evaluator.decision_reducer = reducer
    policies = RecordingPolicyBundle(runtime.evaluator.policies)
    runtime.evaluator.policies = policies
    if llm is not None:
        runtime.evaluator.llm_client = llm
    calls = _Calls(provider=[], rule_match=[], reduce=0, semantic=0)
    try:
        result = asyncio.run(
            runtime.evaluator.evaluate(EvaluationRequest.model_validate(payload))
        )
        calls.provider = list(
            runtime.network_plan_resolver.provider.transport.calls
        )
        calls.rule_match = list(policies.match_calls)
        calls.reduce = len(reducer.calls)
        calls.semantic = getattr(runtime.evaluator.llm_client, "semantic_calls", 0)
        return result, calls
    finally:
        asyncio.run(runtime.aclose())


def _item(result):
    assert len(result.response.items) == 1
    return result.response.items[0]


def _finding_codes(item) -> list[str]:
    return [finding.code for finding in item.decision_findings]


def _semantic(item_id: str, **extra) -> RecordingLlmClient:
    base = {
        "analyzed_item_ids": [item_id],
        "claims": [
            {
                "claim_id": f"{item_id}-conflict",
                "scope": item_id,
                "claim_type": "destination_zone",
                "value": "鳌峰科创生产区",
                "source": "request_description",
                "evidence": "HTTPS 访问",
                "confidence": 0.9,
            }
        ],
        "candidate_rule_ids": [],
        "policy_gaps": [],
    }
    base.update(extra)
    return RecordingLlmClient(semantic_response=LlmSemanticResponse.model_validate(base))


# ---------------------------------------------------------------------------
# V3-01..V3-04: network chain
# ---------------------------------------------------------------------------


def test_v3_01_normal_https(settings: Settings) -> None:
    result, calls = _evaluate(settings, _payload("v3-01"))
    item = _item(result)
    assert item.decision == "合规"
    assert result.response.decision == "合规"
    assert len(calls.rule_match) == 1
    assert calls.reduce == 1


def test_v3_02_network_plan_not_found(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload("v3-02", sources=[{"address": "16.201.3.10", "description": "应用"}]),
    )
    item = _item(result)
    assert (item.decision, item.reason_code) == ("待定", "NETWORK_PLAN_NOT_FOUND")


def test_v3_03_network_dependency_failure(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload("v3-03"),
        network_plan_mock_file=NET_DEPENDENCY_FAILURE,
    )
    item = _item(result)
    assert item.decision == "待定"
    assert item.reason_code == "NETWORK_PLAN_DEPENDENCY_FAILURE"
    assert item.source_network_fact_status == "dependency_failure"


def test_v3_04_subnet_mismatch(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload("v3-04"),
        network_plan_mock_file=NET_SUBNET_MISMATCH,
    )
    item = _item(result)
    assert item.decision == "待定"
    assert item.reason_code == "NETWORK_PLAN_SUBNET_MISMATCH"


# ---------------------------------------------------------------------------
# V3-05..V3-08: rule chain
# ---------------------------------------------------------------------------


def _telnet(request_id: str) -> dict:
    return _payload(
        request_id,
        destinations=[{"address": "16.220.16.20", "description": "设备"}],
        ports=[{"start": 23, "end": 23}],
        request_description="Telnet 管理",
    )


def test_v3_06_telnet(settings: Settings) -> None:
    result, calls = _evaluate(settings, _telnet("v3-06"))
    item = _item(result)
    assert (item.decision, item.reason_code) == ("待定", "PORT-001")
    assert len(calls.rule_match) == 1


def test_v3_07_broad_port_range_keeps_all_findings(settings: Settings) -> None:
    result, calls = _evaluate(
        settings, _payload("v3-07", ports=[{"start": 1, "end": 101}])
    )
    item = _item(result)
    assert item.reason_code == "PORT-001"
    assert [rule.id for rule in item.matched_rules] == ["PORT-001", "LEAST-PORT-001"]
    assert "PORT-001" in _finding_codes(item)
    assert "LEAST-PORT-001" in _finding_codes(item)
    assert len(calls.rule_match) == 1


def test_v3_08_any_address(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload("v3-08", sources=[{"address": "any", "description": "全来源"}]),
    )
    item = _item(result)
    assert (item.decision, item.reason_code) == ("待定", "LEAST-ANY-001")
    # any 不发 provider 查询（仅目的端 /24 一次）
    assert calls.provider == ["16.220.16.0/24"]


# ---------------------------------------------------------------------------
# V3-15..V3-20: semantic chain
# ---------------------------------------------------------------------------


def test_v3_15_semantic_fact_conflict(settings: Settings) -> None:
    llm = _semantic("v3-15-001")
    result, calls = _evaluate(settings, _payload("v3-15"), llm=llm)
    item = _item(result)
    trace = item.decision_trace
    assert trace.deterministic_decision == "合规"
    assert (item.decision, item.reason_code) == ("待定", "SEMANTIC_FACT_CONFLICT")
    assert trace.semantic_effect == "downgraded"
    assert calls.reduce == 1


def test_v3_16_semantic_question_only(settings: Settings) -> None:
    llm = _semantic(
        "v3-16-001",
        claims=[],
        missing_information=[
            {
                "missing_id": "v3-16-missing-1",
                "item_id": "v3-16-001",
                "field": "access_purpose",
                "question": "请补充访问用途。",
            }
        ],
    )
    result, calls = _evaluate(settings, _payload("v3-16"), llm=llm)
    item = _item(result)
    assert item.decision == "合规"
    assert item.decision_trace.semantic_effect == "question_only"
    assert item.decision_trace.semantic_finding_ids == ["v3-16-missing-1"]


def test_v3_17_semantic_observation_only(settings: Settings) -> None:
    from tests.test_architecture_baseline import _policy_gap_response

    llm = RecordingLlmClient(
        semantic_response=_policy_gap_response("v3-17-001")
    )
    # semantic effect 目录要求键集合完备；仅将目标缺口改为 observe_only
    effects = {
        "fact_conflict": "review_required",
        "contradiction": "review_required",
        "temporary_permanent_conflict": "observe_only",
        "purpose_target_mismatch": "review_required",
        "mixed_business_context": "review_required",
        "approval_scope_mismatch": "review_required",
        "unclassified_privileged_access": "review_required",
    }
    result, calls = _evaluate(
        settings,
        _payload(
            "v3-17",
            request_description="临时开通 30 天的 HTTPS 访问",
        ),
        llm=llm,
        semantic_effects=effects,
    )
    item = _item(result)
    assert item.decision == "合规"
    assert item.decision_trace.semantic_effect == "observation_only"


def test_v3_18_semantic_failure(settings: Settings) -> None:
    llm = RecordingLlmClient(fail_stage="semantic")
    result, calls = _evaluate(settings, _payload("v3-18"), llm=llm)
    item = _item(result)
    assert (item.decision, item.reason_code) == (
        "待定",
        "LLM_SEMANTIC_ANALYSIS_FAILURE",
    )
    assert calls.reduce == 1


def test_v3_19_rule_pending_with_semantic_failure(settings: Settings) -> None:
    llm = RecordingLlmClient(fail_stage="semantic")
    result, calls = _evaluate(settings, _telnet("v3-19"), llm=llm)
    item = _item(result)
    # 语义失败不得覆盖确定性 primary
    assert item.reason_code == "PORT-001"
    assert item.decision_trace.deterministic_decision == "待定"
    assert item.decision_trace.semantic_effect == "semantic_failure"
    assert calls.reduce == 1


def test_v3_20_fabricated_semantic_rule(settings: Settings) -> None:
    llm = RecordingLlmClient(
        semantic_response=LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": ["v3-20-001"],
                "claims": [],
                "candidate_rule_ids": ["FABRICATED-RULE-999"],
                "policy_gaps": [],
            }
        )
    )
    result, calls = _evaluate(settings, _payload("v3-20"), llm=llm)
    item = _item(result)
    # 虚构规则触发守卫失败：冻结的 failure code，且 matched_rules 无虚构规则
    assert item.reason_code == "LLM_SEMANTIC_ANALYSIS_FAILURE"
    assert item.decision == "待定"
    assert [rule.id for rule in item.matched_rules] == []
    assert calls.reduce == 1


# ---------------------------------------------------------------------------
# V3-21..V3-22: multi-source findings
# ---------------------------------------------------------------------------


def test_v3_21_port_rule_single_finding(settings: Settings) -> None:
    result, calls = _evaluate(settings, _telnet("v3-21"))
    item = _item(result)
    assert item.reason_code == "PORT-001"
    assert _finding_codes(item) == ["PORT-001"]


def test_v3_22_three_finding_sources_coexist(settings: Settings) -> None:
    # any 源 + telnet + 语义冲突：network(rule 类)/rule/semantic
    payload = _payload(
        "v3-22",
        sources=[{"address": "any", "description": "全来源"}],
        destinations=[{"address": "16.220.16.20", "description": "设备"}],
        ports=[{"start": 23, "end": 23}],
        request_description="Telnet 管理",
    )
    conflict = LlmSemanticResponse.model_validate(
        {
            "analyzed_item_ids": ["v3-22-001"],
            "claims": [
                {
                    "claim_id": "v3-22-conflict",
                    "scope": "v3-22-001",
                    "claim_type": "destination_zone",
                    "value": "鳌峰科创生产区",
                    "source": "request_description",
                    "evidence": "Telnet 管理",
                    "confidence": 0.9,
                }
            ],
            "candidate_rule_ids": [],
            "policy_gaps": [],
        }
    )
    llm = RecordingLlmClient(semantic_response=conflict)
    result, calls = _evaluate(settings, payload, llm=llm)
    item = _item(result)
    assert item.reason_code == "PORT-001"
    codes = _finding_codes(item)
    assert "ADDRESS_ANY" in codes
    assert "PORT-001" in codes
    assert "SEMANTIC_FACT_CONFLICT" in codes
    assert {f.source for f in item.decision_findings} == {
        "network",
        "rule",
        "semantic",
    }


# ---------------------------------------------------------------------------
# V3-23..V3-24: admission limits (API level, zero dependency calls)
# ---------------------------------------------------------------------------


def test_v3_23_item_limit_rejects_before_dependencies(settings: Settings) -> None:
    limited = _mock_chain(settings, max_evaluation_items=3)
    payload = _payload("v3-23")
    payload["sources"].append({"address": "16.201.1.20", "description": "应用 2"})
    payload["destinations"].append({"address": "16.220.16.30", "description": "数据库 2"})
    with TestClient(create_app(limited)) as client:
        runtime = client.app.state.runtime
        response = client.post("/v1/evaluations", json=payload)
        provider_calls = list(
            runtime.network_plan_resolver.provider.transport.calls
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EVALUATION_ITEM_LIMIT_EXCEEDED"
    assert provider_calls == []


def test_v3_24_query_limit_rejects_before_provider(settings: Settings) -> None:
    limited = _mock_chain(settings, network_plan_max_subnets_per_request=1)
    with TestClient(create_app(limited)) as client:
        runtime = client.app.state.runtime
        response = client.post("/v1/evaluations", json=_payload("v3-24"))
        provider_calls = list(
            runtime.network_plan_resolver.provider.transport.calls
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
    assert provider_calls == []


# ---------------------------------------------------------------------------
# V3-25: cross-region CIDR stays segmented
# ---------------------------------------------------------------------------


def test_v3_25_cross_region_cidr_is_segmented(settings: Settings) -> None:
    # 16.201.0.0/22 横跨两个区域与一个 404 /24：按冻结语义拆为三段，
    # 申请范围不得扩大为聚合超网
    result, calls = _evaluate(
        settings,
        _payload("v3-25", sources=[{"address": "16.201.0.0/22", "description": "网段"}]),
    )
    assert len(result.response.items) == 3
    accesses = {item.access.source for item in result.response.items}
    assert accesses == {"16.201.0.0/23", "16.201.2.0/24", "16.201.3.0/24"}
    assert "16.201.0.0/22" not in accesses


# ---------------------------------------------------------------------------
# V3-26..V3-28: provider equivalence / post-decision robustness
# ---------------------------------------------------------------------------


def test_v3_26_mock_and_offline_equivalent_facts(settings: Settings) -> None:
    import json
    import tempfile

    from tests.test_provider_unification import (
        EQUIVALENT_FIXTURE,
        _keys,
        _same_request,
    )

    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "equivalent.v1.json"
        fixture.write_text(
            json.dumps(EQUIVALENT_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        mock_settings = _mock_chain(settings, network_plan_mock_file=fixture)
        offline_settings = replace(
            settings, network_plan_client_mode="offline_catalog"
        )
        with TestClient(create_app(offline_settings)) as client:
            response = client.post(
                "/v1/evaluations", json=_same_request("v3-26-offline")
            )
            offline_status, offline_body = response.status_code, response.json()
        with TestClient(create_app(mock_settings)) as client:
            response = client.post(
                "/v1/evaluations", json=_same_request("v3-26-mock")
            )
            mock_status, mock_body = response.status_code, response.json()

    assert offline_status == mock_status == 200
    assert _keys(offline_body["items"][0]) == _keys(mock_body["items"][0])


def test_v3_27_explanation_failure_keeps_formal_decision(settings: Settings) -> None:
    llm = RecordingLlmClient(fail_stage="explanation")
    result, calls = _evaluate(settings, _payload("v3-27"), llm=llm)
    item = _item(result)
    assert item.decision == "合规"
    assert item.explanation_source == "template"


def test_v3_28_all_shadows_fail_response_still_succeeds(settings: Settings) -> None:
    class AllFail(RecordingLlmClient):
        async def analyze_request_findings(self, inputs, *, request_id=None):
            raise LlmDependencyError("request findings failure")

        async def explain(self, payload):
            raise LlmDependencyError("explanation failure")

    result, calls = _evaluate(
        settings,
        _payload("v3-28"),
        llm=AllFail(),
        llm_request_findings_mode="shadow",
    )
    item = _item(result)
    assert result.response.decision == "合规"
    assert item.decision == "合规"
    assert result.exceptions == [
        "LLM request findings: request findings failure",
        "LLM explanation: explanation failure",
    ]


# ---------------------------------------------------------------------------
# V3-29..V3-30: request aggregation
# ---------------------------------------------------------------------------


def test_v3_29_mixed_items_yield_pending_request(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload(
            "v3-29",
            sources=[
                {"address": "16.201.1.10", "description": "应用"},
                {"address": "16.201.3.10", "description": "应用 2"},
            ],
        ),
    )
    assert [item.decision for item in result.response.items] == ["合规", "待定"]
    assert result.response.decision == "待定"


def test_v3_30_all_compliant_items_yield_compliant_request(settings: Settings) -> None:
    result, calls = _evaluate(
        settings,
        _payload(
            "v3-30",
            sources=[
                {"address": "16.201.1.10", "description": "应用"},
                {"address": "16.201.2.10", "description": "应用 2"},
            ],
        ),
    )
    assert [item.decision for item in result.response.items] == ["合规", "合规"]
    assert result.response.decision == "合规"


# ---------------------------------------------------------------------------
# V3-31..V3-33: V4 additions (offline conflict / order freeze / audit findings)
# ---------------------------------------------------------------------------


def test_v3_31_offline_multi_match_frozen(settings: Settings, tmp_path: Path) -> None:
    import shutil

    policy_dir = tmp_path / "policies"
    shutil.copytree(PROJECT_ROOT / "policies", policy_dir)
    (policy_dir / "network_catalog.yaml").write_text(
        'version: "2026.09.0"\n'
        "networks:\n"
        "  - id: OVERLAP-A\n"
        "    cidr: 16.9.0.0/24\n"
        "    zone: zone-a\n"
        "    environment: production\n"
        "    object_type: application\n"
        "    labels: [a]\n"
        "  - id: OVERLAP-B\n"
        "    cidr: 16.9.0.0/24\n"
        "    zone: zone-b\n"
        "    environment: production\n"
        "    object_type: application\n"
        "    labels: [b]\n",
        encoding="utf-8",
    )
    offline = replace(
        settings,
        network_plan_client_mode="offline_catalog",
        policy_dir=policy_dir,
    )
    payload = _payload(
        "v3-31",
        sources=[{"address": "16.9.0.5", "description": "重叠网段"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
    )
    with TestClient(create_app(offline)) as client:
        response = client.post("/v1/evaluations", json=payload)
        body = response.json()
    assert response.status_code == 200
    assert body["items"][0]["reason_code"] == "NETWORK_PLAN_INVALID_RESPONSE"


def test_v3_32_exceptions_and_stage_metrics_order_frozen(settings: Settings) -> None:
    result, calls = _evaluate(settings, _payload("v3-32"))
    # shadow off：metrics 键顺序 = semantic, request_findings, explanation
    # （off 占位）；ACL 阶段移除后不再有 acl_candidates 键
    assert list(result.model_raw["stages"]) == [
        "semantic",
        "request_findings",
        "explanation",
    ]
    assert result.exceptions == []


def test_v3_33_audit_records_full_decision_findings(settings: Settings) -> None:
    result, calls = _evaluate(settings, _telnet("v3-33"))
    serialized = result.response.model_dump(mode="json")
    item = serialized["items"][0]
    assert [f["code"] for f in item["decision_findings"]] == ["PORT-001"]
    primaries = [f for f in item["decision_findings"] if f["is_primary"]]
    assert len(primaries) == 1
    assert primaries[0]["code"] == item["reason_code"]
