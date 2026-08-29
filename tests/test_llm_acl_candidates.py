from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import build_runtime, create_app
from app.schemas import (
    EvaluationRequest,
    ExtractedFacts,
    LlmAclCandidateFact,
    LlmAclExtractionItem,
    LlmExplanationResponse,
    LlmSemanticResponse,
)
from app.services.acl_candidate_merge import merge_acl_candidate
from app.services.llm_client import (
    LlmAclCandidateClientProtocol,
    LlmClient,
    LlmClientProtocol,
    LlmDependencyError,
)
from tests.conftest import payload
from tests.helpers.llm import RecordingLlmClient

pytestmark = [pytest.mark.llm_guard, pytest.mark.llm_pipeline]


class _CoreOnlyLlmClient:
    mode = "mock"

    @property
    def model_name(self) -> str:
        return "core-only"

    async def analyze(self, llm_payload: dict[str, Any]):
        response = LlmSemanticResponse(
            analyzed_item_ids=[item["item_id"] for item in llm_payload["items"]]
        )
        return response, response.model_dump(mode="json")

    async def explain(self, llm_payload: dict[str, Any]):
        response = LlmExplanationResponse.model_validate(
            {
                "items": [
                    {
                        "item_id": item["item_id"],
                        "explanation": item["reason"],
                        "recommendation": item["recommendation"],
                    }
                    for item in llm_payload["items"]
                ]
            }
        )
        return response, response.model_dump(mode="json")


def _evaluate(settings, recorder: RecordingLlmClient, request_payload: dict[str, Any]):
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = recorder
    request = EvaluationRequest.model_validate(request_payload)
    return asyncio.run(runtime.evaluator.evaluate(request))


def _protected_snapshot(response) -> dict[str, Any]:
    return {
        "decision": response.decision,
        "items": [
            {
                "decision": item.decision,
                "reason_type": item.reason_type,
                "reason_code": item.reason_code,
                "matched_rules": [rule.id for rule in item.matched_rules],
            }
            for item in response.items
        ],
        "deterministic_facts": response.acl_analysis.extracted_facts.model_dump(
            mode="json"
        ),
    }


def _default_candidate(
    item_id: str,
    *,
    firewall: str = "MOCK-FW-01",
    firewall_evidence: str | None = None,
):
    return LlmAclExtractionItem(
        item_id=item_id,
        firewalls=[firewall],
        candidate_acls=["FARE-CANDIDATE"],
        address_objects=["SRC", "DST"],
        observed_ports=[443],
        evidence=[
            firewall_evidence or f"候选路径经过防火墙 {firewall}。",
            "access-list FARE-CANDIDATE object-group SRC object-group DST port 443",
        ],
    )


def _mock_client(tmp_path: Path, response: Any) -> LlmClient:
    fixture = tmp_path / "acl-candidates.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "acl-candidate-test",
                "default": {"acl_candidates": response},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return LlmClient(
        mode="mock",
        base_url=None,
        model=None,
        api_key=None,
        mock_file=fixture,
        semantic_timeout=1,
        explanation_timeout=1,
        max_correction_retries=1,
    )


def _acl_inputs() -> list[dict[str, str]]:
    return [
        {
            "item_id": "candidate-001",
            "analysis": "候选路径经过防火墙 FW-01。",
            "config": "access-list ACL-01 port 443",
        },
        {
            "item_id": "candidate-002",
            "analysis": "候选路径经过防火墙 FW-02。",
            "config": "access-list ACL-02 port 8443",
        },
    ]


def test_acl_candidate_mode_defaults_off_and_is_strict(settings) -> None:
    assert settings.llm_acl_candidate_mode == "off"
    with pytest.raises(ValueError, match="LLM_ACL_CANDIDATE_MODE"):
        replace(settings, llm_acl_candidate_mode="guarded").validate()


def test_mode_off_does_not_call_or_serialize_candidates(settings) -> None:
    recorder = RecordingLlmClient()
    result = _evaluate(
        settings,
        recorder,
        payload(request_id="acl-candidate-mode-off"),
    )

    assert recorder.acl_candidate_calls == 0
    assert result.response.acl_candidate_analysis is None
    assert "acl_candidate_analysis" not in result.response.model_dump(mode="json")
    assert not any(key.startswith("acl_candidates") for key in result.model_raw)


