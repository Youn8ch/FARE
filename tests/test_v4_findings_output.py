"""V4-P4 acceptance: findings become auditable business output (plan §11).

- P4-C01: PORT-001 + ACL-PATH-001 both survive into ``decision_findings``
  while ``matched_rules`` keeps its frozen compatibility semantics
  (ACL-PATH-001 only when primary);
- P4-C02: network/catalog + rule + ACL + semantic findings coexist;
- P4-C03: informational findings (``affects_decision=False``) are recorded
  without changing the decision (unit level on the formal reduce contract);
- every finding maps one-to-one into the API field and the audit record.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: F401 - kept for parity with sibling suites

from app.config import Settings
from app.main import build_runtime
from app.schemas import EvaluationRequest, LlmSemanticResponse
from app.services.decision_reducer import (
    PRIORITY_SEMANTIC,
    Finding,
    ItemFindingSet,
    SemanticTrace,
)
from tests.helpers.llm import RecordingLlmClient
from tests.test_architecture_baseline import ACL_NO_PATH_FIXTURE, _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _evaluate(settings: Settings, payload: dict, llm=None):
    runtime = build_runtime(_mock_chain(settings, acl_mock_file=ACL_NO_PATH_FIXTURE))
    if llm is not None:
        runtime.evaluator.llm_client = llm
    try:
        return asyncio.run(runtime.evaluator.evaluate(EvaluationRequest.model_validate(payload)))
    finally:
        asyncio.run(runtime.aclose())


def _finding_codes(item) -> list[str]:
    return [finding.code for finding in item.decision_findings]


# ---------------------------------------------------------------------------
# P4-C01: rule pending + ACL no path — both findings survive
# ---------------------------------------------------------------------------


def test_p4c01_rule_and_acl_findings_both_survive(settings: Settings) -> None:
    result = _evaluate(
        settings,
        _payload(
            "v4-p4-c01",
            destinations=[{"address": "16.220.16.20", "description": "设备"}],
            ports=[{"start": 23, "end": 23}],
            request_description="Telnet 管理",
        ),
    )
    item = result.response.items[0]
    assert item.decision == "待定"
    assert item.reason_code == "PORT-001"
    # 兼容语义冻结：ACL-PATH-001 不是 primary，不进 matched_rules
    assert [rule.id for rule in item.matched_rules] == ["PORT-001"]
    # 但两个 finding 都进入 decision_findings，primary 唯一
    assert _finding_codes(item) == ["PORT-001", "ACL-PATH-001"]
    assert [finding.is_primary for finding in item.decision_findings] == [True, False]
    assert item.decision_findings[0].source == "rule"
    assert item.decision_findings[1].source == "acl"
    # 审计完整性：序列化 response（即 audit final_response 的来源）含同一 findings
    serialized = result.response.model_dump(mode="json")
    audit_codes = [
        finding["code"] for finding in serialized["items"][0]["decision_findings"]
    ]
    assert audit_codes == ["PORT-001", "ACL-PATH-001"]


# ---------------------------------------------------------------------------
# P4-C02: catalog(any) + rule + ACL + semantic findings coexist
# ---------------------------------------------------------------------------


def test_p4c02_four_finding_sources_coexist(settings: Settings) -> None:
    item_id = "v4-p4-c02-001"
    llm = RecordingLlmClient(
        semantic_response=LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": [item_id],
                "claims": [
                    {
                        "claim_id": "v4-p4-conflict-001",
                        "scope": item_id,
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
    )
    result = _evaluate(
        settings,
        _payload(
            "v4-p4-c02",
            sources=[{"address": "any", "description": "全来源"}],
            destinations=[{"address": "16.220.16.20", "description": "设备"}],
            ports=[{"start": 23, "end": 23}],
            request_description="Telnet 管理",
        ),
        llm=llm,
    )
    item = result.response.items[0]
    assert item.item_id == item_id
    # primary 是最高优先级规则（PORT-001），其余全部保留
    assert item.decision == "待定"
    assert item.reason_code == "PORT-001"
    codes = _finding_codes(item)
    # 历史首个命中顺序：matched rules -> catalog -> ACL -> semantic
    assert codes[:3] == ["PORT-001", "LEAST-ANY-001", "ADDRESS_ANY"]
    assert "ACL-PATH-001" in codes
    assert "SEMANTIC_FACT_CONFLICT" in codes
    sources = {finding.source for finding in item.decision_findings}
    assert sources == {"network", "rule", "acl", "semantic"}
    # reason_code 与 primary 一一对应
    primaries = [f for f in item.decision_findings if f.is_primary]
    assert len(primaries) == 1
    assert primaries[0].code == item.reason_code


# ---------------------------------------------------------------------------
# P4-C03: informational findings never change a decision (unit level)
# ---------------------------------------------------------------------------


def test_p4c03_informational_finding_is_recorded_without_decision_change() -> None:
    from app.services.decision_reducer import DecisionReducer

    reducer = DecisionReducer()
    informational = Finding(
        code="SEMANTIC_OBSERVATION",
        source="semantic",
        reason_type="risk_uncertain",
        detail="observation-001",
        priority=PRIORITY_SEMANTIC,
        affects_decision=False,
    )
    decision = reducer.reduce_item(
        ItemFindingSet(semantic=(informational,)),
        matched_rules=(),
        semantic=SemanticTrace(succeeded=True),
    )
    assert decision.decision == "合规"
    assert decision.primary_finding is None
    assert decision.deterministic_decision == "合规"
    assert decision.trace is not None
    assert decision.trace.final_decision == "合规"
    # finding 仍完整保留，映射为 is_primary=False 的可审计输出
    assert [f.code for f in decision.findings] == ["SEMANTIC_OBSERVATION"]
    assert decision.findings[0].affects_decision is False


# ---------------------------------------------------------------------------
# Every finding maps one-to-one; compliant items carry the (empty) field
# ---------------------------------------------------------------------------


def test_p4_findings_one_to_one_and_trace_reason_consistency(settings: Settings) -> None:
    result = _evaluate(settings, _payload("v4-p4-consistency"))
    for item in result.response.items:
        assert item.decision_findings is not None
        primaries = [f for f in item.decision_findings if f.is_primary]
        assert len(primaries) <= 1
        if item.decision == "待定":
            assert len(primaries) == 1
            assert primaries[0].code == item.reason_code
            assert primaries[0].reason_type == item.reason_type
        else:
            assert primaries == []
