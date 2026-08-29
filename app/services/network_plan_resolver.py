from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import Literal

from pydantic import ValidationError

from app.schemas import (
    EvaluationCardinality,
    EvaluationRequest,
    NetworkAnalysis,
    NetworkFactStatus,
    NetworkPlanFact,
    NetworkPlanLookup,
    NetworkRegion,
    ProviderNetworkPlanResponse,
)
from app.services.catalog import NetworkCatalog, NetworkEntry
from app.services.network_plan_client import (
    NetworkPlanClient,
    NetworkPlanTransportError,
)


class NetworkPlanQueryLimitError(ValueError):
    def __init__(self, actual: int, limit: int) -> None:
        super().__init__("unique /24 query count exceeds the configured limit")
        self.actual = actual
        self.limit = limit


class EvaluationItemLimitError(ValueError):
    def __init__(self, actual: int, limit: int) -> None:
        super().__init__("evaluation item count exceeds the configured limit")
        self.actual = actual
        self.limit = limit


@dataclass(frozen=True, slots=True)
class ResolvedAddressSegment:
    role: Literal["source", "destination"]
    original_index: int
    original_address: str
    original_description: str
    access_network: IPv4Network | IPv6Network | None
    query_subnets: tuple[IPv4Network, ...]
    network_fact_ids: tuple[str, ...]
    network_facts: tuple[NetworkPlanFact, ...]
    region_key: tuple[str, ...] | None
    network_fact_status: NetworkFactStatus
    error_code: str | None
    # Explicit catalog fields for the offline compatibility provider only.
    legacy_entry: NetworkEntry | None = None

    @property
    def primary_fact(self) -> NetworkPlanFact | None:
        return self.network_facts[0] if self.network_facts else None


@dataclass(frozen=True, slots=True)
class NetworkPlanResolution:
    sources: tuple[ResolvedAddressSegment, ...]
    destinations: tuple[ResolvedAddressSegment, ...]
    analysis: NetworkAnalysis
    raw_records: tuple[dict[str, object], ...]


