from __future__ import annotations

from dataclasses import dataclass

from app.schemas import EvaluationRequest, PortRange
from app.services.canonical import (
    CanonicalAddressSegment,
    canonical_from_catalog,
    canonical_from_resolved,
)
from app.services.catalog import NetworkCatalog
from app.services.network_plan_resolver import NetworkPlanResolution


@dataclass(frozen=True, slots=True)
class AccessCombination:
    source: CanonicalAddressSegment
    destination: CanonicalAddressSegment
    protocol: str
    port: PortRange
    source_description: str
    destination_description: str
    request_description: str

    @property
    def source_text(self) -> str:
        return (
            str(self.source.access_network)
            if self.source.access_network
            else self.source.original_address
        )

    @property
    def destination_text(self) -> str:
        return (
            str(self.destination.access_network)
            if self.destination.access_network
            else self.destination.original_address
        )


def split_request(
    request: EvaluationRequest, catalog: NetworkCatalog
) -> list[AccessCombination]:
    sources = [
        (canonical_from_catalog("source", index, segment, item.description))
        for index, item in enumerate(request.sources)
        for segment in catalog.split_and_resolve(item.address)
    ]
    destinations = [
        (canonical_from_catalog("destination", index, segment, item.description))
        for index, item in enumerate(request.destinations)
        for segment in catalog.split_and_resolve(item.address)
    ]
    return [
        AccessCombination(
            source=source,
            destination=destination,
            protocol=request.protocol,
            port=port,
            source_description=source.original_description,
            destination_description=destination.original_description,
            request_description=request.request_description,
        )
        for source in sources
        for destination in destinations
        for port in request.ports
    ]


def split_resolved_request(
    request: EvaluationRequest, resolution: NetworkPlanResolution
) -> list[AccessCombination]:
    """Build combinations only after the caller has enforced the item hard limit."""

    return [
        AccessCombination(
            source=canonical_from_resolved(source),
            destination=canonical_from_resolved(destination),
            protocol=request.protocol,
            port=port,
            source_description=source.original_description,
            destination_description=destination.original_description,
            request_description=request.request_description,
        )
        for source in resolution.sources
        for destination in resolution.destinations
        for port in request.ports
    ]
