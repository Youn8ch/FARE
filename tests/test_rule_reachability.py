"""AC-03 acceptance: RuleEngine consumes canonical facts only; default active
rules are reachable with positive and near-miss cases; OBJECT-001 is disabled
in the default package until a formal mapping exists (plan 7.3/7.4/7.6).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import PortRange
from app.services.canonical import CanonicalAddressSegment, CanonicalNetworkFact
from app.services.rule_loader import PolicyBundle
from app.services.splitter import AccessCombination
from tests.test_architecture_baseline import _mock_chain, _payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBJECT_POLICY_DIR = (
    PROJECT_ROOT / "tests/fixtures/policies/network_plan_object_relation"
)


def _bundle() -> PolicyBundle:
    return PolicyBundle.load(PROJECT_ROOT / "policies")


def _object_bundle() -> PolicyBundle:
    return PolicyBundle.load(OBJECT_POLICY_DIR)


def _segment(cidr: str | None, role: str = "source") -> CanonicalAddressSegment:
    network = ipaddress.ip_network(cidr) if cidr else None
    return CanonicalAddressSegment(
        role=role,  # type: ignore[arg-type]
        original_index=0,
        original_address=cidr or "any",
        original_description="",
        access_network=network,
        query_subnets=(),
        network_fact_ids=(),
        network_facts=(),
        region_key=None,
        network_fact_status="complete" if network else "not_applicable",
        error_code=None if network else "ADDRESS_ANY",
    )


def _combination(
    source: CanonicalAddressSegment,
    destination: CanonicalAddressSegment,
    *,
    port=(443, 443),
    protocol="tcp",
) -> AccessCombination:
    return AccessCombination(
        source=source,
        destination=destination,
        protocol=protocol,
        port=PortRange(start=port[0], end=port[1]),
        source_description="",
        destination_description="",
        request_description="",
    )


def _fact_segment(
    cidr: str,
    *,
    usage_code: str,
    object_type: str | None = None,
    environment: str | None = None,
    role: str = "source",
) -> CanonicalAddressSegment:
    """Segment carrying provider usage_code plus EXPLICIT canonical fields."""

    fact = CanonicalNetworkFact(
        fact_id="NPF-TEST",
        query_subnet=cidr,
        area=usage_code,
        area_id=usage_code,
        region_name=usage_code,
        platform_name=environment or usage_code,
        network=cidr,
        gateway=None,
        subnet=cidr,
        vlan_id=None,
        usage_code=usage_code,
        description=None,
    )
    return CanonicalAddressSegment(
        role=role,  # type: ignore[arg-type]
        original_index=0,
        original_address=cidr,
        original_description="",
        access_network=ipaddress.ip_network(f"{cidr.split('/')[0]}/32"),
        query_subnets=(ipaddress.ip_network(cidr),),
        network_fact_ids=("NPF-TEST",),
        network_facts=(fact,),
        region_key=None,
        network_fact_status="complete",
        error_code=None,
        object_type=object_type,
        environment=environment,
    )


# ---------------------------------------------------------------------------
# OBJECT-001 处置（7.2 / 7.4）
# ---------------------------------------------------------------------------


def test_default_package_disables_object_001() -> None:
    bundle = _bundle()
    assert "OBJECT-001" not in bundle.rule_ids
    assert "PORT-001" in bundle.rule_ids


def test_object_relation_capability_is_preserved_in_fixture_package() -> None:
    bundle = _object_bundle()
    assert "OBJECT-001" in bundle.rule_ids
    assert bundle.version == "test.object-relation.1"


def test_object_001_does_not_hit_without_explicit_object_fields() -> None:
    """模拟输入 1：usageCode 存在但无正式映射 -> object_type/environment 为
    None，OBJECT-001 不得命中（7.3 禁止隐式 usageCode 字符串匹配）。"""

    bundle = _object_bundle()
    source = _fact_segment("16.210.8.0/24", usage_code="OFFICE_CLIENT")
    destination = _fact_segment(
        "16.220.16.0/24", usage_code="PROD_DATABASE", role="destination"
    )
    assert source.object_type is None
    assert destination.object_type is None
    assert destination.environment is None
    combination = _combination(source, destination)
    assert {rule.id for rule in bundle.match(combination, 1)} == set()


def test_object_001_hits_only_with_explicit_canonical_fields() -> None:
    """模拟输入 2：fixture 显式提供 canonical object_type/environment ->
    OBJECT-001 命中。"""

    bundle = _object_bundle()
    source = _fact_segment(
        "16.210.8.0/24",
        usage_code="OFFICE_CLIENT",
        object_type="endpoint",
    )
    destination = _fact_segment(
        "16.220.16.0/24",
        usage_code="PROD_DATABASE",
        object_type="database",
        environment="production",
        role="destination",
    )
    combination = _combination(source, destination)
    assert {rule.id for rule in bundle.match(combination, 1)} == {"OBJECT-001"}


def test_e2e_usage_codes_alone_never_trigger_object_rule(settings: Settings) -> None:
    """端到端输入 1：provider usageCode=OFFICE_CLIENT/PROD_DATABASE 不触发
    对象规则；默认包中该规则已禁用。"""

    payload = _payload(
        "ac03-usage-only",
        sources=[{"address": "16.210.8.10", "description": "办公终端"}],
        destinations=[{"address": "16.220.16.20", "description": "生产数据库"}],
    )
    with TestClient(create_app(_mock_chain(settings))) as client:
        response = client.post("/v2/evaluations", json=payload)
    assert response.status_code == 200
    body = response.json()
    item = body["items"][0]
    # usage codes 确实到达了 canonical 事实层
    source_region = body["network_analysis"]["source_regions"][0]
    assert source_region["usage_code"] == "OFFICE_CLIENT"
    destination_region = body["network_analysis"]["destination_regions"][0]
    assert destination_region["usage_code"] == "PROD_DATABASE"
    # 但没有正式映射 -> 无规则命中 -> 合规
    assert item["matched_rules"] == []
    assert item["decision"] == "合规"
    assert body["decision"] == "合规"
    assert "OBJECT-001" not in body["semantic_analysis"]["candidate_rule_ids"]


# ---------------------------------------------------------------------------
# 规则可达性（7.6）：默认启用规则各需一正一反
# ---------------------------------------------------------------------------


def test_every_default_active_rule_has_positive_and_near_miss() -> None:
    bundle = _bundle()
    source = _segment("16.1.30.10/32")
    destination = _segment("16.1.30.20/32", role="destination")

    def matched(combination: AccessCombination, total: int = 1) -> set[str]:
        return {rule.id for rule in bundle.match(combination, total)}

    # PORT-001: tcp/23 命中，tcp/22 near-miss
    assert matched(
        _combination(source, destination, port=(23, 23))
    ) == {"PORT-001"}
    assert matched(
        _combination(source, destination, port=(22, 22))
    ) == set()

    # LEAST-ANY-001: any 命中，具体 /32 near-miss
    assert matched(_combination(_segment(None), destination)) == {
        "LEAST-ANY-001"
    }
    assert matched(_combination(source, destination)) == set()

    # LEAST-CIDR-001: /15 命中（规则级），/16 near-miss（端到端由查询上限约束）
    assert "LEAST-CIDR-001" in matched(
        _combination(_segment("10.0.0.0/15"), destination)
    )
    assert "LEAST-CIDR-001" not in matched(
        _combination(_segment("10.0.0.0/16"), destination)
    )

    # LEAST-PORT-001: 1-101 命中，1-100 near-miss
    assert "LEAST-PORT-001" in matched(
        _combination(source, destination, port=(1, 101))
    )
    assert "LEAST-PORT-001" not in matched(
        _combination(source, destination, port=(1, 100))
    )

    # LEAST-COMBINATION-001: 65 组合命中，64 near-miss
    assert "LEAST-COMBINATION-001" in matched(
        _combination(source, destination), total=65
    )
    assert "LEAST-COMBINATION-001" not in matched(
        _combination(source, destination), total=64
    )


def test_default_rules_e2e_reachability_mapping() -> None:
    """每条默认启用规则至少有一个主路径正例挂靠点（v2 套件或本套件）。"""

    mapping = {
        "PORT-001": "core.v2.json tcp_23_is_rejected_v2",
        "LEAST-ANY-001": "core.v2.json any_address_v2",
        "LEAST-CIDR-001": "test_rule_limits.py prefix boundaries (rule-level)",
        "LEAST-PORT-001": "core.v2.json port_span_101_is_rejected_v2",
        "LEAST-COMBINATION-001": "test_rule_reachability.py (rule-level)",
    }
    bundle = _bundle()
    assert bundle.rule_ids == set(mapping)


# ---------------------------------------------------------------------------
# 限制规则静态验证（7.3）
# ---------------------------------------------------------------------------


def test_rule_engine_has_no_catalog_or_entry_dependency() -> None:
    source = (PROJECT_ROOT / "app/services/rule_loader.py").read_text(encoding="utf-8")
    assert "NetworkCatalog" not in source
    assert ".entry" not in source
    assert "primary_fact" in source  # canonical fact accessor