class NetworkPlanResolver:
    def __init__(
        self,
        client: NetworkPlanClient,
        *,
        max_subnets: int,
        max_concurrency: int,
        lookup_timeout: float,
        batch_timeout: float,
        offline_catalog: NetworkCatalog | None = None,
    ) -> None:
        if max_subnets < 1 or max_concurrency < 1:
            raise ValueError("network plan limits must be positive")
        if lookup_timeout <= 0 or batch_timeout <= 0:
            raise ValueError("network plan timeouts must be positive")
        self.client = client
        self.max_subnets = max_subnets
        self.max_concurrency = max_concurrency
        self.lookup_timeout = lookup_timeout
        self.batch_timeout = batch_timeout
        self.offline_catalog = offline_catalog

    def cardinality(self, request: EvaluationRequest) -> EvaluationCardinality:
        intervals: list[tuple[int, int]] = []
        source_upper = _collect_intervals(request.sources, intervals)
        destination_upper = _collect_intervals(request.destinations, intervals)
        unique_count = _merged_interval_count(intervals)
        return EvaluationCardinality(
            unique_query_subnet_count=unique_count,
            source_segment_upper_bound=source_upper,
            destination_segment_upper_bound=destination_upper,
            port_range_count=len(request.ports),
            item_upper_bound=source_upper * destination_upper * len(request.ports),
        )

    def ensure_query_limit(self, request: EvaluationRequest) -> EvaluationCardinality:
        cardinality = self.cardinality(request)
        if cardinality.unique_query_subnet_count > self.max_subnets:
            raise NetworkPlanQueryLimitError(
                cardinality.unique_query_subnet_count, self.max_subnets
            )
        return cardinality

    async def resolve(self, request: EvaluationRequest) -> NetworkPlanResolution:
        self.ensure_query_limit(request)
        query_subnets = _materialize_query_plan(request)
        lookups, raw_records = await self._lookup_all(query_subnets)
        lookup_by_subnet = {lookup.query_subnet: lookup for lookup in lookups}
        sources = tuple(
            segment
            for index, item in enumerate(request.sources)
            for segment in _resolve_address(
                "source",
                index,
                item.address,
                item.description,
                lookup_by_subnet,
                self.offline_catalog,
            )
        )
        destinations = tuple(
            segment
            for index, item in enumerate(request.destinations)
            for segment in _resolve_address(
                "destination",
                index,
                item.address,
                item.description,
                lookup_by_subnet,
                self.offline_catalog,
            )
        )
        analysis = NetworkAnalysis(
            lookups=lookups,
            source_regions=[_region(segment) for segment in sources],
            destination_regions=[_region(segment) for segment in destinations],
        )
        return NetworkPlanResolution(
            sources=sources,
            destinations=destinations,
            analysis=analysis,
            raw_records=tuple(raw_records),
        )

    async def _lookup_all(
        self, subnets: list[IPv4Network]
    ) -> tuple[list[NetworkPlanLookup], list[dict[str, object]]]:
        if not subnets:
            return [], []
        queue: asyncio.Queue[IPv4Network] = asyncio.Queue()
        for subnet in subnets:
            queue.put_nowait(subnet)
        results: dict[str, NetworkPlanLookup] = {}
        raw_records: dict[str, dict[str, object]] = {}

        async def worker() -> None:
            while True:
                try:
                    subnet = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                lookup, raw = await self._lookup_one(subnet)
                results[str(subnet)] = lookup
                raw_records[str(subnet)] = raw
                queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self.max_concurrency, len(subnets)))
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*workers), self.batch_timeout)
        except TimeoutError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for subnet in subnets:
                key = str(subnet)
                if key not in results:
                    lookup = _failure_lookup(
                        subnet,
                        "dependency_failure",
                        "NETWORK_PLAN_DEPENDENCY_FAILURE",
                        "network plan batch timeout",
                    )
                    results[key] = lookup
                    raw_records[key] = {
                        "query_subnet": key,
                        "error_code": lookup.error_code,
                    }
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        ordered_keys = [str(subnet) for subnet in subnets]
        return (
            [results[key] for key in ordered_keys],
            [raw_records[key] for key in ordered_keys],
        )

    async def _lookup_one(
        self, subnet: IPv4Network
    ) -> tuple[NetworkPlanLookup, dict[str, object]]:
        try:
            transport = await asyncio.wait_for(
                self.client.lookup(subnet), timeout=self.lookup_timeout
            )
        except (TimeoutError, NetworkPlanTransportError) as exc:
            lookup = _failure_lookup(
                subnet,
                "dependency_failure",
                "NETWORK_PLAN_DEPENDENCY_FAILURE",
                "network plan dependency failed",
            )
            return lookup, {
                "query_subnet": str(subnet),
                "transport_error": type(exc).__name__,
                "validation": lookup.model_dump(mode="json"),
            }
        lookup = validate_network_plan_response(subnet, transport.http_status, transport.body)
        raw: dict[str, object] = {
            "query_subnet": str(subnet),
            "http_status": transport.http_status,
            "validation": lookup.model_dump(mode="json"),
        }
        if transport.body is not None:
            raw["body"] = transport.body
        elif transport.raw_text is not None:
            raw["raw_text"] = transport.raw_text
        return lookup, raw


