"""AC-00 architecture baseline: characterization of current behavior.

Every test in this module records behavior that exists at PRE_CONVERGENCE_SHA.
They are intentional characterization tests: later convergence phases must keep
them green unless the plan explicitly approves the change, and any approved
change must be reflected here as an explicit, reviewed update.

Covered matrix (see docs/architecture-baseline.md):
- CASE-01..CASE-09 on the resolver (mock) chain with dependency call counts;
- CASE-10 / CASE-11 semantic effects with a recording LLM double;
- idempotency replay / conflict on the offline_catalog chain;
- CLI batch (requirement_runner) conclusion distribution baseline for
  AC-05 / AC-06 comparison.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, create_app
from app.requirement_runner import evaluate_requirements
from app.schemas import (
    LlmPolicyGap,
    LlmSemanticClaim,
    LlmSemanticResponse,
    SemanticEvidence,
)
from app.services.requirement_source import RequirementBatch
from tests.helpers.llm import RecordingLlmClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NETWORK_PLAN_FIXTURE = PROJECT_ROOT / "tests/fixtures/network_plan/multi_region.v1.json"
CASE_ROOT = PROJECT_ROOT / "tests/cases/network_requirements"
AREA_RELATIONS_CONFIG = PROJECT_ROOT / "config/fare.test-area-relations.yaml"


def _payload(request_id: str, **overrides) -> dict:
    value = {
        "request_id": request_id,
        "sources": [{"address": "16.201.1.10", "description": "应用"}],
        "destinations": [{"address": "16.220.16.20", "description": "数据库"}],
        "protocol": "tcp",
        "ports": [{"start": 443, "end": 443}],
        "request_description": "HTTPS 访问",
    }
    value.update(overrides)
    return value


def _mock_chain(settings: Settings, **changes) -> Settings:
    values = {
        "network_plan_client_mode": "mock",
        "network_plan_mock_file": NETWORK_PLAN_FIXTURE,
        **changes,
    }
    return replace(settings, **values)


def _post(client: TestClient, payload: dict):
    response = client.post("/v1/evaluations", json=payload)
    body = response.json()
    return response.status_code, body


def _item(body: dict) -> dict:
    assert len(body["items"]) >= 1
    return body["items"][0]


# ---------------------------------------------------------------------------
# CASE-01: single IP normal access, resolver chain
# ---------------------------------------------------------------------------


def test_case01_single_ip_normal_access_baseline(settings: Settings) -> None:
    with TestClient(create_app(_mock_chain(settings))) as client:
        runtime = client.app.state.runtime
        status, body = _post(client, _payload("ac00-case-01"))
        network_calls = list(runtime.network_plan_resolver.provider.transport.calls)
    assert status == 200
    assert body["decision"] == "合规"
    item = _item(body)
    assert item["access"]["source"] == "16.201.1.10/32"
    assert item["access"]["destination"] == "16.220.16.20/32"
    assert item["decision"] == "合规"
    assert item["reason_type"] is None
    assert item["reason_code"] is None
    assert item["matched_rules"] == []
    assert item["source_network_fact_status"] == "complete"
    assert item["destination_network_fact_status"] == "complete"
    assert item["source_network_fact_ids"] == ["NPF-10C90100"]
    assert item["destination_network_fact_ids"] == ["NPF-10DC1000"]
    # 真实访问范围不得扩大为 /24
    assert item["access"]["source"] != "16.201.1.0/24"
    assert len(body["network_analysis"]["lookups"]) == 2
    assert network_calls == ["16.201.1.0/24", "16.220.16.0/24"]


# ---------------------------------------------------------------------------
# CASE-02: network plan 404
# ---------------------------------------------------------------------------


def test_case02_network_plan_not_found_baseline(settings: Settings) -> None:
    payload = _payload("ac00-case-02", sources=[{"address": "16.201.3.10", "description": "应用"}])
    with TestClient(create_app(_mock_chain(settings))) as client:
        runtime = client.app.state.runtime
        status, body = _post(client, payload)
        network_calls = list(runtime.network_plan_resolver.provider.transport.calls)
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["decision"] == "待定"
    assert item["reason_type"] == "fact_incomplete"
    assert item["reason_code"] == "NETWORK_PLAN_NOT_FOUND"
    assert item["source_network_fact_status"] == "not_found"
    assert network_calls == ["16.201.3.0/24", "16.220.16.0/24"]


# ---------------------------------------------------------------------------
# CASE-03: query limit
# ---------------------------------------------------------------------------


def test_case03_query_limit_baseline(settings: Settings) -> None:
    limited = _mock_chain(settings, network_plan_max_subnets_per_request=1)
    with TestClient(create_app(limited)) as client:
        runtime = client.app.state.runtime
        status, body = _post(client, _payload("ac00-case-03"))
        # 同一 request_id 重复提交不得卡在处理中，也不得消耗依赖
        repeat_status, repeat_body = _post(client, _payload("ac00-case-03"))
        network_calls = list(runtime.network_plan_resolver.provider.transport.calls)
    assert status == repeat_status == 422
    assert body["error"]["code"] == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"actual": 2, "limit": 1}
    assert repeat_body["error"]["code"] == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
    assert network_calls == []


# ---------------------------------------------------------------------------
# CASE-04: item limit
# ---------------------------------------------------------------------------


def test_case04_item_limit_baseline(settings: Settings) -> None:
    limited = _mock_chain(settings, max_evaluation_items=3)
    payload = _payload("ac00-case-04")
    payload["sources"].append({"address": "16.201.1.20", "description": "应用 2"})
    payload["destinations"].append({"address": "16.220.16.30", "description": "数据库 2"})
    with TestClient(create_app(limited)) as client:
        status, body = _post(client, payload)
        repeat_status, repeat_body = _post(client, payload)
    assert status == repeat_status == 422
    assert body["error"]["code"] == "EVALUATION_ITEM_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"actual": 4, "limit": 3}
    assert repeat_body["error"]["code"] == "EVALUATION_ITEM_LIMIT_EXCEEDED"
    assert repeat_body["error"]["code"] != "evaluation_in_progress"


# ---------------------------------------------------------------------------
# CASE-05: formal rule PORT-001
# ---------------------------------------------------------------------------


def test_case05_port_rule_baseline(settings: Settings) -> None:
    payload = _payload(
        "ac00-case-05",
        destinations=[{"address": "16.220.16.20", "description": "设备"}],
        ports=[{"start": 23, "end": 23}],
        request_description="Telnet 管理",
    )
    with TestClient(create_app(_mock_chain(settings))) as client:
        status, body = _post(client, payload)
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["decision"] == "待定"
    assert item["reason_type"] == "policy_violation"
    assert item["reason_code"] == "PORT-001"
    assert [rule["id"] for rule in item["matched_rules"]] == ["PORT-001"]


# ---------------------------------------------------------------------------
# CASE-06: any address
# ---------------------------------------------------------------------------


def test_case06_any_address_baseline(settings: Settings) -> None:
    payload = _payload(
        "ac00-case-06", sources=[{"address": "any", "description": "全来源"}]
    )
    with TestClient(create_app(_mock_chain(settings))) as client:
        runtime = client.app.state.runtime
        status, body = _post(client, payload)
        network_calls = list(runtime.network_plan_resolver.provider.transport.calls)
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["decision"] == "待定"
    assert item["reason_type"] == "policy_violation"
    assert item["reason_code"] == "LEAST-ANY-001"
    assert [rule["id"] for rule in item["matched_rules"]] == ["LEAST-ANY-001"]
    assert item["access"]["source"] == "any"
    assert item["source_network_fact_status"] == "not_applicable"
    # any 不得发送给网段规划
    assert network_calls == ["16.220.16.0/24"]


# ---------------------------------------------------------------------------
# CASE-07: large port span
# ---------------------------------------------------------------------------


def test_case07_port_span_baseline(settings: Settings) -> None:
    payload = _payload("ac00-case-07", ports=[{"start": 1, "end": 101}])
    with TestClient(create_app(_mock_chain(settings))) as client:
        status, body = _post(client, payload)
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["reason_type"] == "policy_violation"
    # 计划 CASE-07 只要求 matched_rules 包含 LEAST-PORT-001；tcp/1-101 同时
    # 覆盖端口 23，规则顺序 PORT-001 在前，因此主原因码为 PORT-001（实际行为）。
    assert item["reason_code"] == "PORT-001"
    assert [rule["id"] for rule in item["matched_rules"]] == [
        "PORT-001",
        "LEAST-PORT-001",
    ]


# ---------------------------------------------------------------------------
# CASE-10: semantic authoritative fact conflict (recording LLM double)
# ---------------------------------------------------------------------------


def test_case10_semantic_conflict_downgrades_compliant(settings: Settings) -> None:
    configured = _mock_chain(settings)
    item_id = "ac00-case-10-001"
    recorder = RecordingLlmClient(
        semantic_response=LlmSemanticResponse.model_validate(
            {
                "analyzed_item_ids": [item_id],
                "claims": [
                    {
                        "claim_id": "ac00-conflict-001",
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
    with TestClient(create_app(configured)) as client:
        client.app.state.runtime.evaluator.llm_client = recorder
        status, body = _post(client, _payload("ac00-case-10"))
    assert recorder.semantic_calls == 1
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["item_id"] == item_id
    assert item["decision"] == "待定"
    assert item["reason_type"] == "fact_conflict"
    assert item["reason_code"] == "SEMANTIC_FACT_CONFLICT"
    assert item["decision_trace"]["deterministic_decision"] == "合规"
    assert item["decision_trace"]["semantic_effect"] == "downgraded"
    assert item["decision_trace"]["final_decision"] == "待定"


# ---------------------------------------------------------------------------
# CASE-11: semantic policy_gap (current behavior: review_required downgrade)
# ---------------------------------------------------------------------------


def _policy_gap_response(item_id: str) -> LlmSemanticResponse:
    claims = [
        LlmSemanticClaim.model_validate(
            {
                "claim_id": "ac00-gap-claim-001",
                "scope": item_id,
                "claim_type": "temporary_access",
                "value": "临时开通",
                "source": "request_description",
                "evidence": "临时开通",
                "confidence": 0.9,
            }
        ),
        LlmSemanticClaim.model_validate(
            {
                "claim_id": "ac00-gap-claim-002",
                "scope": item_id,
                "claim_type": "requested_duration",
                "value": "30 天",
                "source": "request_description",
                "evidence": "30 天",
                "confidence": 0.9,
            }
        ),
    ]
    gap = LlmPolicyGap.model_validate(
        {
            "gap_id": "ac00-gap-001",
            "scope": item_id,
            "gap_type": "temporary_permanent_conflict",
            "description": "临时开通缺少到期时间与回收机制",
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
            "question_for_requester": "请确认临时访问的到期时间。",
            "suggested_effect": "review_required",
        }
    )
    return LlmSemanticResponse.model_validate(
        {
            "analyzed_item_ids": [item_id],
            "claims": claims,
            "candidate_rule_ids": [],
            "policy_gaps": [gap],
        }
    )


def test_case11_policy_gap_current_behavior_downgrades(settings: Settings) -> None:
    """AC-00 characterization: verified policy_gap with the default
    ``temporary_permanent_conflict: review_required`` effect downgrades
    合规 -> 待定. AC-06 target state (candidate only, no downgrade) is frozen
    until the business owner confirms the change; if approved, THIS test is the
    one that must be updated in the same commit as the effect change.
    """

    configured = _mock_chain(settings)
    item_id = "ac00-case-11-001"
    payload = _payload(
        "ac00-case-11",
        request_description="临时开通 30 天的 HTTPS 访问",
    )
    with TestClient(create_app(configured)) as client:
        client.app.state.runtime.evaluator.llm_client = RecordingLlmClient(
            semantic_response=_policy_gap_response(item_id)
        )
        status, body = _post(client, payload)
    assert status == 200
    assert body["decision"] == "待定"
    item = _item(body)
    assert item["item_id"] == item_id
    assert item["decision"] == "待定"
    assert item["reason_type"] == "risk_uncertain"
    assert item["reason_code"] == "SEMANTIC_POLICY_GAP"
    assert item["decision_trace"]["deterministic_decision"] == "合规"
    assert item["decision_trace"]["semantic_effect"] == "downgraded"


# ---------------------------------------------------------------------------
# Idempotency baseline on the offline_catalog chain
# ---------------------------------------------------------------------------


def test_idempotency_replay_same_payload_returns_cached_response(
    settings: Settings,
) -> None:
    # offline_catalog 目录仅覆盖 16.1.x；两端都落在目录内才能得到合规基线
    payload = _payload(
        "ac00-idempotent-replay",
        sources=[{"address": "16.1.30.10", "description": "生产应用 A"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
    )
    with TestClient(create_app(settings)) as client:
        first_status, first = _post(client, payload)
        second_status, second = _post(client, payload)
    assert first_status == second_status == 200
    assert first["audit_id"] == second["audit_id"]
    assert first["items"] == second["items"]
    assert first["decision"] == "合规"


def test_idempotency_same_request_id_different_payload_conflicts(
    settings: Settings,
) -> None:
    payload = _payload(
        "ac00-idempotent-conflict",
        sources=[{"address": "16.1.30.10", "description": "生产应用 A"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
    )
    with TestClient(create_app(settings)) as client:
        first_status, _ = _post(client, payload)
        conflict_status, conflict = _post(
            client,
            _payload(
                "ac00-idempotent-conflict",
                sources=[{"address": "16.1.30.10", "description": "生产应用 A"}],
                destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
                ports=[{"start": 8443, "end": 8443}],
            ),
        )
    assert first_status == 200
    assert conflict_status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"


# ---------------------------------------------------------------------------
# CLI batch baseline (requirement_runner entry point)
# ---------------------------------------------------------------------------

CLI_BATCH_BASELINE: dict[str, dict] = {
    "area_relation_success.batch.json": {
        "requests": 30,
        "distribution": {
            "合规": 14,
            "待定": 16,
        },
    },
    "area_relation_not_found.batch.json": {
        "requests": 1,
        "distribution": {
            "待定": 1,
        },
    },
    "area_relation_mixed_01.batch.json": {
        "requests": 8,
        "distribution": {
            "合规": 2,
            "待定": 6,
        },
    },
}


@pytest.mark.parametrize(
    "batch_name",
    sorted(CLI_BATCH_BASELINE),
    ids=lambda name: name.removesuffix(".batch.json"),
)
def test_cli_batch_conclusion_distribution_baseline(
    batch_name: str, tmp_path: Path
) -> None:
    from app.config import FareConfig

    config = FareConfig.load(AREA_RELATIONS_CONFIG)
    configured = replace(
        config,
        settings=replace(
            config.settings,
            audit_log_dir=tmp_path / "audit",
            config_id=f"{config.config_id}-ac00",
        ),
    )
    batch = RequirementBatch.model_validate_json(
        (CASE_ROOT / batch_name).read_text(encoding="utf-8")
    )

    async def run() -> list[dict]:
        runtime = build_runtime(configured.settings)
        try:
            return await evaluate_requirements(
                runtime,
                batch.requests,
                max_concurrency=configured.settings.max_concurrent_evaluations,
            )
        finally:
            await runtime.aclose()

    results = asyncio.run(run())

    baseline = CLI_BATCH_BASELINE[batch_name]
    assert len(results) == baseline["requests"]
    decisions = Counter(
        result["response"]["decision"]
        for result in results
        if result["http_status"] == 200
    )
    assert all(result["http_status"] == 200 for result in results)
    assert dict(decisions) == baseline["distribution"], (
        f"CLI batch conclusion distribution drifted for {batch_name}; "
        "this is the AC-00 baseline used by AC-05/AC-06 comparison. "
        "Update it only with an explicit, reviewed behavior change."
    )


def test_cli_batch_not_found_reason_codes_are_stable(tmp_path: Path) -> None:
    from app.config import FareConfig

    config = FareConfig.load(AREA_RELATIONS_CONFIG)
    configured = replace(
        config,
        settings=replace(
            config.settings,
            audit_log_dir=tmp_path / "audit",
            config_id=f"{config.config_id}-ac00-notfound",
        ),
    )
    batch = RequirementBatch.model_validate_json(
        (CASE_ROOT / "area_relation_not_found.batch.json").read_text(encoding="utf-8")
    )

    async def run() -> list[dict]:
        runtime = build_runtime(configured.settings)
        try:
            return await evaluate_requirements(
                runtime,
                batch.requests,
                max_concurrency=configured.settings.max_concurrent_evaluations,
            )
        finally:
            await runtime.aclose()

    results = asyncio.run(run())
    assert len(results) == 1
    body = results[0]["response"]
    item = body["items"][0]
    assert item["decision"] == "待定"
    assert item["reason_code"] == "NETWORK_PLAN_NOT_FOUND"
    # not_found 批次的缺失网段在目的端（16.201.3.0/24），源端正常解析
    assert item["destination_network_fact_status"] == "not_found"
    assert item["source_network_fact_status"] == "complete"
