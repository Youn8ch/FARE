from __future__ import annotations

import asyncio
from ipaddress import IPv4Network
from pathlib import Path

import pytest

from app.services.network_plan_client import MockNetworkPlanClient, TtlNetworkPlanClient
from app.services.network_plan_resolver import validate_network_plan_response

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
