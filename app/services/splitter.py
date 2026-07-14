from __future__ import annotations

from dataclasses import dataclass

from app.schemas import EvaluationRequest, PortRange
from app.services.catalog import CatalogSegment, NetworkCatalog


@dataclass(frozen=True, slots=True)
class AccessCombination:
    source: CatalogSegment
    destination: CatalogSegment
    protocol: str
    port: PortRange
    source_description: str
    destination_description: str
    request_description: str

    @property
    def source_text(self) -> str:
        return str(self.source.network) if self.source.network else self.source.original

    @property
    def destination_text(self) -> str:
        return (
            str(self.destination.network)
            if self.destination.network
            else self.destination.original
        )


def split_request(request: EvaluationRequest, catalog: NetworkCatalog) -> list[AccessCombination]:
    sources = [
        (segment, item.description)
        for item in request.sources
        for segment in catalog.split_and_resolve(item.address)
    ]
    destinations = [
        (segment, item.description)
        for item in request.destinations
        for segment in catalog.split_and_resolve(item.address)
    ]
    return [
        AccessCombination(
            source=source,
            destination=destination,
            protocol=request.protocol,
            port=port,
            source_description=source_description,
            destination_description=destination_description,
            request_description=request.request_description,
        )
        for source, source_description in sources
        for destination, destination_description in destinations
        for port in request.ports
    ]
