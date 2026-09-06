from __future__ import annotations

import asyncio
from ipaddress import IPv4Network
from pathlib import Path

import httpx
import pytest

from app.services.network_fact_provider import validate_network_plan_response
from app.services.network_plan_client import (
    HttpNetworkPlanClient,
    MockNetworkPlanClient,
    TtlNetworkPlanClient,
)

FIXTURE = Path("tests/fixtures/network_plan/multi_region.v1.json")


def test_mock_client_rejects_non_24_and_records_canonical_calls() -> None:
    client = MockNetworkPlanClient(FIXTURE)
    with pytest.raises(ValueError, match="IPv4 /24"):
        asyncio.run(client.lookup(IPv4Network("16.201.0.0/23")))
    response = asyncio.run(client.lookup(IPv4Network("16.201.1.0/24")))
    assert response.http_status == 200
    assert client.calls == ["16.201.1.0/24"]


def test_positive_ttl_cache_does_not_cache_not_found() -> None:
    inner = MockNetworkPlanClient(FIXTURE)
    client = TtlNetworkPlanClient(inner, ttl_seconds=30, max_entries=2)
    asyncio.run(client.lookup(IPv4Network("16.201.1.0/24")))
    asyncio.run(client.lookup(IPv4Network("16.201.1.0/24")))
    asyncio.run(client.lookup(IPv4Network("16.201.3.0/24")))
    asyncio.run(client.lookup(IPv4Network("16.201.3.0/24")))
    assert inner.calls.count("16.201.1.0/24") == 1
    assert inner.calls.count("16.201.3.0/24") == 2


def test_ttl_cache_coalesces_concurrent_misses_for_same_subnet() -> None:
    inner = MockNetworkPlanClient(FIXTURE)
    inner.responses["16.201.1.0/24"]["delay_ms"] = 25
    client = TtlNetworkPlanClient(inner, ttl_seconds=30, max_entries=2)

    async def lookup_concurrently():
        subnet = IPv4Network("16.201.1.0/24")
        responses = await asyncio.gather(*(client.lookup(subnet) for _ in range(10)))
        await client.aclose()
        return responses

    responses = asyncio.run(lookup_concurrently())

    assert all(response.http_status == 200 for response in responses)
    assert inner.calls.count("16.201.1.0/24") == 1


def test_ttl_cache_closes_inner_client() -> None:
    class CloseTrackingClient(MockNetworkPlanClient):
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    inner = CloseTrackingClient(FIXTURE)
    client = TtlNetworkPlanClient(inner, ttl_seconds=30, max_entries=2)

    asyncio.run(client.aclose())

    assert inner.closed is True


def test_http_network_plan_client_uses_yaml_bearer_token() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"code": 404, "success": False, "data": None})

    client = HttpNetworkPlanClient(
        "https://network-plan.test/query",
        1,
        transport=httpx.MockTransport(handler),
        query_parameter="subnet",
        token="test-network-plan-token",
    )

    response = asyncio.run(client.lookup(IPv4Network("192.0.2.0/24")))

    assert response.http_status == 200
    assert captured == {
        "url": "https://network-plan.test/query?subnet=192.0.2.0%2F24",
        "authorization": "Bearer test-network-plan-token",
    }


def test_http_network_plan_client_reuses_and_closes_internal_client() -> None:
    class TrackingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests = 0
            self.closes = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests += 1
            return httpx.Response(
                200,
                json={"code": 404, "success": False, "data": None},
            )

        async def aclose(self) -> None:
            self.closes += 1

    transport = TrackingTransport()
    client = HttpNetworkPlanClient(
        "https://network-plan.test/query",
        1,
        transport=transport,
        query_parameter="subnet",
    )

    async def use_and_close() -> None:
        await client.lookup(IPv4Network("192.0.2.0/24"))
        await client.lookup(IPv4Network("198.51.100.0/24"))
        await client.aclose()
        await client.aclose()

    asyncio.run(use_and_close())

    assert transport.requests == 2
    assert transport.closes == 1


def test_provider_unknown_fields_are_ignored_but_known_types_are_strict() -> None:
    valid = {
        "code": 200,
        "msg": "success",
        "data": {
            "area": "A",
            "areaId": "A-1",
            "regionName": "R",
            "platformName": "P",
            "network": "192.0.2.0/24",
            "gateway": None,
            "subnet": "192.0.2.0/24",
            "vlanId": None,
            "usageCode": None,
            "description": None,
            "newProviderField": "ignored",
        },
        "success": True,
        "traceId": "ignored",
    }
    lookup = validate_network_plan_response(IPv4Network("192.0.2.0/24"), 200, valid)
    assert lookup.status == "resolved"
    invalid = {**valid, "code": "200"}
    lookup = validate_network_plan_response(IPv4Network("192.0.2.0/24"), 200, invalid)
    assert lookup.error_code == "NETWORK_PLAN_INVALID_RESPONSE"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {}, "NETWORK_PLAN_AUTH_FAILURE"),
        (429, {}, "NETWORK_PLAN_DEPENDENCY_FAILURE"),
        (204, None, "NETWORK_PLAN_INVALID_RESPONSE"),
        (500, {"code": 200, "success": True}, "NETWORK_PLAN_DEPENDENCY_FAILURE"),
    ],
)
def test_http_status_precedence(status: int, body: object, expected: str) -> None:
    lookup = validate_network_plan_response(IPv4Network("192.0.2.0/24"), status, body)
    assert lookup.error_code == expected
