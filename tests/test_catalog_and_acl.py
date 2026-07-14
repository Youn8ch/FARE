from __future__ import annotations

from pathlib import Path

from app.schemas import AclRawResponse, EvaluationRequest
from app.services.acl_extract import AclFactExtractor
from app.services.catalog import NetworkCatalog
from app.services.rule_loader import PolicyBundle, Rule
from app.services.splitter import split_request


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


def test_explicit_no_path_requires_explicit_text():
    extractor = AclFactExtractor()
    missing = extractor.extract(AclRawResponse(analysis="没有可解析设备信息", config=""))
    explicit = extractor.extract(
        AclRawResponse(analysis="未找到该访问组合经过的防火墙。", config="")
    )
    assert missing.explicit_no_path is False
    assert explicit.explicit_no_path is True
    assert explicit.evidence


def test_acl_facts_are_extracted_without_claiming_live_state():
    facts = AclFactExtractor().extract(
        AclRawResponse(
            analysis="候选路径经过防火墙 FW-A。",
            config="access-list ACL-DEMO object-group SRC-GROUP port 443",
        )
    )
    assert facts.firewalls == ["FW-A"]
    assert facts.candidate_acls == ["ACL-DEMO"]
    assert facts.address_objects == ["SRC-GROUP"]
    assert facts.observed_ports == [443]


def test_zone_rule_engine_is_only_enabled_by_an_explicit_test_rule():
    catalog = NetworkCatalog.load(Path("policies/network_catalog.yaml"))
    request = EvaluationRequest.model_validate(
        {
            "request_id": "zone-fixture",
            "sources": [{"address": "20.1.10.10"}],
            "destinations": [{"address": "16.1.30.20"}],
            "protocol": "tcp",
            "ports": [{"start": 443, "end": 443}],
        }
    )
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
    combination = split_request(request, catalog)[0]
    assert [matched.id for matched in bundle.match(combination, 1)] == ["ZONE-TEST-001"]