def validate_network_plan_response(
    query_subnet: IPv4Network, http_status: int, body: object | None
) -> NetworkPlanLookup:
    if query_subnet.prefixlen != 24:
        raise ValueError("network plan response validation requires an IPv4 /24")
    if http_status in {401, 403}:
        return _failure_lookup(
            query_subnet,
            "dependency_failure",
            "NETWORK_PLAN_AUTH_FAILURE",
            "network plan authorization failed",
        )
    if http_status >= 500 or http_status in {408, 429}:
        return _failure_lookup(
            query_subnet,
            "dependency_failure",
            "NETWORK_PLAN_DEPENDENCY_FAILURE",
            "network plan dependency failed",
        )
    if http_status not in {200, 404} or body is None:
        return _failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    try:
        provider = ProviderNetworkPlanResponse.model_validate(body)
    except ValidationError:
        return _failure_lookup(
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
        return _failure_lookup(
            query_subnet,
            "not_found",
            "NETWORK_PLAN_NOT_FOUND",
            "network plan does not exist",
        )
    if http_status == 404:
        return _failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    if provider.code != 200 or provider.success is not True or provider.data is None:
        return _failure_lookup(
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
        return _failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_INVALID_RESPONSE",
            "network plan response is invalid",
        )
    if returned_subnet != query_subnet:
        return _failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_SUBNET_MISMATCH",
            "network plan subnet does not match the query",
        )
    if not returned_subnet.subnet_of(network):
        return _failure_lookup(
            query_subnet,
            "invalid_response",
            "NETWORK_PLAN_NETWORK_MISMATCH",
            "network plan subnet is outside its planned network",
        )
    fact_id = _identifier("NPF", query_subnet)
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
        lookup_id=_identifier("NPL", query_subnet),
        query_subnet=str(query_subnet),
        status="resolved",
        fact_id=fact_id,
        data=fact,
    )


def _failure_lookup(
    subnet: IPv4Network,
    status: Literal[
        "not_found", "dependency_failure", "invalid_response", "conflict"
    ],
    code: str,
    message: str,
) -> NetworkPlanLookup:
    return NetworkPlanLookup(
        lookup_id=_identifier("NPL", subnet),
        query_subnet=str(subnet),
        status=status,
        error_code=code,
        error_message=message,
    )


def _identifier(prefix: str, subnet: IPv4Network) -> str:
    return f"{prefix}-{int(subnet.network_address):08X}"


def _collect_intervals(items: list[object], intervals: list[tuple[int, int]]) -> int:
    upper = 0
    for item in items:
        address = str(item.address)
        if address.lower() == "any":
            upper += 1
            continue
        network = ipaddress.ip_network(address, strict=False)
        if network.version == 6:
            upper += 1
            continue
        first = int(network.network_address) >> 8
        last = int(network.broadcast_address) >> 8
        intervals.append((first, last))
        upper += last - first + 1
    return upper


