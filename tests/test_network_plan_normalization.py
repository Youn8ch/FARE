from __future__ import annotations

from app.schemas import EvaluationRequest
from app.services.network_fact_provider import MockNetworkFactProvider
from app.services.network_plan_client import MockNetworkPlanClient
from app.services.network_plan_resolver import (
    NetworkPlanQueryLimitError,
    NetworkPlanResolver,
)


def _request(sources: list[str], destinations: list[str] | None = None) -> EvaluationRequest:
    return EvaluationRequest.model_validate(
        {
            "request_id": "network-plan-normalization",
            "sources": [{"address": value} for value in sources],
            "destinations": [
                {"address": value} for value in (destinations or ["16.220.16.20"])
            ],
            "protocol": "tcp",
            "ports": [{"start": 443, "end": 443}],
        }
    )


def _resolver(limit: int = 64) -> NetworkPlanResolver:
    return NetworkPlanResolver(
        MockNetworkFactProvider(MockNetworkPlanClient(None)),
        max_subnets=limit,
        max_concurrency=2,
        lookup_timeout=1,
        batch_timeout=2,
    )


def test_large_prefix_is_counted_without_materializing_subnets() -> None:
    request = _request(["0.0.0.0/0"])
    cardinality = _resolver().cardinality(request)
    assert cardinality.unique_query_subnet_count == 2**24
    try:
        _resolver().ensure_query_limit(request)
    except NetworkPlanQueryLimitError as exc:
        assert (exc.actual, exc.limit) == (2**24, 64)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("the /0 request must be rejected")


def test_overlapping_ranges_are_merged_before_counting() -> None:
    request = _request(
        ["16.201.0.0/23", "16.201.1.0/24", "16.201.1.128/25"],
        ["16.201.0.10"],
    )
    assert _resolver().cardinality(request).unique_query_subnet_count == 2


def test_any_and_ipv6_do_not_enter_ipv4_query_count() -> None:
    request = _request(["any", "2001:db8::1"], ["any"])
    assert _resolver().cardinality(request).unique_query_subnet_count == 0