def test_mode_off_http_response_keeps_existing_none_fields(settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/evaluations",
            json=payload(request_id="acl-candidate-mode-off-http"),
        )

    body = response.json()
    assert "acl_candidate_analysis" not in body
    assert "reason_type" in body["items"][0]
    assert body["items"][0]["reason_type"] is None


def test_mode_off_accepts_core_protocol_without_candidate_capability(settings) -> None:
    core_client = _CoreOnlyLlmClient()
    assert isinstance(core_client, LlmClientProtocol)
    assert not isinstance(core_client, LlmAclCandidateClientProtocol)
    runtime = build_runtime(settings)
    runtime.evaluator.llm_client = core_client

    result = asyncio.run(
        runtime.evaluator.evaluate(
            EvaluationRequest.model_validate(
                payload(request_id="acl-candidate-core-protocol-off")
            )
        )
    )

    assert result.response.decision == "合规"
    assert result.response.acl_candidate_analysis is None


def test_shadow_rejects_only_candidate_stage_when_capability_is_missing(settings) -> None:
    runtime = build_runtime(replace(settings, llm_acl_candidate_mode="shadow"))
    runtime.evaluator.llm_client = _CoreOnlyLlmClient()

    result = asyncio.run(
        runtime.evaluator.evaluate(
            EvaluationRequest.model_validate(
                payload(request_id="acl-candidate-core-protocol-shadow")
            )
        )
    )

    assert result.response.decision == "合规"
    assert result.response.acl_candidate_analysis.items[0].status == "rejected"
    assert result.response.items[0].explanation_source == "llm"


def test_shadow_agree_preserves_both_layers_and_decision(settings) -> None:
    item_id = "acl-candidate-agree-001"
    recorder = RecordingLlmClient(
        acl_candidates={item_id: _default_candidate(item_id)}
    )
    shadow_settings = replace(settings, llm_acl_candidate_mode="shadow")
    result = _evaluate(
        shadow_settings,
        recorder,
        payload(request_id="acl-candidate-agree"),
    )

    comparison = result.response.acl_candidate_analysis.items[0]
    assert comparison.status == "agree"
    assert comparison.deterministic.firewalls == ["MOCK-FW-01"]
    assert comparison.llm_candidate.firewalls == ["MOCK-FW-01"]
    assert result.response.decision == "合规"
    assert recorder.acl_candidate_calls == 1