def _merged_interval_count(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    count = 0
    current_start, current_end = sorted(intervals)[0]
    for start, end in sorted(intervals)[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
        else:
            count += current_end - current_start + 1
            current_start, current_end = start, end
    return count + current_end - current_start + 1


def _materialize_query_plan(request: EvaluationRequest) -> list[IPv4Network]:
    values: set[int] = set()
    for item in [*request.sources, *request.destinations]:
        if item.address.lower() == "any":
            continue
        network = ipaddress.ip_network(item.address, strict=False)
        if network.version == 6:
            continue
        first = int(network.network_address) >> 8
        last = int(network.broadcast_address) >> 8
        values.update(range(first, last + 1))
    return [IPv4Network((value << 8, 24)) for value in sorted(values)]


@dataclass(frozen=True, slots=True)
class _Piece:
    lower: int
    upper: int
    lookup: NetworkPlanLookup

    @property
    def key(self) -> tuple[object, ...]:
        fact = self.lookup.data
        if fact is None:
            return (self.lookup.status, self.lookup.error_code)
        return (
            self.lookup.status,
            fact.area_id,
            fact.region_name,
            fact.platform_name,
            fact.network,
            fact.usage_code,
        )


def _resolve_address(
    role: Literal["source", "destination"],
    original_index: int,
    address: str,
    description: str,
    lookups: dict[str, NetworkPlanLookup],
    offline_catalog: NetworkCatalog | None,
) -> list[ResolvedAddressSegment]:
    if address.lower() == "any":
        return [
            ResolvedAddressSegment(
                role=role,
                original_index=original_index,
                original_address=address,
                original_description=description,
                access_network=None,
                query_subnets=(),
                network_fact_ids=(),
                network_facts=(),
                region_key=None,
                network_fact_status="not_applicable",
                error_code="ADDRESS_ANY",
            )
        ]
    network = ipaddress.ip_network(address, strict=False)
    if isinstance(network, IPv6Network):
        return [
            ResolvedAddressSegment(
                role=role,
                original_index=original_index,
                original_address=address,
                original_description=description,
                access_network=network,
                query_subnets=(),
                network_fact_ids=(),
                network_facts=(),
                region_key=None,
                network_fact_status="invalid_response",
                error_code="NETWORK_PLAN_IPV6_UNSUPPORTED",
            )
        ]
    lower = int(network.network_address)
    upper = int(network.broadcast_address)
    first = lower >> 8
    last = upper >> 8
    pieces: list[_Piece] = []
    for value in range(first, last + 1):
        query = IPv4Network((value << 8, 24))
        lookup = lookups[str(query)]
        pieces.append(
            _Piece(
                lower=max(lower, int(query.network_address)),
                upper=min(upper, int(query.broadcast_address)),
                lookup=lookup,
            )
        )
    groups: list[list[_Piece]] = []
    for piece in pieces:
        if (
            groups
            and groups[-1][-1].upper + 1 == piece.lower
            and groups[-1][-1].key == piece.key
        ):
            groups[-1].append(piece)
        else:
            groups.append([piece])
    segments: list[ResolvedAddressSegment] = []
    for group in groups:
        for summarized in ipaddress.summarize_address_range(
            IPv4Address(group[0].lower), IPv4Address(group[-1].upper)
        ):
            relevant = [
                piece
                for piece in group
                if piece.lower <= int(summarized.broadcast_address)
                and piece.upper >= int(summarized.network_address)
            ]
            resolved = [piece.lookup.data for piece in relevant if piece.lookup.data]
            facts = tuple(fact for fact in resolved if fact is not None)
            fact_ids = tuple(dict.fromkeys(fact.fact_id for fact in facts))
            query_subnets = tuple(
                IPv4Network(piece.lookup.query_subnet) for piece in relevant
            )
            lookup = relevant[0].lookup
            fact = facts[0] if facts else None
            status = _fact_status(lookup)
            region_key = (
                (
                    fact.area_id,
                    fact.region_name,
                    fact.platform_name,
                    fact.network,
                    fact.usage_code or "",
                )
                if fact
                else None
            )
            legacy_entry = _legacy_entry(offline_catalog, summarized)
            segments.append(
                ResolvedAddressSegment(
                    role=role,
                    original_index=original_index,
                    original_address=address,
                    original_description=description,
                    access_network=summarized,
                    query_subnets=query_subnets,
                    network_fact_ids=fact_ids,
                    network_facts=facts,
                    region_key=region_key,
                    network_fact_status=status,
                    error_code=lookup.error_code,
                    legacy_entry=legacy_entry,
                )
            )
    return segments


def _fact_status(lookup: NetworkPlanLookup) -> NetworkFactStatus:
    return {
        "resolved": "complete",
        "not_found": "not_found",
        "dependency_failure": "dependency_failure",
        "invalid_response": "invalid_response",
        "conflict": "conflict",
    }[lookup.status]  # type: ignore[return-value]


def _legacy_entry(
    catalog: NetworkCatalog | None, network: IPv4Network
) -> NetworkEntry | None:
    if catalog is None:
        return None
    matches = [entry for entry in catalog.entries if network.subnet_of(entry.network)]
    return matches[0] if len(matches) == 1 else None


def _region(segment: ResolvedAddressSegment) -> NetworkRegion:
    fact = segment.primary_fact
    return NetworkRegion(
        role=segment.role,
        original_index=segment.original_index,
        original_address=segment.original_address,
        access_network=(
            str(segment.access_network) if segment.access_network is not None else "any"
        ),
        network_fact_ids=list(segment.network_fact_ids),
        status=segment.network_fact_status,
        area_id=fact.area_id if fact else None,
        region_name=fact.region_name if fact else None,
        platform_name=fact.platform_name if fact else None,
        usage_code=fact.usage_code if fact else None,
        error_code=segment.error_code,
    )
