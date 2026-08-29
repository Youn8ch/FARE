"""AC-04 acceptance: DecisionReducer is the only formal decision entry.

F-01..F-05 are the plan's simulated finding inputs (direct internal tests, no
HTTP); the wiring tests prove the evaluator feeds stage findings through the
reducer and preserves the AC-00 API baseline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.decision_reducer import (
    PRIORITY_ACL,
    PRIORITY_NETWORK,
    PRIORITY_RULE,
    PRIORITY_SEMANTIC,
    DecisionReducer,
    Finding,
)
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _network(code: str, reason_type: str) -> Finding:
    return Finding(code=code, source="network", reason_type=reason_type, priority=PRIORITY_NETWORK)


def _rule(code: str, reason_type: str = "policy_violation") -> Finding:
    return Finding(code=code, source="rule", reason_type=reason_type, priority=PRIORITY_RULE)


def _acl(code: str, reason_type: str) -> Finding:
    return Finding(code=code, source="acl", reason_type=reason_type, priority=PRIORITY_ACL)


def _semantic(code: str, reason_type: str, *, affects: bool = True) -> Finding:
    return Finding(
        code=code,
        source="semantic",
        reason_type=reason_type,
        priority=PRIORITY_SEMANTIC,
        affects_decision=affects,
    )


def test_f01_network_wins_over_rule_acl_semantic() -> None:
    findings = [
        _network("NETWORK_PLAN_NOT_FOUND", "fact_incomplete"),
        _rule("PORT-001"),
        _acl("ACL-PATH-001", "acl_no_path"),
        _semantic("SEMANTIC_FACT_CONFLICT", "fact_conflict"),
    ]
    result = DecisionReducer().reduce(findings)
    assert result.decision == "待定"
    assert result.primary_finding is not None
    assert result.primary_finding.code == "NETWORK_PLAN_NOT_FOUND"
    assert result.reason_type == "fact_incomplete"
    assert result.reason_code == "NETWORK_PLAN_NOT_FOUND"
    assert {finding.code for finding in result.findings} >= {
        "NETWORK_PLAN_NOT_FOUND",
        "PORT-001",
        "ACL-PATH-001",
        "SEMANTIC_FACT_CONFLICT",
    }


def test_f02_rule_wins_over_acl_semantic() -> None:
    findings = [
        _rule("PORT-001"),
        _acl("ACL-PATH-001", "acl_no_path"),
        _semantic("SEMANTIC_FACT_CONFLICT", "fact_conflict"),
    ]
    result = DecisionReducer().reduce(findings)
    assert result.decision == "待定"
    assert result.primary_finding is not None
    assert result.primary_finding.code == "PORT-001"
    assert result.reason_type == "policy_violation"


def test_f03_acl_wins_over_semantic() -> None:
    findings = [
        _acl("ACL-PATH-001", "acl_no_path"),
        _semantic("SEMANTIC_FACT_CONFLICT", "fact_conflict"),
    ]
    result = DecisionReducer().reduce(findings)
    assert result.decision == "待定"
    assert result.primary_finding is not None
    assert result.primary_finding.code == "ACL-PATH-001"
    assert result.reason_type == "acl_no_path"


def test_f04_semantic_conflict_downgrades_compliant() -> None:
    result = DecisionReducer().reduce(
        [_semantic("SEMANTIC_FACT_CONFLICT", "fact_conflict")]
    )
    assert result.decision == "待定"
    assert result.primary_finding is not None
    assert result.primary_finding.code == "SEMANTIC_FACT_CONFLICT"


def test_f05_only_semantic_policy_gap_candidate_stays_compliant() -> None:
    result = DecisionReducer().reduce(
        [_semantic("SEMANTIC_POLICY_GAP", "risk_uncertain", affects=False)]
    )
    assert result.decision == "合规"
    assert result.primary_finding is None
    assert result.reason_code is None
    # 信息性 finding 仍然完整保留用于审计
    assert [finding.code for finding in result.findings] == ["SEMANTIC_POLICY_GAP"]


def test_secondary_findings_never_override_higher_priority_primary() -> None:
    # 乱序输入：优先级仍由阶段决定，与插入顺序无关
    findings = [
        _semantic("SEMANTIC_FACT_CONFLICT", "fact_conflict"),
        _acl("ACL-PATH-001", "acl_no_path"),
        _rule("PORT-001"),
        _network("NETWORK_PLAN_NOT_FOUND", "fact_incomplete"),
    ]
    result = DecisionReducer().reduce(findings)
    assert result.primary_finding is not None
    assert result.primary_finding.code == "NETWORK_PLAN_NOT_FOUND"


def test_finding_order_is_stable_within_a_stage() -> None:
    findings = [_rule("PORT-001"), _rule("LEAST-PORT-001")]
    result = DecisionReducer().reduce(findings)
    assert result.primary_finding is not None
    assert result.primary_finding.code == "PORT-001"
    assert [finding.code for finding in result.findings] == [
        "PORT-001",
        "LEAST-PORT-001",
    ]


def test_matched_rules_are_preserved_verbatim() -> None:
    result = DecisionReducer().reduce(
        [_rule("PORT-001"), _rule("LEAST-PORT-001")],
        matched_rules=["PORT-001", "LEAST-PORT-001"],
    )
    assert result.matched_rules == ("PORT-001", "LEAST-PORT-001")


def test_evaluator_wiring_primary_finding_matches_api_baseline(
    settings: Settings,
) -> None:
    """接线验证：CASE-02 的内部 Decision 与 API 输出同源。"""

    from app.main import build_runtime
    from app.schemas import EvaluationRequest

    runtime = build_runtime(_mock_chain(settings))
    try:
        assert runtime.evaluator.decision_reducer.__class__ is DecisionReducer
        request = EvaluationRequest.model_validate(
            _payload(
                "ac04-case-02",
                sources=[{"address": "16.201.3.10", "description": "应用"}],
            )
        )
        result = asyncio.run(runtime.evaluator.evaluate(request))
        item = result.response.items[0]
        assert item.decision == "待定"
        assert item.reason_code == "NETWORK_PLAN_NOT_FOUND"
        assert result.response.decision == "待定"
    finally:
        asyncio.run(runtime.aclose())


def test_api_cases_keep_baseline_outputs(settings: Settings) -> None:
    cases = {
        "ac04-case-05-telnet": (
            _payload(
                "ac04-case-05-telnet",
                destinations=[{"address": "16.220.16.20", "description": "设备"}],
                ports=[{"start": 23, "end": 23}],
                request_description="Telnet 管理",
            ),
            "待定",
            "PORT-001",
        ),
        "ac04-case-09-no-path": (
            _payload("ac04-case-09-no-path"),
            "合规",
            None,
        ),
    }
    with TestClient(create_app(_mock_chain(settings))) as client:
        responses = {
            name: client.post("/v1/evaluations", json=payload).json()
            for name, (payload, _, _) in cases.items()
        }
    case05 = responses["ac04-case-05-telnet"]
    assert case05["items"][0]["decision"] == "待定"
    assert case05["items"][0]["reason_code"] == "PORT-001"
    assert case05["items"][0]["matched_rules"][0]["id"] == "PORT-001"
    case09 = responses["ac04-case-09-no-path"]
    assert case09["items"][0]["decision"] == "合规"
    assert case09["items"][0]["reason_code"] is None
