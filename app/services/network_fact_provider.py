"""V4-P3: the single typed provider-fact channel.

mock / http / offline_catalog providers all produce the same frozen
``ProviderLookup`` result (validated lookup + optional explicit
classification + transport-level audit raw). The resolver consumes only this
contract: it never imports the catalog, never re-queries a second fact
source, and never parses transport DTOs (V4 plan §10).

The offline compatibility provider keeps its masqueraded transport body
(zone -> area/areaId/regionName, environment -> platformName; frozen
behavior, docs/v3-baseline.md §4.3) while the explicit catalog classification
travels the typed channel that replaces the resolver's ``legacy_entry``
second query. ``usage_code`` is never masqueraded from ``object_type``.
"""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from pydantic import ValidationError

from app.schemas import (
    NetworkPlanFact,
    NetworkPlanLookup,
    NetworkPlanTransportResponse,
    ProviderNetworkPlanResponse,
)
from app.services.catalog import NetworkCatalog
from app.services.network_plan_client import (
    HttpNetworkPlanClient,
    MockNetworkPlanClient,
    NetworkPlanClient,
    OfflineCatalogNetworkPlanClient,
)


@dataclass(frozen=True, slots=True)
class ExplicitNetworkClassification:
    """Explicit catalog classification; never inferred (V4 invariant 4.1.4)."""

    catalog_entry_id: str | None = None
    zone: str | None = None
    environment: str | None = None
    object_type: str | None = None
    labels: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ProviderNetworkFact:
    """The typed provider fact: normalized plan fact + optional explicit
    classification (V4 §10.2 — no fourth duplicated fact model)."""

    fact: NetworkPlanFact
    classification: ExplicitNetworkClassification | None = None
    source: str = "http"


@dataclass(frozen=True, slots=True)
class ProviderLookup:
    """One /24 provider result entering the resolver.

    ``raw`` is the frozen transport-level audit record:
    ``{query_subnet, http_status, validation[, body | raw_text]}``.
    """

    lookup: NetworkPlanLookup
    fact: ProviderNetworkFact | None
    raw: dict[str, object]


class NetworkFactProvider(ABC):
    """The typed provider boundary consumed by the resolver."""

    transport: NetworkPlanClient

    @abstractmethod
    async def lookup(self, subnet: IPv4Network) -> ProviderLookup:
        raise NotImplementedError

    async def aclose(self) -> None:
        await self.transport.aclose()


class TransportValidatedNetworkFactProvider(NetworkFactProvider):
    """Shared transport -> validation -> typed fact path (http / mock)."""

    source: str = "http"

    def __init__(
        self, transport: NetworkPlanClient, *, source: str | None = None
    ) -> None:
        self.transport = transport
        if source is not None:
            self.source = source

    async def lookup(self, subnet: IPv4Network) -> ProviderLookup:
        transport_response = await self.transport.lookup(subnet)
        return _typed_lookup(
            subnet, transport_response, classification=None, source=self.source
        )


class HttpNetworkFactProvider(TransportValidatedNetworkFactProvider):
    """HTTP transport provider.

    ``classification`` stays None: zone/environment/object_type/labels are
    never inferred from usage_code, platform_name, or free-text description.
    """

    source = "http"

    def __init__(self, transport: HttpNetworkPlanClient) -> None:
        super().__init__(transport, source="http")


class MockNetworkFactProvider(TransportValidatedNetworkFactProvider):
    """Mock fixture provider.

    ``classification_by_subnet`` lets a fixture simulate an
    explicit-classification provider (equivalence tests against the offline
    catalog); without it classification is None like HTTP.
    """

    source = "mock"

    def __init__(
        self,
        transport: MockNetworkPlanClient,
        classification_by_subnet: dict[str, ExplicitNetworkClassification]
        | None = None,
    ) -> None:
        super().__init__(transport, source="mock")
        self._classification_by_subnet = dict(classification_by_subnet or {})

    @property
    def fixture_version(self) -> str | None:
        return getattr(self.transport, "fixture_version", None)

    @property
    def purpose(self) -> str | None:
        return getattr(self.transport, "purpose", None)

    async def lookup(self, subnet: IPv4Network) -> ProviderLookup:
        result = await super().lookup(subnet)
        classification = self._classification_by_subnet.get(str(subnet))
        if classification is not None and result.fact is not None:
            result = replace(
                result, fact=replace(result.fact, classification=classification)
            )
        return result


class OfflineCatalogNetworkFactProvider(NetworkFactProvider):
    """Explicit offline compatibility provider (never an implicit fallback).

    The transport body masquerade is frozen baseline behavior; the explicit
    catalog classification rides the typed channel. A /24 with zero or
    multiple matching catalog entries resolves through the transport's
    frozen 404/409 bodies and carries no classification.
    """

    source = "offline_catalog"

    def __init__(
        self,
        transport: OfflineCatalogNetworkPlanClient,
        catalog: NetworkCatalog,
    ) -> None:
        self.transport = transport
        self.catalog = catalog

    async def lookup(self, subnet: IPv4Network) -> ProviderLookup:
        transport_response = await self.transport.lookup(subnet)
        result = _typed_lookup(
            subnet, transport_response, classification=None, source=self.source
        )
        if result.lookup.status == "resolved":
            classification = self._classification_for(subnet)
            if classification is not None and result.fact is not None:
                result = replace(
                    result,
                    fact=replace(result.fact, classification=classification),
                )
        return result

    def _classification_for(
        self, subnet: IPv4Network
    ) -> ExplicitNetworkClassification | None:
        matches = [
            entry
            for entry in self.catalog.entries
            if entry.network.version == 4 and subnet.subnet_of(entry.network)
        ]
        if len(matches) != 1:
            return None
        entry = matches[0]
        return ExplicitNetworkClassification(
            catalog_entry_id=entry.id,
            zone=entry.zone,
            environment=entry.environment,
            object_type=entry.object_type,
            labels=entry.labels,
        )


def _typed_lookup(
    subnet: IPv4Network,
    transport_response: NetworkPlanTransportResponse,
    *,
    classification: ExplicitNetworkClassification | None,
    source: str,
) -> ProviderLookup:
    lookup = validate_network_plan_response(
        subnet, transport_response.http_status, transport_response.body
    )
    raw: dict[str, object] = {
        "query_subnet": str(subnet),
        "http_status": transport_response.http_status,
        "validation": lookup.model_dump(mode="json"),
    }
    if transport_response.body is not None:
        raw["body"] = transport_response.body
    elif transport_response.raw_text is not None:
        raw["raw_text"] = transport_response.raw_text
    fact = None
    if lookup.data is not None:
        fact = ProviderNetworkFact(
            fact=lookup.data, classification=classification, source=source
        )
    return ProviderLookup(lookup=lookup, fact=fact, raw=raw)


def validate_network_plan_response(
    query_subnet: IPv4Network, http_status: int, body: object | None
) -> NetworkPlanLookup:
    if query_subnet.prefixlen != 24:
        raise ValueError("network plan response validation requires an IPv4 /24")
    if http_status in {401, 403}:
        return failure_lookup(
            query_subnet,
            "dependency_failure",
            "NETWORK_PLAN_AUTH_FAILURE",
            "network plan authorization failed",
        )
    if http_status >= 500 or http_status in {408, 429}:
        return failure_lookup(
            query_subnet,
            "dependency_failure",
            "NETWORK_PLAN_DEPENDENCY_FAILURE",
            "network plan dependency failed",
        )
    if http_status not in {200, 404} or body is None:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    try:
        provider = ProviderNetworkPlanResponse.model_validate(body)
    except ValidationError:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    if (
        provider.code == 404
        and provider.success is False
        and provider.data is None
        and http_status in {200, 404}
    ):
        return failure_lookup(
            query_subnet,
            "not_found",
            "NETWORK_PLAN_NOT_FOUND",
            "network plan does not exist",
        )
    if http_status == 404:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    if provider.code != 200 or provider.success is not True or provider.data is None:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    data = provider.data
    try:
        returned_subnet = ipaddress.ip_network(data.subnet, strict=True)
        network = ipaddress.ip_network(data.network, strict=True)
        if not isinstance(returned_subnet, IPv4Network) or returned_subnet.prefixlen != 24:
            raise ValueError
        if not isinstance(network, IPv4Network):
            raise ValueError
        if data.gateway is not None:
            gateway = ipaddress.ip_address(data.gateway)
            if not isinstance(gateway, IPv4Address):
                raise ValueError
    except ValueError:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    if returned_subnet != query_subnet:
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_SUBNET_MISMATCH",
            "network plan subnet does not match the query",
        )
    if not returned_subnet.subnet_of(network):
        return failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_NETWORK_MISMATCH",
            "network plan subnet is outside its planned network",
        )
    fact_id = identifier("NPF", query_subnet)
    fact = NetworkPlanFact(
        fact_id=fact_id,
        query_subnet=str(query_subnet),
        area=data.area,
        area_id=data.area_id,
        region_name=data.region_name,
        platform_name=data.platform_name,
        network=str(network),
        gateway=data.gateway,
        subnet=str(returned_subnet),
        vlan_id=data.vlan_id,
        usage_code=data.usage_code,
        description=data.description,
    )
    return NetworkPlanLookup(
        lookup_id=identifier("NPL", query_subnet),
        query_subnet=str(query_subnet),
        status="resolved",
        fact_id=fact_id,
        data=fact,
    )


FailureStatus = Literal[
    "not_found", "dependency_failure", "invalid_response", "conflict"
]


def failure_lookup(
    subnet: IPv4Network,
    status: FailureStatus,
    code: str,
    message: str,
) -> NetworkPlanLookup:
    return NetworkPlanLookup(
        lookup_id=identifier("NPL", subnet),
        query_subnet=str(subnet),
        status=status,
        error_code=code,
        error_message=message,
    )


def identifier(prefix: str, subnet: IPv4Network) -> str:
    return f"{prefix}-{int(subnet.network_address):08X}"
