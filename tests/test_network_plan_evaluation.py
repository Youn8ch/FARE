from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

FIXTURE = Path("tests/fixtures/network_plan/multi_region.v1.json").resolve()


def _payload(request_id: str, *, source: str = "16.201.1.10") -> dict:
    return {
        "request_id": request_id,
        "sources": [{"address": source, "description": "应用"}],
        "destinations": [{"address": "16.220.16.20", "description": "数据库"}],
        "protocol": "tcp",
        "ports": [{"start": 443, "end": 443}],
        "request_description": "HTTPS 访问",
    }


def _mock_settings(settings, **changes):
    return replace(
        settings,
        network_plan_client_mode="mock",
        network_plan_mock_file=FIXTURE,
        acl_decision_mode="advisory",
        **changes,
    )


def test_api_exposes_authoritative_facts_without_expanding_single_ip(settings) -> None:
    with TestClient(create_app(_mock_settings(settings))) as client:
        response = client.post(
            "/v1/evaluations", json=_payload("network-plan-e2e-single")
        )
    assert response.status_code == 200
    body = response.json()
    item = body["items"][0]
    assert item["access"]["source"] == "16.201.1.10/32"
    assert item["source_network_fact_ids"] == ["NPF-10C90100"]
    assert item["destination_network_fact_ids"] == ["NPF-10DC1000"]
    assert item["source_network_fact_status"] == "complete"
    assert len(body["network_analysis"]["lookups"]) == 2


def test_network_plan_404_is_primary_and_acl_is_skipped(settings) -> None:
    with TestClient(create_app(_mock_settings(settings))) as client:
        response = client.post(
            "/v1/evaluations",
            json=_payload("network-plan-e2e-not-found", source="16.201.3.10"),
        )
    item = response.json()["items"][0]
    assert item["reason_code"] == "NETWORK_PLAN_NOT_FOUND"
    assert item["source_network_fact_status"] == "not_found"
    assert item["acl_verification_status"] == "skipped"


def test_query_limit_rejects_before_idempotency_claim_and_dependencies(settings) -> None:
    limited = _mock_settings(settings, network_plan_max_subnets_per_request=1)
    with TestClient(create_app(limited)) as client:
        response = client.post(
            "/v1/evaluations", json=_payload("network-plan-query-limit")
        )
        assert client.app.state.runtime.network_plan_resolver.client.calls == []
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED",
        "message": "unique /24 query count exceeds the configured limit",
        "details": {"actual": 2, "limit": 1},
    }
    assert not list(limited.audit_log_dir.glob("*.jsonl"))


def test_item_limit_releases_idempotency_claim_and_skips_acl_llm(settings) -> None:
    limited = _mock_settings(settings, max_evaluation_items=3)
    value = _payload("network-plan-item-limit")
    value["sources"].append({"address": "16.201.1.20", "description": "应用 2"})
    value["destinations"].append(
        {"address": "16.220.16.30", "description": "数据库 2"}
    )
    with TestClient(create_app(limited)) as client:
        first = client.post("/v1/evaluations", json=value)
        second = client.post("/v1/evaluations", json=value)
        runtime = client.app.state.runtime
        assert runtime.evaluator.acl_client.__class__.__name__ == "MockAclClient"
    assert first.status_code == second.status_code == 422
    assert first.json()["error"]["code"] == "EVALUATION_ITEM_LIMIT_EXCEEDED"
    assert second.json()["error"]["code"] != "evaluation_in_progress"


def test_acl_dependency_is_advisory_or_required_by_configuration(settings) -> None:
    base = _mock_settings(
        settings,
        acl_client_mode="http",
        acl_api_url="http://acl.invalid",
    )
    with TestClient(create_app(base)) as client:
        advisory = client.post(
            "/v1/evaluations", json=_payload("network-plan-acl-advisory")
        ).json()
    with TestClient(create_app(replace(base, acl_decision_mode="required"))) as client:
        required = client.post(
            "/v1/evaluations", json=_payload("network-plan-acl-required")
        ).json()
    assert advisory["items"][0]["acl_verification_status"] == "unverified"
    assert advisory["items"][0]["decision"] == "合规"
    assert required["items"][0]["decision"] == "待定"
    assert required["items"][0]["reason_code"] == "ACL_DEPENDENCY_FAILURE"


def test_http_mode_rejects_missing_query_parameter(settings, tmp_path: Path) -> None:
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    shutil.copy(settings.policy_dir / "manifest.yaml", policy_dir / "manifest.yaml")
    shutil.copy(
        settings.policy_dir / "compliance_rules.yaml",
        policy_dir / "compliance_rules.yaml",
    )
    http_settings = replace(
        settings,
        policy_dir=policy_dir,
        network_plan_client_mode="http",
        network_plan_api_url="http://network-plan.invalid",
        network_plan_http_query_parameter=None,
        acl_decision_mode="advisory",
    )
    with pytest.raises(ValueError, match="NETWORK_PLAN_HTTP_QUERY_PARAMETER"):
        with TestClient(create_app(http_settings)):
            pass