def test_shadow_deterministic_only_and_conflict_do_not_change_decision(
    settings, tmp_path: Path
) -> None:
    request_payload = payload(request_id="acl-candidate-fixed-conflict")
    acl_fixture = tmp_path / "conflicting-candidate-source.json"
    acl_fixture.write_text(
        json.dumps(
            {
                "default": {
                    "analysis": (
                        "候选路径经过防火墙 MOCK-FW-01。备选设备 OTHER-FW。"
                    ),
                    "config": (
                        "access-list FARE-CANDIDATE object-group SRC "
                        "object-group DST port 443"
                    ),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shadow_settings = replace(
        settings,
        acl_mock_file=acl_fixture,
        llm_acl_candidate_mode="shadow",
        acl_decision_mode="required",  # 未确认防火墙仅在 required 模式降为待定
    )
    fixed_only = _evaluate(shadow_settings, RecordingLlmClient(), request_payload)
    conflict_id = "acl-candidate-fixed-conflict-001"
    conflict = _evaluate(
        shadow_settings,
        RecordingLlmClient(
            acl_candidates={
                conflict_id: _default_candidate(
                    conflict_id,
                    firewall="OTHER-FW",
                    firewall_evidence="备选设备 OTHER-FW。",
                )
            }
        ),
        request_payload,
    )

    assert fixed_only.response.acl_candidate_analysis.items[0].status == (
        "deterministic_only"
    )
    assert conflict.response.acl_candidate_analysis.items[0].status == "conflict"
    assert _protected_snapshot(fixed_only.response) == _protected_snapshot(
        conflict.response
    )


def test_llm_only_firewall_cannot_resolve_deterministic_missing_fact(
    settings, tmp_path: Path
) -> None:
    acl_fixture = tmp_path / "unstructured-acl.json"
    acl_fixture.write_text(
        json.dumps(
            {
                "default": {
                    "analysis": "路径设备名称为 EDGE-FW-9。",
                    "config": "",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    request_id = "acl-candidate-llm-only"
    item_id = f"{request_id}-001"
    shadow_settings = replace(
        settings,
        acl_mock_file=acl_fixture,
        llm_acl_candidate_mode="shadow",
        acl_decision_mode="required",  # 未确认防火墙仅在 required 模式降为待定
    )
    result = _evaluate(
        shadow_settings,
        RecordingLlmClient(
            acl_candidates={
                item_id: LlmAclExtractionItem(
                    item_id=item_id,
                    firewalls=["EDGE-FW-9"],
                    evidence=["路径设备名称为 EDGE-FW-9。"],
                )
            }
        ),
        payload(request_id=request_id),
    )

    comparison = result.response.acl_candidate_analysis.items[0]
    assert comparison.status == "llm_only"
    assert comparison.deterministic.firewalls == []
    assert comparison.llm_candidate.firewalls == ["EDGE-FW-9"]
    assert result.response.items[0].reason_code == "ACL_FIREWALL_UNRESOLVED"
    assert result.response.acl_analysis.extracted_facts.firewalls == []


def test_shadow_batches_four_items_once(settings) -> None:
    recorder = RecordingLlmClient()
    shadow_settings = replace(settings, llm_acl_candidate_mode="shadow")
    result = _evaluate(
        shadow_settings,
        recorder,
        payload(
            request_id="acl-candidate-four-items",
            sources=[
                {"address": "16.1.30.10", "description": "A"},
                {"address": "16.1.30.11", "description": "B"},
            ],
            destinations=[
                {"address": "16.1.30.20", "description": "C"},
                {"address": "16.1.30.21", "description": "D"},
            ],
        ),
    )

    assert recorder.acl_candidate_calls == 1
    assert recorder.acl_candidate_item_ids == [
        [f"acl-candidate-four-items-{index:03d}" for index in range(1, 5)]
    ]
    assert len(result.response.acl_candidate_analysis.items) == 4


def test_acl_dependency_item_is_still_in_shadow_batch(settings) -> None:
    recorder = RecordingLlmClient()
    shadow_settings = replace(
        settings,
        acl_client_mode="http",
        acl_api_url="https://acl.invalid",
        llm_acl_candidate_mode="shadow",
        acl_decision_mode="required",  # 依赖失败仅在 required 模式降为待定
    )
    result = _evaluate(
        shadow_settings,
        recorder,
        payload(request_id="acl-candidate-empty-source"),
    )

    assert recorder.acl_candidate_item_ids == [["acl-candidate-empty-source-001"]]
    assert result.response.acl_candidate_analysis.items[0].status == "empty"
    assert result.response.items[0].reason_code == "ACL_DEPENDENCY_FAILURE"


def test_candidate_dependency_failure_is_rejected_without_decision_change(settings) -> None:
    request_payload = payload(request_id="acl-candidate-dependency-failure")
    baseline = _evaluate(settings, RecordingLlmClient(), request_payload)
    recorder = RecordingLlmClient(fail_stage="acl_candidates")
    failed = _evaluate(
        replace(settings, llm_acl_candidate_mode="shadow"),
        recorder,
        request_payload,
    )

    assert _protected_snapshot(failed.response) == _protected_snapshot(baseline.response)
    assert failed.response.acl_candidate_analysis.items[0].status == "rejected"
    assert recorder.failed_stage == "acl_candidates"
    assert recorder.semantic_calls == 1
    assert recorder.explanation_calls == 1


@pytest.mark.parametrize(
    "invalid_kind",
    ["flat-no-evidence", "wrong-internal-item-id", "structured-source-mismatch"],
)
def test_evaluator_revalidates_all_protocol_candidate_outputs(
    settings, invalid_kind: str
) -> None:
    request_id = f"acl-candidate-reguard-{invalid_kind}"
    item_id = f"{request_id}-001"
    if invalid_kind == "flat-no-evidence":
        candidate = LlmAclExtractionItem(
            item_id=item_id,
            firewalls=["MOCK-FW-01"],
        )
    elif invalid_kind == "wrong-internal-item-id":
        candidate = LlmAclExtractionItem(item_id="wrong-item-id")
    else:
        candidate = LlmAclExtractionItem(
            item_id=item_id,
            facts=[
                LlmAclCandidateFact(
                    type="firewall",
                    value="MOCK-FW-01",
                    source="acl_config",
                    evidence="候选路径经过防火墙 MOCK-FW-01。",
                    confidence=0.9,
                )
            ],
        )
    request_payload = payload(request_id=request_id)
    baseline = _evaluate(settings, RecordingLlmClient(), request_payload)
    result = _evaluate(
        replace(settings, llm_acl_candidate_mode="shadow"),
        RecordingLlmClient(acl_candidates={item_id: candidate}),
        request_payload,
    )

    comparison = result.response.acl_candidate_analysis.items[0]
    assert comparison.status == "rejected"
    assert comparison.rejection_reason == "ACL candidate shadow output was rejected"
    assert _protected_snapshot(result.response) == _protected_snapshot(baseline.response)
    assert result.model_raw["acl_candidates"]["error"] != comparison.rejection_reason


def test_shadow_response_and_audit_are_observable(settings) -> None:
    shadow_settings = replace(settings, llm_acl_candidate_mode="shadow")
    with TestClient(create_app(shadow_settings)) as client:
        response = client.post(
            "/v1/evaluations",
            json=payload(request_id="acl-candidate-audit"),
        )

    body = response.json()
    assert body["acl_candidate_analysis"]["mode"] == "shadow"
    audit_file = next(shadow_settings.audit_log_dir.glob("*.jsonl"))
    audit = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["model_raw"]["acl_candidates_input"]
    assert audit["model_raw"]["acl_candidates"]["items"]


def test_structured_candidate_fact_is_bound_and_normalized(tmp_path: Path) -> None:
    response = {
        "items": [
            {
                "item_id": "candidate-001",
                "facts": [
                    {
                        "type": "firewall",
                        "value": "FW-01",
                        "source": "acl_analysis",
                        "evidence": "候选路径经过防火墙 FW-01。",
                        "confidence": 0.9,
                    }
                ],
            },
            {"item_id": "candidate-002"},
        ]
    }
    client = _mock_client(tmp_path, response)
    result = asyncio.run(client.extract_acl_facts(_acl_inputs()))

    assert result["candidate-001"].firewalls == ["FW-01"]
    assert result["candidate-001"].facts == [
        LlmAclCandidateFact(
            type="firewall",
            value="FW-01",
            source="acl_analysis",
            evidence="候选路径经过防火墙 FW-01。",
            confidence=0.9,
        )
    ]


def test_merge_counts_structured_facts_without_flat_compatibility_fields() -> None:
    candidate = LlmAclExtractionItem(
        item_id="structured-merge-001",
        facts=[
            LlmAclCandidateFact(
                type="firewall",
                value="FW-01",
                source="acl_analysis",
                evidence="防火墙 FW-01",
                confidence=0.9,
            )
        ],
    )

    merged = merge_acl_candidate(
        item_id="structured-merge-001",
        deterministic=ExtractedFacts(firewalls=["fw-01"]),
        llm_candidate=candidate,
    )

    assert merged.status == "agree"


def test_http_candidate_stage_uses_one_injected_batch_request() -> None:
    request_count = 0
    candidate_response = {
        "items": [
            {
                "item_id": "candidate-001",
                "facts": [
                    {
                        "type": "firewall",
                        "value": "FW-01",
                        "source": "acl_analysis",
                        "evidence": "候选路径经过防火墙 FW-01。",
                        "confidence": 0.9,
                    }
                ],
            },
            {"item_id": "candidate-002"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(candidate_response)}}
                ]
            },
        )

    client = LlmClient(
        mode="http",
        base_url="https://model.invalid/v1",
        model="test-model",
        api_key=None,
        mock_file=None,
        semantic_timeout=1,
        explanation_timeout=1,
        max_correction_retries=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.extract_acl_facts(_acl_inputs()))

    assert request_count == 1
    assert result["candidate-001"].firewalls == ["FW-01"]


def test_mock_candidate_stage_defaults_to_complete_empty_items(tmp_path: Path) -> None:
    fixture = tmp_path / "no-candidate-stage.json"
    fixture.write_text(json.dumps({"version": "no-candidate-stage"}), encoding="utf-8")
    client = LlmClient(
        mode="mock",
        base_url=None,
        model=None,
        api_key=None,
        mock_file=fixture,
        semantic_timeout=1,
        explanation_timeout=1,
        max_correction_retries=1,
    )

    result = asyncio.run(client.extract_acl_facts(_acl_inputs()))

    assert list(result) == ["candidate-001", "candidate-002"]
    assert all(not item.facts and not item.firewalls for item in result.values())


@pytest.mark.parametrize(
    "candidate_item",
    [
        {
            "item_id": "candidate-001",
            "firewalls": ["FW-01"],
            "evidence": [],
        },
        {
            "item_id": "candidate-001",
            "firewalls": ["FABRICATED"],
            "evidence": ["候选路径经过防火墙 FW-01。"],
        },
        {
            "item_id": "candidate-001",
            "firewalls": ["FW-02"],
            "evidence": ["候选路径经过防火墙 FW-02。"],
        },
    ],
    ids=["no-evidence", "fact-evidence-mismatch", "cross-item-evidence"],
)
def test_flat_candidate_requires_item_bound_fact_evidence(
    tmp_path: Path, candidate_item: dict[str, Any]
) -> None:
    response = {"items": [candidate_item, {"item_id": "candidate-002"}]}
    client = _mock_client(tmp_path, response)

    with pytest.raises(LlmDependencyError):
        asyncio.run(client.extract_acl_facts(_acl_inputs()))


@pytest.mark.parametrize("structured", [False, True], ids=["flat", "structured"])
def test_candidate_identifier_requires_token_boundary(
    tmp_path: Path, structured: bool
) -> None:
    candidate: dict[str, Any] = {
        "item_id": "candidate-001",
        "evidence": ["候选路径经过防火墙 FW-010。"],
    }
    if structured:
        candidate = {
            "item_id": "candidate-001",
            "facts": [
                {
                    "type": "firewall",
                    "value": "FW-01",
                    "source": "acl_analysis",
                    "evidence": "候选路径经过防火墙 FW-010。",
                    "confidence": 0.9,
                }
            ],
        }
    else:
        candidate["firewalls"] = ["FW-01"]
    inputs = _acl_inputs()
    inputs[0]["analysis"] = "候选路径经过防火墙 FW-010。"
    client = _mock_client(
        tmp_path,
        {"items": [candidate, {"item_id": "candidate-002"}]},
    )

    with pytest.raises(LlmDependencyError):
        asyncio.run(client.extract_acl_facts(inputs))


@pytest.mark.parametrize(
    "items",
    [
        [{"item_id": "candidate-001"}],
        [
            {"item_id": "candidate-001"},
            {"item_id": "candidate-001"},
            {"item_id": "candidate-002"},
        ],
        [
            {"item_id": "candidate-001"},
            {"item_id": "candidate-002"},
            {"item_id": "candidate-extra"},
        ],
    ],
    ids=["missing", "duplicate", "extra"],
)
def test_candidate_item_coverage_is_exact(
    tmp_path: Path, items: list[dict[str, Any]]
) -> None:
    client = _mock_client(tmp_path, {"items": items})

    with pytest.raises(LlmDependencyError, match="completely and uniquely"):
        asyncio.run(client.extract_acl_facts(_acl_inputs()))


def test_candidate_port_must_be_in_protocol_range(tmp_path: Path) -> None:
    client = _mock_client(
        tmp_path,
        {
            "items": [
                {"item_id": "candidate-001", "observed_ports": [65536]},
                {"item_id": "candidate-002"},
            ]
        },
    )

    with pytest.raises(LlmDependencyError, match="schema validation"):
        asyncio.run(client.extract_acl_facts(_acl_inputs()))


def test_flat_candidate_port_rejects_boolean(tmp_path: Path) -> None:
    client = _mock_client(
        tmp_path,
        {
            "items": [
                {"item_id": "candidate-001", "observed_ports": [True]},
                {"item_id": "candidate-002"},
            ]
        },
    )

    with pytest.raises(LlmDependencyError, match="schema validation"):
        asyncio.run(client.extract_acl_facts(_acl_inputs()))


def test_mock_candidate_schema_failure_is_shadow_only(settings, tmp_path: Path) -> None:
    fixture = tmp_path / "invalid-candidate-stage.json"
    fixture.write_text(
        json.dumps(
            {
                "version": "invalid-candidate-stage",
                "default": {"acl_candidates": {"items": "invalid"}},
            }
        ),
        encoding="utf-8",
    )
    request_payload = payload(request_id="acl-candidate-schema-failure")
    baseline = _evaluate(settings, RecordingLlmClient(), request_payload)
    runtime = build_runtime(
        replace(
            settings,
            llm_mock_file=fixture,
            llm_acl_candidate_mode="shadow",
        )
    )
    result = asyncio.run(
        runtime.evaluator.evaluate(EvaluationRequest.model_validate(request_payload))
    )

    assert _protected_snapshot(result.response) == _protected_snapshot(baseline.response)
    assert result.response.acl_candidate_analysis.items[0].status == "rejected"
