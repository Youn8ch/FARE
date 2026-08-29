"""V4-P0 characterization: freeze the a70ea45 observable main-chain behavior.

These tests complement tests/test_architecture_baseline.py (AC-00) by pinning
the structural and hidden outputs listed in docs/v3-baseline.md §2-§5. Later
V4 phases keep them green unless the plan explicitly approves the change
(approved changes are pre-registered in docs/v3-baseline.md §6 and must be
updated in the same commit).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, create_app
from app.schemas import LlmSemanticResponse
from app.services.rule_loader import PolicyBundle
from tests.helpers.llm import LlmDependencyError, RecordingLlmClient
from tests.test_architecture_baseline import (
    ACL_NO_PATH_FIXTURE,
    _mock_chain,
    _payload,
)
from tests.test_evaluator_orchestration import (
    RecordingAclClient,
    RecordingDecisionReducer,
    StageRecorder,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# a70ea45 真实阶段顺序（docs/v3-baseline.md §2）
BASELINE_STAGES = [
    "plan",
    "network",
    "acl",
    "rules",
    "semantic",
    "reduce",
    "explain",
    "assemble",
]

# model_raw["stages"] 固定记录四个 LLM 阶段（off 状态也占位），顺序即执行顺序
CURRENT_STAGES_METRICS_ORDER = [
    "semantic",
    "acl_candidates",
    "request_findings",
    "explanation",
]


class RecordingPolicyBundle:
    """Wrapper that records PolicyBundle.match() invocations."""

    def __init__(self, inner: PolicyBundle) -> None:
        self._inner = inner
        self.match_calls: list[str] = []

    def match(self, combination, total_combinations: int):
        self.match_calls.append(combination.source_text)
        return self._inner.match(combination, total_combinations)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class AllPostStagesFailingLlmClient(RecordingLlmClient):
    """Fails the three non-authoritative post-decision stages at once."""

    async def extract_acl_facts(self, inputs, *, request_id=None):
        raise LlmDependencyError("recorded ACL candidate failure")

    async def analyze_request_findings(self, inputs, *, request_id=None):
        raise LlmDependencyError("recorded request findings failure")

    async def explain(self, payload):
        raise LlmDependencyError("recorded explanation failure")


def _instrument(settings: Settings, **chain_changes):
    runtime = build_runtime(_mock_chain(settings, **chain_changes))
    recorder = StageRecorder()
    llm = RecordingLlmClient()
    acl = RecordingAclClient(runtime.evaluator.acl_client)
    reducer = RecordingDecisionReducer()
    policies = RecordingPolicyBundle(runtime.evaluator.policies)
    runtime.evaluator.llm_client = llm
    runtime.evaluator.acl_client = acl
    runtime.evaluator.decision_reducer = reducer
    runtime.evaluator.policies = policies
    runtime.evaluator._stage_observer = recorder
    return runtime, recorder, llm, acl, reducer, policies


def _close(runtime) -> None:
    asyncio.run(runtime.aclose())


def _run(runtime, payload: dict):
    return asyncio.run(runtime.evaluator.evaluate(_request(payload)))


def _request(payload: dict):
    from app.schemas import EvaluationRequest

    return EvaluationRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# P0-C01: normal HTTPS — full structural freeze
# ---------------------------------------------------------------------------


def test_p0c01_normal_https_freezes_stage_order_and_call_counts(
    settings: Settings,
) -> None:
    runtime, recorder, llm, acl, reducer, policies = _instrument(settings)
    try:
        result = _run(runtime, _payload("v4-p0-c01"))
    finally:
        _close(runtime)

    assert recorder.stages == BASELINE_STAGES
    assert len(result.response.items) == 1
    item = result.response.items[0]
    assert item.decision == "合规"
    assert result.response.decision == "合规"
    assert len(acl.calls) == 1
    assert llm.semantic_calls == 1
    assert llm.explanation_calls == 1
    # a70ea45 双裁决基线：V4-P2 将改为 1（docs/v3-baseline.md §6，D1）
    assert len(reducer.calls) == 2 * len(result.response.items)
    # a70ea45 双匹配基线：ACL gating 1 次 + Rules 阶段 1 次；V4-P1 将改为 1
    assert len(policies.match_calls) == 2
    assert result.model_raw["metrics"]["llm_added_pending_count"] == 0
    assert list(result.model_raw["stages"]) == CURRENT_STAGES_METRICS_ORDER
    assert result.model_raw["stages"]["acl_candidates"]["status"] == "off"
    assert result.model_raw["stages"]["request_findings"]["status"] == "off"
    assert result.model_raw["stages"]["semantic"]["status"] == "passed"
    assert result.model_raw["stages"]["explanation"]["status"] == "passed"


# ---------------------------------------------------------------------------
# P0-C02: network plan not found
# ---------------------------------------------------------------------------


def test_p0c02_network_not_found_freeze(settings: Settings) -> None:
    runtime, recorder, llm, acl, reducer, policies = _instrument(settings)
    try:
        result = _run(
            runtime,
            _payload(
                "v4-p0-c02",
                sources=[{"address": "16.201.3.10", "description": "应用"}],
            ),
        )
    finally:
        _close(runtime)

    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_type == "fact_incomplete"
    assert item.reason_code == "NETWORK_PLAN_NOT_FOUND"
    assert item.acl_verification_status == "skipped"
    assert acl.calls == []
    assert item.decision_trace is not None
    assert item.decision_trace.deterministic_decision == "待定"
    assert item.decision_trace.semantic_effect == "unchanged"
    # 网络阻断时 ACL gating 不匹配；Rules 阶段仍匹配一次
    assert len(policies.match_calls) == 1
    assert len(reducer.calls) == 2


# ---------------------------------------------------------------------------
# P0-C03: telnet + deterministic_pending_mode=skip
# ---------------------------------------------------------------------------


def test_p0c03_telnet_skip_mode_freeze(settings: Settings) -> None:
    runtime, recorder, llm, acl, reducer, policies = _instrument(
        settings, acl_deterministic_pending_mode="skip"
    )
    try:
        result = _run(
            runtime,
            _payload(
                "v4-p0-c03",
                destinations=[{"address": "16.220.16.20", "description": "设备"}],
                ports=[{"start": 23, "end": 23}],
                request_description="Telnet 管理",
            ),
        )
    finally:
        _close(runtime)

    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_code == "PORT-001"
    assert [rule.id for rule in item.matched_rules] == ["PORT-001"]
    assert item.acl_verification_status == "skipped"
    assert acl.calls == []
    # gating 需要一次匹配判定 skip；Rules 阶段再一次
    assert len(policies.match_calls) == 2
    assert len(reducer.calls) == 2


# ---------------------------------------------------------------------------
# P0-C04: ACL explicit no path
# ---------------------------------------------------------------------------


def test_p0c04_acl_explicit_no_path_freeze(settings: Settings) -> None:
    runtime, recorder, llm, acl, _reducer, _policies = _instrument(
        settings, acl_mock_file=ACL_NO_PATH_FIXTURE
    )
    try:
        result = _run(runtime, _payload("v4-p0-c04"))
    finally:
        _close(runtime)

    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_type == "acl_no_path"
    assert item.reason_code == "ACL-PATH-001"
    assert [rule.id for rule in item.matched_rules] == ["ACL-PATH-001"]
    assert item.acl_verification_status == "review_required"
    assert len(acl.calls) == 1


# ---------------------------------------------------------------------------
# P0-C05: semantic conflict downgrades a compliant item
# ---------------------------------------------------------------------------


def test_p0c05_semantic_conflict_downgrade_freeze(settings: Settings) -> None:
    item_id = "v4-p0-c05-001"
    runtime, recorder, llm, acl, reducer, _policies = _instrument(settings)
    runtime.evaluator.llm_client = RecordingLlmClient(
        semantic_response=LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": [item_id],
                "claims": [
                    {
                        "claim_id": "v4-p0-conflict-001",
                        "scope": item_id,
                        "claim_type": "source_zone",
                        "value": "核心生产区",
                        "source": "request_description",
                        "evidence": "HTTPS 访问",
                        "confidence": 0.9,
                    }
                ],
                "candidate_rule_ids": [],
                "policy_gaps": [],
            }
        )
    )
    try:
        result = _run(runtime, _payload("v4-p0-c05"))
    finally:
        _close(runtime)

    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_type == "fact_conflict"
    assert item.reason_code == "SEMANTIC_FACT_CONFLICT"
    trace = item.decision_trace
    assert trace is not None
    assert trace.deterministic_decision == "合规"
    assert trace.semantic_effect == "downgraded"
    assert trace.final_decision == "待定"
    assert trace.final_reason_code == "SEMANTIC_FACT_CONFLICT"
    # 降级指标基线：llm_added_pending_count = 1
    assert result.model_raw["metrics"]["llm_added_pending_count"] == 1
    assert len(reducer.calls) == 2
    assert len(acl.calls) == 1


# ---------------------------------------------------------------------------
# Hidden freeze: ACL-PATH-001 enters matched_rules only when primary
# ---------------------------------------------------------------------------


def test_p0_acl_path_rule_not_in_matched_rules_when_not_primary(
    settings: Settings,
) -> None:
    runtime, _recorder, _llm, acl, _reducer, _policies = _instrument(
        settings, acl_mock_file=ACL_NO_PATH_FIXTURE
    )
    try:
        result = _run(
            runtime,
            _payload(
                "v4-p0-path-secondary",
                destinations=[{"address": "16.220.16.20", "description": "设备"}],
                ports=[{"start": 23, "end": 23}],
                request_description="Telnet 管理",
            ),
        )
    finally:
        _close(runtime)

    item = result.response.items[0]
    # PORT-001（priority 10）压过 ACL-PATH-001（priority 30）
    assert item.reason_code == "PORT-001"
    assert [rule.id for rule in item.matched_rules] == ["PORT-001"]
    assert item.acl_verification_status == "review_required"
    assert len(acl.calls) == 1


# ---------------------------------------------------------------------------
# Hidden freeze: post-decision shadow stages run after reduce, before explain;
# their failures never change the business conclusion; exceptions order is
# acl_candidates -> request_findings -> explanation.
# ---------------------------------------------------------------------------


def test_p0_post_decision_failures_keep_business_result_and_exceptions_order(
    settings: Settings,
) -> None:
    shadowed = _mock_chain(
        settings,
        llm_acl_candidate_mode="shadow",
        llm_request_findings_mode="shadow",
    )
    runtime = build_runtime(shadowed)
    recorder = StageRecorder()
    llm = AllPostStagesFailingLlmClient()
    runtime.evaluator.llm_client = llm
    runtime.evaluator._stage_observer = recorder
    try:
        result = _run(runtime, _payload("v4-p0-shadow-failures"))
    finally:
        _close(runtime)

    item = result.response.items[0]
    assert item.decision == "合规"
    assert result.response.decision == "合规"
    assert item.reason_code is None
    assert item.explanation_source == "template"
    assert result.exceptions == [
        "LLM ACL candidates: recorded ACL candidate failure",
        "LLM request findings: recorded request findings failure",
        "LLM explanation: recorded explanation failure",
    ]
    assert list(result.model_raw["stages"]) == CURRENT_STAGES_METRICS_ORDER
    stages = result.model_raw["stages"]
    assert stages["acl_candidates"]["status"] == "rejected"
    assert stages["request_findings"]["status"] == "rejected"
    assert stages["explanation"]["status"] == "rejected"
    metrics = result.model_raw["metrics"]
    assert metrics["explanation_fallback_count"] == 1
    assert metrics["model_dependency_failure_count"] == 3


# ---------------------------------------------------------------------------
# Hidden freeze: offline_catalog masqueraded region fields
# ---------------------------------------------------------------------------


def test_p0_offline_masquerade_freeze(settings: Settings) -> None:
    offline = replace(settings, network_plan_client_mode="offline_catalog")
    payload = _payload(
        "v4-p0-offline-masquerade",
        sources=[{"address": "16.1.30.10", "description": "生产应用 A"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
        request_description="生产应用 HTTPS 访问",
    )
    with TestClient(create_app(offline)) as client:
        response = client.post("/v1/evaluations", json=payload)
        body = response.json()
    assert response.status_code == 200
    assert body["decision"] == "合规"
    source_region = body["network_analysis"]["source_regions"][0]
    destination_region = body["network_analysis"]["destination_regions"][0]
    # 伪装冻结：zone -> area/areaId/regionName；environment -> platformName
    assert source_region["area_id"] == "production"
    assert source_region["region_name"] == "production"
    assert source_region["platform_name"] == "production"
    assert destination_region["area_id"] == "production"
    assert destination_region["platform_name"] == "production"


# ---------------------------------------------------------------------------
# Hidden freeze: offline catalog multi-match resolves to
# NETWORK_PLAN_INVALID_RESPONSE today
# ---------------------------------------------------------------------------


def _write_conflicting_catalog(tmp_path: Path) -> Path:
    import shutil

    policy_dir = tmp_path / "policies"
    shutil.copytree(PROJECT_ROOT / "policies", policy_dir)
    catalog = policy_dir / "network_catalog.yaml"
    catalog.write_text(
        'version: "2026.08.0"\n'
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
    return policy_dir


def test_p0_offline_multi_match_freeze(settings: Settings, tmp_path: Path) -> None:
    offline = replace(
        settings,
        network_plan_client_mode="offline_catalog",
        policy_dir=_write_conflicting_catalog(tmp_path),
    )
    payload = _payload(
        "v4-p0-offline-conflict",
        sources=[{"address": "16.9.0.5", "description": "重叠网段"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
    )
    with TestClient(create_app(offline)) as client:
        response = client.post("/v1/evaluations", json=payload)
        body = response.json()
    assert response.status_code == 200
    item = body["items"][0]
    assert item["decision"] == "待定"
    assert item["reason_code"] == "NETWORK_PLAN_INVALID_RESPONSE"
    assert item["source_network_fact_status"] == "invalid_response"
    assert item["acl_verification_status"] == "skipped"
