from __future__ import annotations

from pathlib import Path

from app.services.canonical import canonical_from_catalog
from app.services.catalog import NetworkCatalog
from app.services.rule_loader import PolicyBundle, Rule


def test_catalog_splits_request_at_authoritative_boundary():
    catalog = NetworkCatalog.load(Path("policies/network_catalog.yaml"))
    segments = catalog.split_and_resolve("16.1.20.0/23")
    mapped = [segment for segment in segments if segment.entry]
    unmapped = [segment for segment in segments if segment.error_code == "ZONE_UNRESOLVED"]
    assert [str(segment.network) for segment in mapped] == ["16.1.20.0/24"]
    assert unmapped
    assert sum(segment.network.num_addresses for segment in segments if segment.network) == 512


def test_single_ip_is_normalized_to_host_prefix():
    catalog = NetworkCatalog.load(Path("policies/network_catalog.yaml"))
    assert str(catalog.split_and_resolve("20.1.2.3")[0].network) == "20.1.2.3/32"


def test_zone_rule_engine_is_only_enabled_by_an_explicit_test_rule():
    catalog = NetworkCatalog.load(Path("policies/network_catalog.yaml"))
    rule = Rule(
        id="ZONE-TEST-001",
        name="区域规则测试夹具",
        category="zone_relation",
        decision="待定",
        reason_type="policy_violation",
        when={"source_zone": "office", "destination_zone": "production"},
        reason_template="仅用于测试。",
        recommendation="仅用于测试。",
        description="未进入默认发布包的区域能力测试规则",
        semantic_keywords=("区域",),
        evidence_requirements=("source_zone", "destination_zone"),
        remediation_template="仅用于测试。",
    )
    bundle = PolicyBundle(version=catalog.version, released_at="2026-07-14", rules=(rule,))
    source_segments = catalog.split_and_resolve("20.1.10.10")
    destination_segments = catalog.split_and_resolve("16.1.30.20")
    combination = _combination(source_segments[0], destination_segments[0])
    assert [matched.id for matched in bundle.match(combination, 1)] == ["ZONE-TEST-001"]


def _combination(source, destination):
    from app.schemas import PortRange
    from app.services.splitter import AccessCombination

    return AccessCombination(
        source=canonical_from_catalog("source", 0, source, ""),
        destination=canonical_from_catalog("destination", 0, destination, ""),
        protocol="tcp",
        port=PortRange(start=443, end=443),
        source_description="",
        destination_description="",
        request_description="",
    )
