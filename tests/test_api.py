from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import payload


def test_health_and_readiness(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_complete_facts_without_rejection_are_compliant(client: TestClient):
    response = client.post("/v2/evaluations", json=payload())
    assert response.status_code == 200
    body = response.json()
    assert body["config_id"] == "direct-settings"
    assert body["environment"] == "unspecified"
    assert body["config_fingerprint"] == "direct-settings"
    assert body["decision"] == "合规"
    assert body["items"][0]["reason_type"] is None
    assert "acl" not in json.dumps(body).lower().replace("dataclass", "")


def test_object_rule_is_deterministic_without_unapproved_zone_rule(settings):
    # OBJECT-001 已从默认规则包禁用（AC-03 7.4）；对象关系能力由本 fixture 包
    # （显式 object_type 事实来源）端到端保留。
    fixture_policy = (
        Path(__file__).resolve().parent
        / "fixtures/policies/network_plan_object_relation"
    )
    value = payload(
        request_id="fare-test-zone",
        sources=[{"address": "20.1.10.10", "description": "办公终端"}],
        destinations=[{"address": "16.1.20.20", "description": "生产数据库"}],
    )
    with TestClient(
        create_app(
            replace(
                settings,
                policy_dir=fixture_policy,
                # OBJECT-001 的显式 object_type 事实只来自 offline 兼容目录
                network_plan_client_mode="offline_catalog",
            )
        )
    ) as test_client:
        body = test_client.post("/v2/evaluations", json=value).json()
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
    body = client.post("/v2/evaluations", json=value).json()
    item_ids = {item["item_id"] for item in body["items"]}
    semantic = body["semantic_analysis"]
    assert set(semantic["analyzed_item_ids"]) == item_ids
    assert {claim["scope"] for claim in semantic["claims"]} == item_ids
    assert all(claim["status"] == "candidate" for claim in semantic["claims"])
    assert all(item["explanation_source"] == "llm" for item in body["items"])


def test_two_by_two_cartesian_split_and_summary(client: TestClient):
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
        ports=[{"start": 443, "end": 443}],
    )
    body = client.post("/v2/evaluations", json=value).json()
    assert len(body["items"]) == 4
    assert body["decision"] == "合规"


def test_unknown_catalog_address_is_fact_incomplete(client: TestClient):
    value = payload(
        request_id="fare-test-unknown",
        destinations=[{"address": "192.0.2.10", "description": "未知"}],
    )
    body = client.post("/v2/evaluations", json=value).json()
    assert body["items"][0]["reason_type"] == "fact_incomplete"
    # AC-02：offline 目录经统一 resolver 主链路，未规划网段与 provider 404 同码
    assert body["items"][0]["reason_code"] == "NETWORK_PLAN_NOT_FOUND"


def test_any_is_policy_violation_not_a_parser_error(client: TestClient):
    value = payload(
        request_id="fare-test-any",
        sources=[{"address": "any", "description": "全来源"}],
    )
    body = client.post("/v2/evaluations", json=value).json()
    assert body["items"][0]["reason_code"] == "LEAST-ANY-001"


def test_idempotent_replay_returns_original_audit_id(client: TestClient):
    value = payload(request_id="fare-test-replay")
    first = client.post("/v2/evaluations", json=value)
    second = client.post("/v2/evaluations", json=value)
    assert second.status_code == 200
    assert second.json() == first.json()


def test_idempotency_conflict(client: TestClient):
    value = payload(request_id="fare-test-conflict")
    assert client.post("/v2/evaluations", json=value).status_code == 200
    value["request_description"] = "不同输入"
    response = client.post("/v2/evaluations", json=value)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_schema_error_is_422_and_has_no_audit_record(client: TestClient, settings):
    value = payload(request_id="fare-test-schema", ports=[{"start": 100, "end": 1}])
    assert client.post("/v2/evaluations", json=value).status_code == 422
    audit_files = list(settings.audit_log_dir.glob("*.jsonl"))
    assert not audit_files or all(not path.read_text(encoding="utf-8") for path in audit_files)


