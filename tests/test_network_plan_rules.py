from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
import yaml

from app.schemas import NetworkPlanFact, PortRange
from app.services.canonical import canonical_from_resolved
from app.services.network_plan_resolver import ResolvedAddressSegment
from app.services.rule_loader import PolicyBundle
from app.services.splitter import AccessCombination


def _write_bundle(path: Path, when: dict[str, object]) -> PolicyBundle:
    path.mkdir()
    (path / "manifest.yaml").write_text(
        """version: '2026.08.0'
released_at: '2026-08-05'
sources: [{id: test, name: test}]
approvals: [{role: owner, approver: test, approved_at: '2026-08-05'}]
""",
        encoding="utf-8",
    )
    rules = {
        "rules": [
            {
                "id": "ZONE-TEST-001",
                "name": "区域测试规则",
                "description": "区域测试规则",
                "category": "zone_relation",
                "decision": "待定",
                "reason_type": "policy_violation",
                "when": when,
                "semantic_keywords": [],
                "evidence_requirements": [],
                "reason_template": "区域关系需要复核。",
                "recommendation": "人工复核。",
                "remediation_template": "人工复核。",
            },
        ]
    }
    (path / "compliance_rules.yaml").write_text(
        yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return PolicyBundle.load(path)


def _segment(role: str, cidr: str, fact: NetworkPlanFact) -> ResolvedAddressSegment:
    network = ipaddress.ip_network(cidr)
    return ResolvedAddressSegment(
        role=role,  # type: ignore[arg-type]
        original_index=0,
        original_address=cidr,
        original_description="",
        access_network=network,
        query_subnets=(ipaddress.ip_network(fact.query_subnet),),
        network_fact_ids=(fact.fact_id,),
        network_facts=(fact,),
        region_key=(
            fact.area_id,
            fact.region_name,
            fact.platform_name,
            fact.network,
            fact.usage_code or "",
        ),
        network_fact_status="complete",
        error_code=None,
    )


def _fact(fact_id: str, subnet: str, area: str, platform: str) -> NetworkPlanFact:
    return NetworkPlanFact(
        fact_id=fact_id,
        query_subnet=subnet,
        area=area,
        area_id=area,
        region_name=area,
        platform_name=platform,
        network=subnet,
        gateway=None,
        subnet=subnet,
        vlan_id=None,
        usage_code=None,
        description=None,
    )


def test_zone_rule_matches_authoritative_planning_fields(tmp_path: Path) -> None:
    policies = _write_bundle(
        tmp_path / "policy",
        {
            "source_area_id": "本部办公区",
            "destination_area_id": "核心生产区",
            "destination_platform_name": "数据库平台",
        },
    )
    source_fact = _fact("NPF-10D20800", "16.210.8.0/24", "本部办公区", "办公网络")
    destination_fact = _fact(
        "NPF-10DC1000", "16.220.16.0/24", "核心生产区", "数据库平台"
    )
    combination = AccessCombination(
        source=canonical_from_resolved(_segment("source", "16.210.8.10/32", source_fact)),
        destination=canonical_from_resolved(
            _segment("destination", "16.220.16.20/32", destination_fact)
        ),
        protocol="tcp",
        port=PortRange(start=443, end=443),
        source_description="",
        destination_description="",
        request_description="",
    )
    assert {rule.id for rule in policies.match(combination, 1)} == {"ZONE-TEST-001"}


def test_unknown_rule_condition_is_rejected_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported matching conditions"):
        _write_bundle(
            tmp_path / "policy",
            {"destination_platfrom_name": "拼写错误"},
        )
