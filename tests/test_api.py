from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import payload


def test_health_and_readiness(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_complete_facts_without_rejection_are_compliant(client: TestClient):
    response = client.post("/v1/evaluations", json=payload())
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "合规"
    assert body["items"][0]["reason_type"] is None
    assert body["acl_analysis"]["classification"] == "候选路径与拟新增策略分析，非现网 ACL 状态"
    assert "MOCK-FW-01" in body["acl_analysis"]["extracted_facts"]["firewalls"]


def test_object_rule_is_deterministic_without_unapproved_zone_rule(client: TestClient):
    value = payload(
        request_id="fare-test-zone",
        sources=[{"address": "20.1.10.10", "description": "办公终端"}],
        destinations=[{"address": "16.1.20.20", "description": "生产数据库"}],
    )
    body = client.post("/v1/evaluations", json=value).json()
    assert body["decision"] == "待定"
    assert body["items"][0]["reason_type"] == "policy_violation"
    assert {rule["id"] for rule in body["items"][0]["matched_rules"]} == {"OBJECT-001"}
    assert "ZONE-001" not in body["semantic_analysis"]["candidate_rule_ids"]


def test_semantic_analysis_covers_all_items_and_explanation_is_guarded(client: TestClient):
    value = payload(
        request_id="fare-test-semantic-batch",
        sources=[
            {"address": "16.1.30.10", "description": "生产应用 A"},
            {"address": "16.1.30.11", "description": "生产应用 B"},
        ],
    )
    body = client.post("/v1/evaluations", json=value).json()
    item_ids = {item["item_id"] for item in body["items"]}
    semantic = body["semantic_analysis"]
    assert set(semantic["analyzed_item_ids"]) == item_ids
    assert {claim["scope"] for claim in semantic["claims"]} == item_ids
    assert all(claim["status"] == "candidate" for claim in semantic["claims"])
    assert all(item["explanation_source"] == "llm" for item in body["items"])


def test_multi_source_destination_port_cartesian_split_and_summary(client: TestClient):
    value = payload(
        request_id="fare-test-cartesian",
        sources=[
            {"address": "16.1.30.10", "description": "A"},
            {"address": "16.1.30.11", "description": "B"},
        ],
        destinations=[
            {"address": "16.1.30.20", "description": "C"},
            {"address": "16.1.30.21", "description": "D"},
        ],
        ports=[{"start": 443, "end": 443}, {"start": 23, "end": 23}],
    )
    body = client.post("/v1/evaluations", json=value).json()
    assert len(body["items"]) == 8
    assert body["decision"] == "待定"
    assert sum(item["reason_code"] == "PORT-001" for item in body["items"]) == 4


def test_unknown_catalog_address_is_fact_incomplete(client: TestClient):
    value = payload(
        request_id="fare-test-unknown",
        destinations=[{"address": "192.0.2.10", "description": "未知"}],
    )
    body = client.post("/v1/evaluations", json=value).json()
    assert body["items"][0]["reason_type"] == "fact_incomplete"
    assert body["items"][0]["reason_code"] == "ZONE_UNRESOLVED"


def test_any_is_policy_violation_not_a_parser_error(client: TestClient):
    value = payload(
        request_id="fare-test-any",
        sources=[{"address": "any", "description": "全来源"}],
    )
    body = client.post("/v1/evaluations", json=value).json()
    assert body["items"][0]["reason_code"] == "LEAST-ANY-001"


def test_idempotent_replay_returns_original_audit_id(client: TestClient):
    value = payload(request_id="fare-test-replay")
    first = client.post("/v1/evaluations", json=value)
    second = client.post("/v1/evaluations", json=value)
    assert second.status_code == 200
    assert second.json() == first.json()


def test_idempotency_conflict(client: TestClient):
    value = payload(request_id="fare-test-conflict")
    assert client.post("/v1/evaluations", json=value).status_code == 200
    value["request_description"] = "不同输入"
    response = client.post("/v1/evaluations", json=value)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_schema_error_is_422_and_has_no_audit_record(client: TestClient, settings):
    value = payload(request_id="fare-test-schema", ports=[{"start": 100, "end": 1}])
    assert client.post("/v1/evaluations", json=value).status_code == 422
    audit_files = list(settings.audit_log_dir.glob("*.jsonl"))
    assert not audit_files or all(not path.read_text(encoding="utf-8") for path in audit_files)


def test_invalid_address_contract_is_422(client: TestClient):
    value = payload(
        request_id="fare-test-invalid-address",
        sources=[{"address": "not-an-address", "description": "非法地址"}],
    )
    assert client.post("/v1/evaluations", json=value).status_code == 422


def test_host_address_and_explicit_host_prefix_are_same_idempotent_input(client: TestClient):
    value = payload(request_id="fare-test-normalized-address")
    first = client.post("/v1/evaluations", json=value).json()
    value["sources"][0]["address"] = "16.1.30.10/32"
    second = client.post("/v1/evaluations", json=value)
    assert second.status_code == 200
    assert second.json() == first


def test_http_acl_mode_fails_closed_as_business_pending(settings):
    http_settings = replace(settings, acl_client_mode="http", acl_api_url="http://acl.invalid")
    with TestClient(create_app(http_settings)) as client:
        response = client.post(
            "/v1/evaluations", json=payload(request_id="fare-test-http-adapter")
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["decision"] == "待定"
    assert item["reason_type"] == "dependency_failure"
    assert item["reason_code"] == "ACL_DEPENDENCY_FAILURE"


def test_explicit_acl_no_path_is_distinct_from_missing_firewall(settings, tmp_path):
    fixture = tmp_path / "acl.json"
    fixture.write_text(
        json.dumps(
            {
                "default": {
                    "analysis": "未找到该访问组合经过的防火墙。",
                    "config": "",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, acl_mock_file=fixture)
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post(
            "/v1/evaluations", json=payload(request_id="fare-test-no-path")
        ).json()
    item = body["items"][0]
    assert item["reason_type"] == "acl_no_path"
    assert item["reason_code"] == "ACL-PATH-001"


def test_acl_port_mismatch_is_fact_conflict(settings, tmp_path):
    fixture = tmp_path / "acl-port.json"
    fixture.write_text(
        json.dumps(
            {
                "default": {
                    "analysis": "候选路径经过防火墙 FW-01。",
                    "config": "access-list CANDIDATE port 8443",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, acl_mock_file=fixture)
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post(
            "/v1/evaluations", json=payload(request_id="fare-test-port-mismatch")
        ).json()
    assert body["items"][0]["reason_type"] == "fact_conflict"
    assert body["items"][0]["reason_code"] == "ACL_PORT_MISMATCH"


def test_audit_is_written_before_response_and_contains_no_api_key(client, settings):
    response = client.post("/v1/evaluations", json=payload(request_id="fare-test-audit"))
    audit_file = next(settings.audit_log_dir.glob("*.jsonl"))
    record = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["audit_id"] == response.json()["audit_id"]
    assert record["final_response"] == response.json()
    assert record["model_raw"]["semantic"]
    assert record["model_raw"]["explanation"]
    assert "api_key" not in audit_file.read_text(encoding="utf-8").lower()


def test_restart_rebuilds_idempotency_index(settings):
    value = payload(request_id="fare-test-restart")
    with TestClient(create_app(settings)) as first_client:
        first = first_client.post("/v1/evaluations", json=value).json()
    with TestClient(create_app(settings)) as second_client:
        second = second_client.post("/v1/evaluations", json=value).json()
    assert second == first


def test_semantic_guard_failure_preserves_deterministic_pending(settings, tmp_path):
    fixture = tmp_path / "llm-invalid-rule.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "test-invalid-rule",
                "responses": {
                    "fare-test-semantic-failure": {
                        "semantic": {
                            "analyzed_item_ids": [
                                "fare-test-semantic-failure-001",
                                "fare-test-semantic-failure-002",
                            ],
                            "claims": [],
                            "contradictions": [],
                            "candidate_rule_ids": ["FABRICATED-001"],
                            "policy_gaps": [],
                            "questions_for_requester": [],
                            "recommendations": [],
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, llm_mock_file=fixture)
    value = payload(
        request_id="fare-test-semantic-failure",
        sources=[{"address": "20.1.10.10", "description": "办公终端"}],
        destinations=[
            {"address": "16.1.20.20", "description": "生产数据库"},
            {"address": "16.1.30.20", "description": "生产应用"},
        ],
    )
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post("/v1/evaluations", json=value).json()
    assert body["items"][0]["reason_code"] == "OBJECT-001"
    assert body["items"][1]["reason_code"] == "LLM_SEMANTIC_ANALYSIS_FAILURE"
    assert all(item["explanation_source"] == "template" for item in body["items"])
    assert body["semantic_analysis"]["guard_results"][0]["status"] == "rejected"


def test_verified_policy_gap_becomes_risk_uncertain(settings, tmp_path):
    fixture = tmp_path / "llm-policy-gap.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "test-policy-gap",
                "default": {
                    "semantic": {
                        "analyzed_item_ids": ["fare-test-policy-gap-001"],
                        "claims": [],
                        "contradictions": [],
                        "candidate_rule_ids": [],
                        "policy_gaps": [
                            {
                                "gap_id": "gap-001",
                                "scope": "fare-test-policy-gap-001",
                                "description": "有证据的规则覆盖疑点",
                                "evidence": ["生产应用 HTTPS 访问"],
                            }
                        ],
                        "questions_for_requester": ["请补充审批依据。"],
                        "recommendations": ["提交人工复核。"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, llm_mock_file=fixture)
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post(
            "/v1/evaluations", json=payload(request_id="fare-test-policy-gap")
        ).json()
    assert body["items"][0]["reason_type"] == "risk_uncertain"
    assert body["items"][0]["reason_code"] == "SEMANTIC_POLICY_GAP"
    assert body["semantic_analysis"]["policy_gaps"][0]["status"] == "verified"


def test_semantic_claim_conflicting_with_catalog_becomes_fact_conflict(settings, tmp_path):
    fixture = tmp_path / "llm-authoritative-conflict.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "test-authoritative-conflict",
                "default": {
                    "semantic": {
                        "analyzed_item_ids": ["fare-test-authoritative-conflict-001"],
                        "claims": [
                            {
                                "claim_id": "claim-conflict-001",
                                "scope": "fare-test-authoritative-conflict-001",
                                "field": "destination_zone",
                                "value": "test",
                                "source": "destination_description",
                                "evidence": "生产应用 B",
                                "confidence": 0.8,
                            }
                        ],
                        "contradictions": [],
                        "candidate_rule_ids": [],
                        "policy_gaps": [],
                        "questions_for_requester": [],
                        "recommendations": [],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, llm_mock_file=fixture)
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post(
            "/v1/evaluations",
            json=payload(request_id="fare-test-authoritative-conflict"),
        ).json()
    assert body["items"][0]["reason_type"] == "fact_conflict"
    assert body["items"][0]["reason_code"] == "SEMANTIC_FACT_CONFLICT"
    assert body["semantic_analysis"]["claims"][0]["status"] == "conflict"


def test_explanation_failure_uses_template_without_changing_decision(settings, tmp_path):
    fixture = tmp_path / "llm-explanation-invalid.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "test-explanation-failure",
                "default": {
                    "semantic": {
                        "analyzed_item_ids": ["fare-test-explanation-failure-001"],
                        "claims": [],
                        "contradictions": [],
                        "candidate_rule_ids": [],
                        "policy_gaps": [],
                        "questions_for_requester": [],
                        "recommendations": [],
                    },
                    "explanation": {"items": []},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fixture_settings = replace(settings, llm_mock_file=fixture)
    with TestClient(create_app(fixture_settings)) as fixture_client:
        body = fixture_client.post(
            "/v1/evaluations",
            json=payload(request_id="fare-test-explanation-failure"),
        ).json()
    assert body["decision"] == "合规"
    assert body["items"][0]["explanation_source"] == "template"