def test_invalid_address_contract_is_422(client: TestClient):
    value = payload(
        request_id="fare-test-invalid-address",
        sources=[{"address": "not-an-address", "description": "非法地址"}],
    )
    assert client.post("/v2/evaluations", json=value).status_code == 422


def test_host_address_and_explicit_host_prefix_are_same_idempotent_input(client: TestClient):
    value = payload(request_id="fare-test-normalized-address")
    first = client.post("/v2/evaluations", json=value).json()
    value["sources"][0]["address"] = "16.1.30.10/32"
    second = client.post("/v2/evaluations", json=value)
    assert second.status_code == 200
    assert second.json() == first


def test_audit_is_written_before_response_and_contains_no_api_key(client, settings):
    response = client.post("/v2/evaluations", json=payload(request_id="fare-test-audit"))
    audit_file = next(settings.audit_log_dir.glob("*.jsonl"))
    record = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["audit_id"] == response.json()["audit_id"]
    assert record["config_id"] == "direct-settings"
    assert record["environment"] == "unspecified"
    assert record["config_fingerprint"] == "direct-settings"
    assert record["final_response"] == response.json()
    assert record["model_raw"]["semantic"]
    assert record["model_raw"]["explanation"]
    assert "api_key" not in audit_file.read_text(encoding="utf-8").lower()


def test_restart_rebuilds_idempotency_index(settings):
    value = payload(request_id="fare-test-restart")
    with TestClient(create_app(settings)) as first_client:
        first = first_client.post("/v2/evaluations", json=value).json()
    with TestClient(create_app(settings)) as second_client:
        second = second_client.post("/v2/evaluations", json=value).json()
    assert second == first


def test_configuration_fingerprint_creates_a_new_idempotency_scope(settings):
    value = payload(request_id="fare-test-profile-switch")
    external = replace(
        settings,
        config_id="fare-external-test",
        environment="external-test",
        config_fingerprint="external-fingerprint",
    )
    intranet = replace(
        settings,
        config_id="fare-intranet-uat",
        environment="intranet-uat",
        config_fingerprint="intranet-fingerprint",
    )

    with TestClient(create_app(external)) as external_client:
        first = external_client.post("/v2/evaluations", json=value).json()
    with TestClient(create_app(intranet)) as intranet_client:
        second = intranet_client.post("/v2/evaluations", json=value).json()

    assert first["audit_id"] != second["audit_id"]
    assert first["environment"] == "external-test"
    assert second["environment"] == "intranet-uat"
    audit_file = next(settings.audit_log_dir.glob("*.jsonl"))
    records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    assert [record["config_fingerprint"] for record in records] == [
        "external-fingerprint",
        "intranet-fingerprint",
    ]


def test_v1_evaluations_retired_without_reaching_runtime(settings):
    """0.3.x compatibility window: /v1 only returns HTTP 410 with the stable
    API_VERSION_RETIRED code and never invokes the evaluation runtime."""

    value = payload(request_id="fare-test-v1-retired")
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/evaluations", json=value)
        provider_calls = list(
            client.app.state.runtime.network_plan_resolver.provider.transport.calls
        )
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "API_VERSION_RETIRED"
        assert provider_calls == []
        assert not list(settings.audit_log_dir.glob("*.jsonl"))
        # /v1 must not claim the request id: the same payload succeeds on /v2
        v2_response = client.post("/v2/evaluations", json=value)
        assert v2_response.status_code == 200
        assert v2_response.json()["request_id"] == "fare-test-v1-retired"


