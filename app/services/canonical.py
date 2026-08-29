"""Canonical network fact model (AC-01).

The canonical model is the single fact vocabulary shared by every stage after
fact resolution. It performs NORMALIZATION ONLY:

- fields are copied verbatim from an authoritative source;
- ``object_type`` / ``environment`` on :class:`CanonicalNetworkFact` default to
  ``None`` and may only be populated from an explicit, formally approved
  mapping (none exists yet) — never inferred from ``usage_code``,
  ``platform_name``, or free-text ``description``;
- catalog entries (``network_catalog.yaml``) are a formal source, so their
  explicit ``zone`` / ``environment`` / ``object_type`` / ``labels`` values are
  carried over as-is on the segment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network
from typing import Literal

from app.schemas import NetworkFactStatus, NetworkPlanFact
from app.services.catalog import CatalogSegment
from app.services.network_plan_resolver import ResolvedAddressSegment

SegmentRole = Literal["source", "destination"]


@dataclass(frozen=True, slots=True)
class CanonicalNetworkFact:
    """Normalized provider fact; no business inference is performed here."""

    fact_id: str
    query_subnet: str
    area: str
    area_id: str
    region_name: str
    platform_name: str
    network: str
    gateway: str | None
    subnet: str
    vlan_id: str | None
    usage_code: str | None
    description: str | None
    # Explicit-mapping only (formal data dictionary required before use).
    object_type: str | None = None
    environment: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalAddressSegment:
    role: SegmentRole
    original_index: int
    original_address: str
    original_description: str
    access_network: IPv4Network | IPv6Network | None
    query_subnets: tuple[IPv4Network, ...]
    network_fact_ids: tuple[str, ...]
    network_facts: tuple[CanonicalNetworkFact, ...]
    region_key: tuple[str, ...] | None
    network_fact_status: NetworkFactStatus
    error_code: str | None
    # Explicit catalog fields (offline compatibility source; never inferred).
    catalog_entry_id: str | None = None
    zone: str | None = None
    environment: str | None = None
    object_type: str | None = None
    labels: frozenset[str] = field(default_factory=frozenset)

    @property
    def primary_fact(self) -> CanonicalNetworkFact | None:
        return self.network_facts[0] if self.network_facts else None


def canonical_fact_from_plan(fact: NetworkPlanFact) -> CanonicalNetworkFact:
    """Normalize a provider fact verbatim; no field is derived or remapped."""

    return CanonicalNetworkFact(
        fact_id=fact.fact_id,
        query_subnet=fact.query_subnet,
        area=fact.area,
        area_id=fact.area_id,
        region_name=fact.region_name,
        platform_name=fact.platform_name,
        network=fact.network,
        gateway=fact.gateway,
        subnet=fact.subnet,
        vlan_id=fact.vlan_id,
        usage_code=fact.usage_code,
        description=fact.description,
    )


def canonical_from_resolved(segment: ResolvedAddressSegment) -> CanonicalAddressSegment:
    """Normalize a resolver segment verbatim, preserving explicit catalog data."""

    facts = tuple(canonical_fact_from_plan(fact) for fact in segment.network_facts)
    entry = segment.classification
    return CanonicalAddressSegment(
        role=segment.role,
        original_index=segment.original_index,
        original_address=segment.original_address,
        original_description=segment.original_description,
        access_network=segment.access_network,
        query_subnets=segment.query_subnets,
        network_fact_ids=segment.network_fact_ids,
        network_facts=facts,
        region_key=segment.region_key,
        network_fact_status=segment.network_fact_status,
        error_code=segment.error_code,
        catalog_entry_id=entry.catalog_entry_id if entry is not None else None,
        zone=entry.zone if entry is not None else None,
        environment=entry.environment if entry is not None else None,
        object_type=entry.object_type if entry is not None else None,
        labels=entry.labels if entry is not None else frozenset(),
    )


def canonical_from_catalog(
    role: SegmentRole,
    original_index: int,
    segment: CatalogSegment,
    description: str,
) -> CanonicalAddressSegment:
    """Normalize a legacy catalog segment verbatim.

    Catalog segments carry no provider fact status; the historical observable
    behavior reports ``complete`` (see AC-00 baseline), which is preserved here.
    """

    entry = segment.entry
    return CanonicalAddressSegment(
        role=role,
        original_index=original_index,
        original_address=segment.original,
        original_description=description,
        access_network=segment.network,
        query_subnets=(),
        network_fact_ids=(),
        network_facts=(),
        region_key=None,
        network_fact_status="complete",
        error_code=segment.error_code,
        catalog_entry_id=entry.id if entry is not None else None,
        zone=entry.zone if entry is not None else None,
        environment=entry.environment if entry is not None else None,
        object_type=entry.object_type if entry is not None else None,
        labels=entry.labels if entry is not None else frozenset(),
    )
