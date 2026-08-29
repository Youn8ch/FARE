"""AC-02 acceptance: one provider -> resolver -> canonical segment main path.

The offline catalog no longer bypasses the resolver: it acts as an explicit
compatibility provider, so mock/http/offline_catalog share the same query
limit, segmentation, and reason codes for equivalent facts.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import build_runtime, create_app
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Equivalent provider fact for the real policies catalog entry PROD-APP:
# OfflineCatalogNetworkPlanClient masquerades zone/environment/object_type/id
# as areaId/regionName/platformName/usageCode/description.
EQUIVALENT_FIXTURE = {
    "fixture_version": "2026.08.0",
    "purpose": "AC-02 offline/mock equivalence on the same catalog fact",
    "responses": {
        "16.1.30.0/24": {
            "http_status": 200,
            "body": {
                "code": 200,
                "msg": "success",
                "data": {
                    "area": "production",
                    "areaId": "production",
                    "regionName": "production",
                    "platformName": "production",
                    "network": "16.1.30.0/24",
                    "gateway": None,
                    "subnet": "16.1.30.0/24",
                    "vlanId": None,
                    "usageCode": "application",
                    "description": "PROD-APP",
                },
                "success": True,
            },
        }
    },
}


def _write_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "equivalent.v1.json"
    fixture.write_text(json.dumps(EQUIVALENT_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return fixture


def _same_request(request_id: str) -> dict:
    return _payload(
        request_id,
        sources=[{"address": "16.1.30.10", "description": "生产应用 A"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
        request_description="生产应用 HTTPS 访问",
    )


def _keys(item: dict) -> dict:
    return {
        "access": item["access"],
        "decision": item["decision"],
        "reason_type": item["reason_type"],
        "reason_code": item["reason_code"],
        "matched_rules": item["matched_rules"],
        "source_network_fact_status": item["source_network_fact_status"],
        "destination_network_fact_status": item["destination_network_fact_status"],
        "source_network_fact_ids": item["source_network_fact_ids"],
        "destination_network_fact_ids": item["destination_network_fact_ids"],
        "acl_verification_status": item["acl_verification_status"],
        "semantic_effect": item["decision_trace"]["semantic_effect"],
        "final_decision": item["decision_trace"]["final_decision"],
    }


def test_mock_and_offline_produce_identical_items_for_equivalent_facts(
    settings: Settings, tmp_path: Path
) -> None:
    fixture = _write_fixture(tmp_path)
    offline_settings = settings
    mock_settings = _mock_chain(settings, network_plan_mock_file=fixture)
    with TestClient(create_app(offline_settings)) as client:
        offline_status, offline_body = _run(client, _same_request("ac02-equiv-offline"))
        offline_runtime = client.app.state.runtime
        offline_calls = list(
            offline_runtime.network_plan_resolver.client.calls
        )
    with TestClient(create_app(mock_settings)) as client:
        mock_status, mock_body = _run(client, _same_request("ac02-equiv-mock"))
        mock_runtime = client.app.state.runtime
        mock_calls = list(mock_runtime.network_plan_resolver.client.calls)

    assert offline_status == mock_status == 200
    assert offline_body["decision"] == mock_body["decision"]
    assert _keys(offline_body["items"][0]) == _keys(mock_body["items"][0])
    assert len(offline_body["items"]) == len(mock_body["items"]) == 1
    assert offline_calls == mock_calls == ["16.1.30.0/24"]


def _run(client: TestClient, payload: dict):
    response = client.post("/v1/evaluations", json=payload)
    return response.status_code, response.json()


def test_query_limit_applies_to_offline_catalog_mode(settings: Settings) -> None:
    limited = replace(settings, network_plan_max_subnets_per_request=1)
    payload = _payload(
        "ac02-offline-query-limit",
        sources=[{"address": "16.1.20.10", "description": "生产数据库"}],
        destinations=[{"address": "16.1.30.20", "description": "生产应用 B"}],
    )
    with TestClient(create_app(limited)) as client:
        runtime = client.app.state.runtime
        status, body = _run(client, payload)
        repeat_status, repeat_body = _run(client, payload)
        calls = list(runtime.network_plan_resolver.client.calls)
    assert status == repeat_status == 422
    assert body["error"]["code"] == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"actual": 2, "limit": 1}
    assert repeat_body["error"]["code"] == "NETWORK_PLAN_QUERY_LIMIT_EXCEEDED"
    assert calls == []


def test_item_limit_applies_to_offline_catalog_mode(settings: Settings) -> None:
    limited = replace(settings, max_evaluation_items=3)
    payload = _payload(
        "ac02-offline-item-limit",
        sources=[
            {"address": "16.1.30.10", "description": "生产应用 A"},
            {"address": "16.1.30.11", "description": "生产应用 A2"},
        ],
        destinations=[
            {"address": "16.1.30.20", "description": "生产应用 B"},
            {"address": "16.1.30.21", "description": "生产应用 B2"},
        ],
    )
    with TestClient(create_app(limited)) as client:
        status, body = _run(client, payload)
        repeat_status, repeat_body = _run(client, payload)
    assert status == repeat_status == 422
    assert body["error"]["code"] == "EVALUATION_ITEM_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"actual": 4, "limit": 3}
    assert repeat_body["error"]["code"] != "evaluation_in_progress"


def test_all_provider_modes_build_a_runtime_resolver(settings: Settings) -> None:
    async def close(runtime) -> None:
        await runtime.aclose()

    import asyncio

    offline_runtime = build_runtime(settings)
    try:
        assert offline_runtime.network_plan_resolver is not None
        assert (
            offline_runtime.network_plan_resolver.client.__class__.__name__
            == "OfflineCatalogNetworkPlanClient"
        )
    finally:
        asyncio.run(close(offline_runtime))

    mock_runtime = build_runtime(_mock_chain(settings))
    try:
        assert mock_runtime.network_plan_resolver is not None
        assert (
            mock_runtime.network_plan_resolver.client.__class__.__name__
            == "MockNetworkPlanClient"
        )
    finally:
        asyncio.run(close(mock_runtime))

    http_settings = replace(
        _mock_chain(settings),
        network_plan_client_mode="http",
        network_plan_api_url="http://network-plan.invalid",
        network_plan_http_query_parameter="subnet",
    )
    http_runtime = build_runtime(http_settings)
    try:
        assert http_runtime.network_plan_resolver is not None
        assert (
            http_runtime.network_plan_resolver.client.__class__.__name__
            == "HttpNetworkPlanClient"
        )
    finally:
        asyncio.run(close(http_runtime))


def test_app_runtime_has_no_legacy_split_call_sites() -> None:
    """STATIC-01 (early): app/ must not call split_request; only the definition
    remains until AC-08 removes it."""

    offenders: list[str] = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "split_request(" in line and not line.lstrip().startswith("def "):
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == []
