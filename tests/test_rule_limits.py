from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from app.schemas import PortRange
from app.services.canonical import canonical_from_catalog
from app.services.catalog import CatalogSegment, NetworkCatalog
from app.services.rule_loader import PolicyBundle
from app.services.splitter import AccessCombination

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def policies() -> PolicyBundle:
    policy_dir = PROJECT_ROOT / "policies"
    catalog = NetworkCatalog.load(policy_dir / "network_catalog.yaml")
    return PolicyBundle.load(policy_dir, catalog.version)


def _combination(
    *,
    source: str = "192.0.2.10/32",
    destination: str = "198.51.100.20/32",
    protocol: str = "tcp",
    port_start: int = 443,
    port_end: int = 443,
) -> AccessCombination:
    return AccessCombination(
        source=canonical_from_catalog("source", 0, _segment(source), ""),
        destination=canonical_from_catalog("destination", 0, _segment(destination), ""),
        protocol=protocol,
        port=PortRange(start=port_start, end=port_end),
        source_description="",
        destination_description="",
        request_description="",
    )


def _segment(cidr: str) -> CatalogSegment:
    return CatalogSegment(
        network=ipaddress.ip_network(cidr, strict=True),
        original=cidr,
        matches=(),
    )


def _matched_rule_ids(
    policies: PolicyBundle, combination: AccessCombination
) -> set[str]:
    return {rule.id for rule in policies.match(combination, total_combinations=1)}


@pytest.mark.parametrize(
    ("source", "expected_rule_ids"),
    [
        pytest.param("10.20.0.0/16", set(), id="ipv4-prefix-16-allowed"),
        pytest.param(
            "10.20.0.0/15",
            {"LEAST-CIDR-001"},
            id="ipv4-prefix-15-too-broad",
        ),
        pytest.param("2001:db8:1::/48", set(), id="ipv6-prefix-48-allowed"),
        pytest.param(
            "2001:db8::/47",
            {"LEAST-CIDR-001"},
            id="ipv6-prefix-47-too-broad",
        ),
    ],
)
def test_cidr_prefix_boundaries(
    policies: PolicyBundle, source: str, expected_rule_ids: set[str]
) -> None:
    assert _matched_rule_ids(policies, _combination(source=source)) == expected_rule_ids


@pytest.mark.parametrize(
    ("port_end", "expected_rule_ids"),
    [
        pytest.param(1099, set(), id="span-100-allowed"),
        pytest.param(1100, {"LEAST-PORT-001"}, id="span-101-too-wide"),
    ],
)
def test_port_span_boundaries(
    policies: PolicyBundle, port_end: int, expected_rule_ids: set[str]
) -> None:
    combination = _combination(port_start=1000, port_end=port_end)
    assert _matched_rule_ids(policies, combination) == expected_rule_ids


@pytest.mark.parametrize(
    ("protocol", "expected_rule_ids"),
    [
        pytest.param("tcp", {"PORT-001"}, id="tcp-23-is-telnet"),
        pytest.param("udp", set(), id="udp-23-is-not-telnet"),
    ],
)
def test_telnet_rule_is_protocol_specific(
    policies: PolicyBundle, protocol: str, expected_rule_ids: set[str]
) -> None:
    combination = _combination(protocol=protocol, port_start=23, port_end=23)
    assert _matched_rule_ids(policies, combination) == expected_rule_ids
