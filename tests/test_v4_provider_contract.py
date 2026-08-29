"""V4-P3 acceptance: one typed provider fact channel (plan §10).

- mock / http / offline_catalog providers all emit the frozen ProviderLookup
  contract (P3-C01 equivalence at the ProviderNetworkFact / Canonical level);
- HTTP/mock carry no classification and never infer one (P3-C02);
- offline catalog classification is preserved verbatim (P3-C03);
- offline multi-match resolves through the frozen 409 body (P3-C04);
- query limits apply identically across modes (P3-C05 is covered by
  tests/test_provider_unification.py on every mode).
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Network
from pathlib import Path

from app.services.canonical import canonical_from_resolved
from app.services.catalog import NetworkCatalog
from app.services.network_fact_provider import (
    ExplicitNetworkClassification,
    MockNetworkFactProvider,
    OfflineCatalogNetworkFactProvider,
)
from app.services.network_plan_client import (
    MockNetworkPlanClient,
    OfflineCatalogNetworkPlanClient,
)
from app.services.network_plan_resolver import ResolvedAddressSegment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = PROJECT_ROOT / "policies" / "network_catalog.yaml"
DB_SUBNET = IPv4Network("16.1.20.0/24")
APP_SUBNET = IPv4Network("16.1.30.0/24")
MISSING_SUBNET = IPv4Network("16.9.9.0/24")

CANONICAL_FACT_FIELDS = (
    "area",
    "area_id",
    "region_name",
    "platform_name",
    "network",
    "subnet",
    "usage_code",
)
CANONICAL_SEGMENT_FIELDS = (
    "access_network",
    "network_fact_status",
    "error_code",
    "catalog_entry_id",
    "zone",
    "environment",
    "object_type",
    "labels",
)


def _lookup(provider, subnet: IPv4Network):
    return asyncio.run(provider.lookup(subnet))


def _offline_provider() -> OfflineCatalogNetworkFactProvider:
    catalog = NetworkCatalog.load(CATALOG_PATH)
    return OfflineCatalogNetworkFactProvider(
        OfflineCatalogNetworkPlanClient(catalog), catalog
    )


# ---------------------------------------------------------------------------
# P3-C03: offline explicit classification preserved verbatim
# ---------------------------------------------------------------------------


def test_offline_provider_attaches_explicit_classification() -> None:
    provider = _offline_provider()
    result = _lookup(provider, DB_SUBNET)

    assert result.lookup.status == "resolved"
    assert result.fact is not None
    assert result.fact.source == "offline_catalog"
    classification = result.fact.classification
    assert classification is not None
    assert classification.catalog_entry_id == "PROD-DB"
    assert classification.zone == "production"
    assert classification.environment == "production"
    assert classification.object_type == "database"
    assert classification.labels == frozenset({"production-db"})
    # 冻结的伪装行为不变（D2）：zone -> area/areaId/regionName,
    # environment -> platformName；usage_code 不伪装 object_type。
    assert result.fact.fact.area_id == "production"
    assert result.fact.fact.platform_name == "production"
    assert result.fact.fact.usage_code is None


# ---------------------------------------------------------------------------
# P3-C02: mock/http carry no classification
# ---------------------------------------------------------------------------


def test_mock_provider_has_no_classification_without_explicit_map() -> None:
    provider = MockNetworkFactProvider(
        MockNetworkPlanClient(
            PROJECT_ROOT / "tests/fixtures/network_plan/core_catalog.v1.json"
        )
    )
    result = _lookup(provider, APP_SUBNET)

    assert result.lookup.status == "resolved"
    assert result.fact is not None
    assert result.fact.classification is None
    assert result.fact.source == "mock"


# ---------------------------------------------------------------------------
# P3-C01: mock (with simulated classification) == offline canonical equality
# ---------------------------------------------------------------------------


def test_mock_and_offline_produce_equal_provider_facts_and_canonical_segments() -> None:
    offline = _offline_provider()
    offline_result = _lookup(offline, DB_SUBNET)
    assert offline_result.fact is not None

    # 等价 mock：fixture 复刻 offline 的（伪装后）API 形状事实，并以显式
    # classification 模拟同一目录条目（V4 §10.2 Mock 语义）。
    catalog = NetworkCatalog.load(CATALOG_PATH)
    entry = next(e for e in catalog.entries if e.id == "PROD-DB")
    mock = MockNetworkFactProvider(
        MockNetworkPlanClient(None),
        classification_by_subnet={
            str(DB_SUBNET): ExplicitNetworkClassification(
                catalog_entry_id=entry.id,
                zone=entry.zone,
                environment=entry.environment,
                object_type=entry.object_type,
                labels=entry.labels,
            )
        },
    )
    mock.transport.responses = {
        str(DB_SUBNET): {
            "http_status": 200,
            "body": {
                "code": 200,
                "msg": "success",
                "data": {
                    "area": entry.zone,
                    "areaId": entry.zone,
                    "regionName": entry.zone,
                    "platformName": entry.environment,
                    "network": str(entry.network),
                    "gateway": None,
                    "subnet": str(DB_SUBNET),
                    "vlanId": None,
                    "usageCode": None,
                    "description": entry.id,
                },
                "success": True,
            },
        }
    }
    mock_result = _lookup(mock, DB_SUBNET)
    assert mock_result.fact is not None

    # ProviderNetworkFact 内容完整相等（source 标识来源方，允许不同）
    assert mock_result.fact.fact == offline_result.fact.fact
    assert mock_result.fact.classification == offline_result.fact.classification

    # Canonical segment 完整相等（逐字段，不只比 decision）
    offline_segment = _segment_from(offline_result)
    mock_segment = _segment_from(mock_result)
    assert offline_segment == mock_segment
    offline_canonical = canonical_from_resolved(offline_segment)
    mock_canonical = canonical_from_resolved(mock_segment)
    assert offline_canonical == mock_canonical
    for field in CANONICAL_SEGMENT_FIELDS:
        assert getattr(offline_canonical, field) == getattr(mock_canonical, field), field
    for fact_a, fact_b in zip(
        offline_canonical.network_facts, mock_canonical.network_facts, strict=True
    ):
        for field in CANONICAL_FACT_FIELDS:
            assert getattr(fact_a, field) == getattr(fact_b, field), field


def _segment_from(provider_lookup):
    fact = provider_lookup.fact.fact if provider_lookup.fact else None
    return ResolvedAddressSegment(
        role="destination",
        original_index=0,
        original_address=str(fact.subnet) if fact else "16.1.20.20",
        original_description="生产数据库",
        access_network=IPv4Network(fact.subnet) if fact else None,
        query_subnets=(DB_SUBNET,),
        network_fact_ids=(fact.fact_id,) if fact else (),
        network_facts=(fact,) if fact else (),
        region_key=None,
        network_fact_status="complete" if fact else "invalid_response",
        error_code=provider_lookup.lookup.error_code,
        classification=(
            provider_lookup.fact.classification
            if provider_lookup.fact is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# P3-C04: offline multi-match keeps the frozen 409 -> INVALID_RESPONSE path
# ---------------------------------------------------------------------------


def test_offline_multi_match_is_invalid_response_without_classification(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "network_catalog.yaml"
    catalog_path.write_text(
        'version: "2026.08.0"\n'
        "networks:\n"
        "  - id: OVERLAP-A\n"
        "    cidr: 16.9.0.0/24\n"
        "    zone: zone-a\n"
        "    environment: production\n"
        "    object_type: application\n"
        "    labels: [a]\n"
        "  - id: OVERLAP-B\n"
        "    cidr: 16.9.0.0/24\n"
        "    zone: zone-b\n"
        "    environment: production\n"
        "    object_type: application\n"
        "    labels: [b]\n",
        encoding="utf-8",
    )
    catalog = NetworkCatalog.load(catalog_path)
    provider = OfflineCatalogNetworkFactProvider(
        OfflineCatalogNetworkPlanClient(catalog), catalog
    )
    result = _lookup(provider, IPv4Network("16.9.0.0/24"))

    # 冻结现状：provider 409 -> NETWORK_PLAN_INVALID_RESPONSE；无分类、无事实
    assert result.lookup.status == "invalid_response"
    assert result.lookup.error_code == "NETWORK_PLAN_INVALID_RESPONSE"
    assert result.fact is None


def test_offline_lookup_outside_catalog_is_not_found() -> None:
    provider = _offline_provider()
    result = _lookup(provider, MISSING_SUBNET)
    assert result.lookup.status == "not_found"
    assert result.lookup.error_code == "NETWORK_PLAN_NOT_FOUND"
    assert result.fact is None
