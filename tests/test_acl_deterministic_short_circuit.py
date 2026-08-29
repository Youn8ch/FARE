from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import payload


def _telnet_payload(request_id: str) -> dict:
    return payload(
        request_id=request_id,
        ports=[{"start": 23, "end": 23}],
        request_description="Telnet 管理访问",
    )


def test_skip_mode_avoids_acl_for_deterministic_pending_and_records_reason(
    settings,
) -> None:
    configured = replace(settings, acl_deterministic_pending_mode="skip")
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/v1/evaluations",
            json=_telnet_payload("acl-short-circuit-skip"),
        )
        calls = list(client.app.state.runtime.evaluator.acl_client.calls)

    assert response.status_code == 200
    body = response.json()
    item = body["items"][0]
    assert calls == []
    assert item["decision"] == "待定"
    assert item["reason_code"] == "PORT-001"
    assert item["acl_verification_status"] == "skipped"
    assert body["semantic_analysis"]["analyzed_item_ids"] == [item["item_id"]]
    assert item["decision_trace"]["deterministic_decision"] == "待定"
    assert item["decision_trace"]["final_decision"] == "待定"

    audit_file = next(configured.audit_log_dir.glob("*.jsonl"))
    audit = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["acl_raw"] == [
        {
            "item_id": item["item_id"],
            "skipped": True,
            "skip_reason": "deterministic_pending_rule",
            "matched_rule_ids": ["PORT-001"],
        }
    ]


def test_skip_mode_still_analyzes_acl_for_compliant_item(settings) -> None:
    configured = replace(settings, acl_deterministic_pending_mode="skip")
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/v1/evaluations",
            json=payload(request_id="acl-short-circuit-compliant"),
        )
        calls = list(client.app.state.runtime.evaluator.acl_client.calls)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert len(calls) == 1
    assert item["decision"] == "合规"
    assert item["acl_verification_status"] == "verified"


def test_analyze_mode_keeps_acl_analysis_for_deterministic_pending(settings) -> None:
    configured = replace(settings, acl_deterministic_pending_mode="analyze")
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/v1/evaluations",
            json=_telnet_payload("acl-short-circuit-analyze"),
        )
        calls = list(client.app.state.runtime.evaluator.acl_client.calls)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert len(calls) == 1
    assert item["decision"] == "待定"
    assert item["reason_code"] == "PORT-001"
    assert item["acl_verification_status"] == "verified"
