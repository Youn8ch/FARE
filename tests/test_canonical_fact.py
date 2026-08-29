"""AC-01 acceptance: canonical fact model introduces normalization only.

Inputs come from the AC-00 unified dataset (CASE-01 provider data via the
versioned multi_region fixture); no business inference is allowed anywhere in
the canonical layer.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import EvaluationRequest, NetworkPlanFact
from app.services.canonical import (
    CanonicalAddressSegment,
    canonical_fact_from_plan,
    canonical_from_catalog,
    canonical_from_resolved,
)
from app.services.catalog import NetworkCatalog
from app.services.network_plan_client import MockNetworkPlanClient
from app.services.network_plan_resolver import NetworkPlanResolver
from app.services.splitter import split_resolved_request
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _plan_fact(**overrides) -> NetworkPlanFact:
    value = {
        "fact_id": "NPF-0DC10000",
        "query_subnet": "16.220.16.0/24",
        "area": "核心生产区",
        "area_id": "核心生产区",
        "region_name": "核心生产数据库区",
        "platform_name": "核心数据库平台",
        "network": "16.220.16.0/22",
        "gateway": "16.220.16.1",
        "subnet": "16.220.16.0/24",
        "vlan_id": "916",
        "usage_code": "PROD_DATABASE",
        "description": "生产数据库服务器",
    }
    value.update(overrides)
    return NetworkPlanFact.model_validate(value)


def _canonical_combinations(settings: Settings, payload: dict):
    request = EvaluationRequest.model_validate(payload)
    resolver = NetworkPlanResolver(
        MockNetworkPlanClient(settings.network_plan_mock_file),
        max_subnets=settings.network_plan_max_subnets_per_request,
        max_concurrency=settings.network_plan_max_concurrency,
        lookup_timeout=settings.network_plan_timeout_seconds,
        batch_timeout=settings.network_plan_batch_timeout_seconds,
    )
    resolution = asyncio.run(resolver.resolve(request))
    return split_resolved_request(request, resolution), resolution


# ---------------------------------------------------------------------------
# Normalization inputs (AC-01 5.2 / 5.4)
# ---------------------------------------------------------------------------


def test_input_a_case01_source_fact_is_normalized_verbatim() -> None:
    fact = canonical_fact_from_plan(
        _plan_fact(
            fact_id="NPF-10C90100",
            query_subnet="16.201.1.0/24",
            area="鳌峰科创生产区",
            area_id="鳌峰科创生产区",
            region_name="鳌峰生产区（科创鲲鹏应用）",
            platform_name="鳌峰生产区（科创鲲鹏应用）",
            network="16.201.0.0/22",
            gateway="16.201.0.1",
            subnet="16.201.1.0/24",
            vlan_id="300",
            usage_code=None,
            description="鲲鹏虚拟机（云管分配）",
        )
    )
    assert fact.area_id == "鳌峰科创生产区"
    assert fact.region_name == "鳌峰生产区（科创鲲鹏应用）"
    assert fact.platform_name == "鳌峰生产区（科创鲲鹏应用）"
    assert fact.network == "16.201.0.0/22"
    assert fact.usage_code is None
    assert fact.object_type is None
    assert fact.environment is None


def test_input_b_usage_code_is_kept_but_never_mapped() -> None:
    fact = canonical_fact_from_plan(_plan_fact())
    assert fact.usage_code == "PROD_DATABASE"
    assert fact.object_type is None
    assert fact.environment is None


def test_input_c_empty_usage_code_stays_empty() -> None:
    fact = canonical_fact_from_plan(_plan_fact(usage_code=None, description=None))
    assert fact.usage_code is None
    assert fact.object_type is None
    assert fact.environment is None


def test_no_inference_from_usage_code_platform_or_description() -> None:
    suspicious = canonical_fact_from_plan(_plan_fact())
    assert suspicious.object_type is None
    assert suspicious.environment is None

    office = canonical_fact_from_plan(
        _plan_fact(
            usage_code="OFFICE_CLIENT",
            platform_name="办公网络",
            description="办公终端",
        )
    )
    assert office.object_type is None
    assert office.environment is None


# ---------------------------------------------------------------------------
# Canonical segments on the real resolver chain (CASE-01 / CASE-02 / CASE-12)
# ---------------------------------------------------------------------------


def test_case01_combinations_hold_canonical_segments(settings: Settings) -> None:
    combinations, resolution = _canonical_combinations(
        _mock_chain(settings), _payload("ac01-case-01")
    )
    assert combinations
    for combination in combinations:
        assert isinstance(combination.source, CanonicalAddressSegment)
        assert isinstance(combination.destination, CanonicalAddressSegment)
    source = combinations[0].source
    assert source.access_network == ipaddress.ip_network("16.201.1.10/32")
    assert list(source.network_fact_ids) == ["NPF-10C90100"]
    assert source.zone is None
    assert source.object_type is None
    assert source.environment is None
    fact = source.primary_fact
    assert fact is not None
    assert fact.usage_code is None
    assert fact.object_type is None
    assert fact.environment is None

    destination = combinations[0].destination
    assert destination.access_network == ipaddress.ip_network("16.220.16.20/32")
    assert list(destination.network_fact_ids) == ["NPF-10DC1000"]
    destination_fact = destination.primary_fact
    assert destination_fact is not None
    assert destination_fact.usage_code == "PROD_DATABASE"
    assert destination_fact.object_type is None
    assert destination_fact.environment is None
    # 未改变 CASE-01 基线：真实访问范围保持 /32
    assert combinations[0].source_text == "16.201.1.10/32"
    assert combinations[0].destination_text == "16.220.16.20/32"
    assert len(resolution.analysis.lookups) == 2


def test_case02_not_found_is_carried_into_canonical_segment(
    settings: Settings,
) -> None:
    payload = _payload(
        "ac01-case-02", sources=[{"address": "16.201.3.10", "description": "应用"}]
    )
    combinations, _ = _canonical_combinations(_mock_chain(settings), payload)
    source = combinations[0].source
    assert source.network_fact_status == "not_found"
    assert source.error_code == "NETWORK_PLAN_NOT_FOUND"
    assert source.network_facts == ()
    assert source.primary_fact is None


def test_case12_cross_region_cidr_splits_by_real_request_scope(
    settings: Settings,
) -> None:
    payload = _payload(
        "ac01-case-12",
        sources=[{"address": "16.201.1.0/24", "description": "跨区域源段"}],
    )
    combinations, _ = _canonical_combinations(_mock_chain(settings), payload)
    # 申请 16.201.1.0/24 只允许查询/拆分该 /24 的子范围；不得扩大到 16.201.0.0/22
    sources = {
        str(combination.source.access_network) for combination in combinations
    }
    assert sources == {"16.201.1.0/24"}
    assert all(
        str(combination.source.access_network) != "16.201.0.0/22"
        for combination in combinations
    )
    source = combinations[0].source
    assert source.network_fact_status == "complete"
    assert list(source.network_fact_ids) == ["NPF-10C90100"]


def test_case12_multi_region_cidr_splits_at_region_boundaries(
    settings: Settings,
) -> None:
    # /23 横跨 16.201.2.0/24（通用计算平台）与 16.201.3.0/24（404）：
    # 属性不同必须拆分，访问范围只按真实申请范围拆为两个 /24
    payload = _payload(
        "ac01-case-12-multi",
        sources=[{"address": "16.201.2.0/23", "description": "混合区域源段"}],
    )
    combinations, _ = _canonical_combinations(_mock_chain(settings), payload)
    by_network = {
        str(combination.source.access_network): combination.source
        for combination in combinations
    }
    assert set(by_network) == {"16.201.2.0/24", "16.201.3.0/24"}
    assert by_network["16.201.2.0/24"].network_fact_status == "complete"
    assert by_network["16.201.2.0/24"].primary_fact is not None
    assert by_network["16.201.3.0/24"].network_fact_status == "not_found"
    assert by_network["16.201.2.0/24"].region_key != (
        by_network["16.201.3.0/24"].region_key
    )
    # 不得扩大到整个 16.201.0.0/22
    assert "16.201.0.0/22" not in by_network


# ---------------------------------------------------------------------------
# Catalog normalization (offline compatibility source; explicit fields only)
# ---------------------------------------------------------------------------


def test_catalog_entry_fields_are_carried_as_explicit_values() -> None:
    catalog = NetworkCatalog.load(PROJECT_ROOT / "policies/network_catalog.yaml")
    segments = catalog.split_and_resolve("16.1.20.20")
    assert len(segments) == 1
    canonical = canonical_from_catalog("destination", 0, segments[0], "生产数据库")
    assert canonical.zone == "production"
    assert canonical.environment == "production"
    assert canonical.object_type == "database"
    assert canonical.labels == frozenset({"production-db"})
    assert canonical.catalog_entry_id == "PROD-DB"
    assert canonical.network_fact_status == "complete"
    assert isinstance(canonical, CanonicalAddressSegment)


def test_catalog_unresolved_address_keeps_error_code() -> None:
    catalog = NetworkCatalog.load(PROJECT_ROOT / "policies/network_catalog.yaml")
    segments = catalog.split_and_resolve("any")
    canonical = canonical_from_catalog("source", 0, segments[0], "全来源")
    assert canonical.access_network is None
    assert canonical.error_code == "ADDRESS_ANY"
    assert canonical.zone is None


def test_resolved_conversion_preserves_provider_metadata(settings: Settings) -> None:
    combinations, _ = _canonical_combinations(
        _mock_chain(settings), _payload("ac01-metadata")
    )
    source = combinations[0].source
    assert source.role == "source"
    assert source.original_address == "16.201.1.10"
    assert source.original_description == "应用"
    assert source.query_subnets == (ipaddress.ip_network("16.201.1.0/24"),)
    resolved_view = canonical_from_resolved  # 恒等引用检查（转换器唯一）
    assert resolved_view.__module__ == "app.services.canonical"


# ---------------------------------------------------------------------------
# CASE-01 API baseline unchanged (AC-01 限制规则：不改 decision)
# ---------------------------------------------------------------------------


def test_case01_api_baseline_unchanged(settings: Settings) -> None:
    with TestClient(create_app(_mock_chain(settings))) as client:
        response = client.post("/v1/evaluations", json=_payload("ac01-api-case-01"))
    assert response.status_code == 200
    body = response.json()
    item = body["items"][0]
    assert item["access"]["source"] == "16.201.1.10/32"
    assert item["source_network_fact_ids"] == ["NPF-10C90100"]
    assert item["destination_network_fact_ids"] == ["NPF-10DC1000"]
    assert item["source_network_fact_status"] == "complete"
    assert item["decision"] == "合规"
    assert body["decision"] == "合规"


def test_canonical_fact_is_frozen() -> None:
    fact = canonical_fact_from_plan(_plan_fact())
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.usage_code = "PROD_DATABASE"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.object_type = "database"  # type: ignore[misc]