def test_openapi_declares_v2_entry_and_retired_v1(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {
        "/healthz",
        "/readyz",
        "/v1/evaluations",
        "/v2/evaluations",
    }
    assert spec["paths"]["/v2/evaluations"]["post"]["responses"]["200"] is not None
    assert (
        spec["paths"]["/v1/evaluations"]["post"]["responses"]["410"] is not None
    )
    assert spec["info"]["version"] == "0.3.0"


_RESPONSE_PROPERTIES = {
    "request_id",
    "config_id",
    "environment",
    "config_fingerprint",
    "decision",
    "policy_version",
    "model",
    "semantic_analysis",
    "items",
    "audit_id",
    "network_analysis",
    "request_findings",
}
_RESPONSE_REQUIRED = {
    "request_id",
    "decision",
    "policy_version",
    "model",
    "semantic_analysis",
    "items",
    "audit_id",
}
_ITEM_PROPERTIES = {
    "item_id",
    "access",
    "decision",
    "reason_type",
    "reason_code",
    "matched_rules",
    "decision_findings",
    "evidence",
    "reason",
    "recommendation",
    "explanation_source",
    "llm_explanation",
    "llm_recommendation",
    "source_network_fact_ids",
    "destination_network_fact_ids",
    "source_network_fact_status",
    "destination_network_fact_status",
    "decision_trace",
}
_ITEM_REQUIRED = {"item_id", "access", "decision", "reason", "recommendation"}
_ACL_COMPONENTS = {
    "AclAnalysis",
    "AclCandidateAnalysis",
    "AclCandidateComparison",
    "LlmAclCandidateFact",
    "LlmAclExtractionItem",
}
_ACL_FIELDS = {
    "acl_analysis",
    "acl_candidate_analysis",
    "acl_verification_status",
    "acl_no_path",
}


def test_openapi_v2_response_schemas_are_complete(client):
    """The hardening gate: /v2 response schemas are real, generator-ready
    contracts — exact target properties, correct required sets, and no ACL
    residue — not the degraded 0-property shells of the 0.2.0 baseline."""

    spec = client.get("/openapi.json").json()
    components = spec["components"]["schemas"]

    assert set(components["EvaluationResponse"]["properties"]) == (
        _RESPONSE_PROPERTIES
    )
    assert set(components["EvaluationResponse"]["required"]) == _RESPONSE_REQUIRED
    assert set(components["EvaluationItem"]["properties"]) == _ITEM_PROPERTIES
    assert set(components["EvaluationItem"]["required"]) == _ITEM_REQUIRED
    # The serialization schema FastAPI publishes for responses must be the
    # complete model, not the dict[str, Any] collapse of a wrap serializer.
    assert components["EvaluationResponse"]["properties"]
    assert components["EvaluationItem"]["properties"]

    success_ref = spec["paths"]["/v2/evaluations"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert success_ref == {
        "$ref": "#/components/schemas/EvaluationResponse"
    }

    v1_responses = spec["paths"]["/v1/evaluations"]["post"]["responses"]
    assert set(v1_responses) == {"410"}
    assert v1_responses["410"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

    assert not (_ACL_COMPONENTS & set(components))
    serialized_components = json.dumps(components)
    for banned in _ACL_FIELDS:
        assert banned not in serialized_components, banned


def test_openapi_current_artifact_matches_runtime_spec(client):
    artifact_path = (
        Path(__file__).resolve().parent.parent
        / "docs/implementation/openapi-current-v0.3.0.json"
    )
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == (
        client.get("/openapi.json").json()
    )


def test_v2_response_body_omits_disabled_optional_fields(client):
    """The JSON contract is unchanged: disabled optional fields stay out of
    the serialized body even though the OpenAPI schema now declares them."""

    body = client.post("/v2/evaluations", json=payload(request_id="openapi-omit")).json()
    assert "request_findings" not in body
    assert set(body) <= _RESPONSE_PROPERTIES
    item = body["items"][0]
    assert set(item) <= _ITEM_PROPERTIES
    if item["llm_explanation"] is None:
        assert "llm_explanation" not in item


def test_v1_error_body_omits_empty_error_details(client):
    response = client.post("/v1/evaluations", json=payload(request_id="err-detail"))
    assert response.status_code == 410
    assert "details" not in response.json()["error"]
