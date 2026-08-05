from __future__ import annotations

import asyncio
from pathlib import Path

from app.schemas import EvaluationRequest
from app.services.network_plan_client import MockNetworkPlanClient
from app.services.network_plan_resolver import NetworkPlanResolver

FIXTURE = Path("tests/fixtures/network_plan/multi_region.v1.json")


def _request(source: str) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "request_id": "network-plan-resolver",
            "sources": [{"address": source}],
            "destinations": [{"address": "16.220.16.20"}],
            "protocol": "tcp",
            "ports": [{"start": 443, "end": 443}],
        }
    )


def _resolver(client: MockNetworkPlanClient) -> NetworkPlanResolver:
    return NetworkPlanResolver(
        client,
        max_subnets=64,
        max_concurrency=2,
        lookup_timeout=1,
        batch_timeout=3,
    )


def test_same_region_23_is_aggregated_without_expanding_access() -> None:
    client = MockNetworkPlanClient(FIXTURE)
    result = asyncio.run(_resolver(client).resolve(_request("16.201.0.0/23")))
    assert [str(segment.access_network) for segment in result.sources] == [
        "16.201.0.0/23"
    ]
    assert len(result.sources[0].network_fact_ids) == 2
    assert client.calls.count("16.201.0.0/24") == 1
    assert client.calls.count("16.201.1.0/24") == 1


def test_multi_region_and_404_are_separate_boundaries() -> None:
    client = MockNetworkPlanClient(FIXTURE)
    result = asyncio.run(_resolver(client).resolve(_request("16.201.0.0/22")))
    assert [str(segment.access_network) for segment in result.sources] == [
        "16.201.0.0/23",
        "16.201.2.0/24",
        "16.201.3.0/24",
    ]
    assert [segment.network_fact_status for segment in result.sources] == [
        "complete",
        "complete",
        "not_found",
    ]


def test_two_hosts_share_lookup_but_remain_distinct_access_ranges() -> None:
    request = _request("16.201.1.10")
    request = request.model_copy(
        update={
            "sources": [
                request.sources[0],
                request.sources[0].model_copy(update={"address": "16.201.1.20"}),
            ]
        }
    )
    client = MockNetworkPlanClient(FIXTURE)
    result = asyncio.run(_resolver(client).resolve(request))
    assert [str(segment.access_network) for segment in result.sources] == [
        "16.201.1.10/32",
        "16.201.1.20/32",
    ]
    assert client.calls.count("16.201.1.0/24") == 1
    assert result.sources[0].network_fact_ids == result.sources[1].network_fact_ids
