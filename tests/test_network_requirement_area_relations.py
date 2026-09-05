from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from ipaddress import ip_network
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import FareConfig
from app.main import create_app
from app.requirement_runner import fetch_requirements
from app.services.network_plan_client import MockNetworkPlanClient
from app.services.requirement_source import RequirementBatch
from app.services.rule_loader import PolicyBundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ROOT = PROJECT_ROOT / "tests/cases/network_requirements"
SUCCESS_BATCH = CASE_ROOT / "area_relation_success.batch.json"
NOT_FOUND_BATCH = CASE_ROOT / "area_relation_not_found.batch.json"
EXPECTED_FILE = CASE_ROOT / "area_relation_expected.json"
NETWORK_PLAN_FIXTURE = PROJECT_ROOT / "tests/fixtures/network_plan/multi_region.v1.json"
POLICY_DIR = (
    PROJECT_ROOT / "tests/fixtures/policies/network_plan_area_relation_matrix"
)
CONFIG_FILE = PROJECT_ROOT / "config/fare.test-area-relations.yaml"


def _load_batch(path: Path) -> RequirementBatch:
    return RequirementBatch.model_validate_json(path.read_text(encoding="utf-8"))


def _expected() -> dict[str, dict]:
    paths = [EXPECTED_FILE, *sorted(CASE_ROOT.glob("area_relation_*.expected.json"))]
    merged: dict[str, dict] = {}
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == "fare-area-relation-expected/v1"
        overlap = set(merged) & set(document["requests"])
        assert not overlap, f"duplicate expected request IDs: {sorted(overlap)}"
        merged.update(document["requests"])
    return merged


def _query_subnet(address: str) -> str:
    return str(ip_network(f"{address}/24", strict=False))


def test_area_relation_batches_are_valid_unique_and_contain_no_any() -> None:
    paths = sorted(CASE_ROOT.glob("area_relation_*.batch.json"))
    batches = [_load_batch(path) for path in paths]
    requests = [request for batch in batches for request in batch.requests]
    request_ids = [request.request_id for request in requests]
    expected_by_id = _expected()

    assert len(paths) == 30
    assert len(request_ids) == len(set(request_ids)) == 159
    assert set(request_ids) == set(expected_by_id)
    assert sum(value["item_count"] for value in expected_by_id.values()) == 261
    assert len(_load_batch(SUCCESS_BATCH).requests) == 30
    assert len(_load_batch(NOT_FOUND_BATCH).requests) == 1
    assert all(
        len(_load_batch(path).requests) == 8
        for path in paths
        if "mixed" in path.name
    )
    assert all(
        len(_load_batch(path).requests) == 4
        for path in paths
        if "generated" in path.name
    )
    for path in paths:
        if "mixed" not in path.name and "generated" not in path.name:
            continue
        mixed_requests = _load_batch(path).requests
        assert any(
            len(request.sources) > 1 and len(request.destinations) > 1
            for request in mixed_requests
        )
        assert {
            expected_by_id[request.request_id]["decision"]
            for request in mixed_requests
        } == {"合规", "待定"}
    for request in requests:
        assert request.protocol == "tcp"
        assert request.ports[0].start == request.ports[0].end == 443
        assert len(request.ports) == 1
        for address in (*request.sources, *request.destinations):
            assert address.address.lower() != "any"


def test_network_plan_fixture_has_one_fixed_not_found_and_success_elsewhere() -> None:
    fixture = json.loads(NETWORK_PLAN_FIXTURE.read_text(encoding="utf-8"))
    responses = fixture["responses"]
    failed = {
        subnet: response
        for subnet, response in responses.items()
        if response["body"].get("code") != 200
    }

    assert failed == {
        "16.201.3.0/24": {
            "http_status": 200,
            "body": {
                "code": 404,
                "msg": "网段规划不存在",
                "data": None,
                "success": False,
            },
        }
    }
    assert all(
        response["http_status"] == 200
        and response["body"]["code"] == 200
        and response["body"]["success"] is True
        and response["body"]["data"] is not None
        for subnet, response in responses.items()
        if subnet != "16.201.3.0/24"
    )

    success_requests = [
        request
        for path in sorted(CASE_ROOT.glob("area_relation_*.batch.json"))
        if path != NOT_FOUND_BATCH
        for request in _load_batch(path).requests
    ]
    success_subnets = {
        _query_subnet(address.address)
        for request in success_requests
        for address in (*request.sources, *request.destinations)
    }
    assert success_subnets <= set(responses) - {"16.201.3.0/24"}


def test_area_relation_policy_is_isolated_ordered_and_pending_only() -> None:
    bundle = PolicyBundle.load(POLICY_DIR)
    ids = [rule.id for rule in bundle.rules]

    assert ids[:2] == ["ZONE-OFFICE-CORE-DB-001", "ZONE-OFFICE-CORE-001"]
    assert all(rule.decision == "待定" for rule in bundle.rules)
    assert not (POLICY_DIR / "network_catalog.yaml").exists()


def test_requirement_source_runs_area_matrix_with_mock_dependencies(
    tmp_path: Path,
) -> None:
    base = FareConfig.load(CONFIG_FILE)
    configured = replace(
        base,
        requirement_source=replace(
            base.requirement_source,
            output_file=tmp_path / "area-relation-results.json",
        ),
        settings=replace(base.settings, audit_log_dir=tmp_path / "audit"),
    )
    requests = asyncio.run(fetch_requirements(configured))
    expected_by_id = _expected()

    assert len(requests) == 159
    assert {request.request_id for request in requests} == set(expected_by_id)

    with TestClient(create_app(configured.settings)) as client:
        runtime = client.app.state.runtime
        resolver = runtime.network_plan_resolver
        assert resolver is not None
        assert isinstance(resolver.provider.transport, MockNetworkPlanClient)

        for request in requests:
            network_before = len(resolver.provider.transport.calls)
            response = client.post(
                "/v2/evaluations", json=request.model_dump(mode="json")
            )
            expected = expected_by_id[request.request_id]

            assert response.status_code == 200
            body = response.json()
            assert body["decision"] == expected["decision"]
            assert len(body["items"]) == expected["item_count"]
            assert (
                len(resolver.provider.transport.calls) - network_before
                == expected["lookup_count"]
            )
            assert len(body["network_analysis"]["lookups"]) == expected["lookup_count"]

            for item, expected_item in zip(
                body["items"], expected["items"], strict=True
            ):
                assert item["access"]["source"] == expected_item["source"]
                assert item["access"]["destination"] == expected_item["destination"]
                assert item["decision"] == expected_item["decision"]
                assert item["reason_code"] == expected_item["reason_code"]
                assert [rule["id"] for rule in item["matched_rules"]] == (
                    expected_item["matched_rule_ids"]
                )
                assert item["source_network_fact_status"] == (
                    expected_item["source_status"]
                )
                assert item["destination_network_fact_status"] == (
                    expected_item["destination_status"]
                )


def test_not_found_batch_uses_only_the_planned_missing_destination() -> None:
    requests = _load_batch(NOT_FOUND_BATCH).requests

    assert len(requests) == 1
    request = requests[0]
    assert [_query_subnet(value.address) for value in request.sources] == [
        "16.201.2.0/24"
    ]
    assert [_query_subnet(value.address) for value in request.destinations] == [
        "16.201.3.0/24"
    ]
