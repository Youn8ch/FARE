from __future__ import annotations

from pathlib import Path

from app.schemas import PortRange
from app.services.canonical import canonical_from_catalog
from app.services.catalog import NetworkCatalog
from app.services.rule_loader import PolicyBundle
from app.services.splitter import AccessCombination

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ZONE_POLICY_DIR = Path(__file__).resolve().parent / "fixtures" / "policies" / "zone_relation"


def _load_bundle(policy_dir: Path) -> tuple[NetworkCatalog, PolicyBundle]:
    catalog = NetworkCatalog.load(policy_dir / "network_catalog.yaml")
    return catalog, PolicyBundle.load(policy_dir, catalog.version)


def _combination(
    catalog: NetworkCatalog, source: str, destination: str
) -> AccessCombination:
    source_segments = catalog.split_and_resolve(source)
    destination_segments = catalog.split_and_resolve(destination)
    assert len(source_segments) == 1
    assert len(destination_segments) == 1
    return AccessCombination(
        source=canonical_from_catalog("source", 0, source_segments[0], ""),
        destination=canonical_from_catalog("destination", 0, destination_segments[0], ""),
        protocol="tcp",
        port=PortRange(start=443, end=443),
        source_description="",
        destination_description="",
        request_description="",
    )


def test_zone_relation_fixture_is_a_complete_loadable_policy_bundle() -> None:
    catalog, policies = _load_bundle(ZONE_POLICY_DIR)

    assert policies.version == catalog.version == "test.zone-relation.1"
    assert policies.rule_ids == {"ZONE-TEST-001"}


def test_zone_relation_fixture_matches_only_the_configured_direction() -> None:
    catalog, policies = _load_bundle(ZONE_POLICY_DIR)
    office_to_production = _combination(catalog, "192.0.2.10", "198.51.100.20")
    production_to_office = _combination(catalog, "198.51.100.20", "192.0.2.10")

    assert [rule.id for rule in policies.match(office_to_production, 1)] == [
        "ZONE-TEST-001"
    ]
    assert policies.match(production_to_office, 1) == []


def test_released_policy_bundle_contains_no_zone_relation_rules() -> None:
    _, policies = _load_bundle(PROJECT_ROOT / "policies")

    assert all(rule.category != "zone_relation" for rule in policies.rules)
