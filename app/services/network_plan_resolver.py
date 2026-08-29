from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from typing import Literal

from app.schemas import (
    EvaluationCardinality,
    EvaluationRequest,
    NetworkAnalysis,
    NetworkFactStatus,
    NetworkPlanFact,
    NetworkPlanLookup,
    NetworkRegion,
)
from app.services.network_fact_provider import (
    ExplicitNetworkClassification,
    NetworkFactProvider,
    ProviderLookup,
    failure_lookup,
)
from app.services.network_plan_client import NetworkPlanTransportError


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
    # Explicit catalog classification from the typed provider fact channel
    # (offline compatibility source; never inferred).
    classification: ExplicitNetworkClassification | None = None

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
        provider: NetworkFactProvider,
        *,
        max_subnets: int,
        max_concurrency: int,
        lookup_timeout: float,
        batch_timeout: float,
    ) -> None:
        if max_subnets < 1 or max_concurrency < 1:
            raise ValueError("network plan limits must be positive")
        if lookup_timeout <= 0 or batch_timeout <= 0:
            raise ValueError("network plan timeouts must be positive")
        self.provider = provider
        self.max_subnets = max_subnets
        self.max_concurrency = max_concurrency
        self.lookup_timeout = lookup_timeout
        self.batch_timeout = batch_timeout

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
        provider_lookups, raw_records = await self._lookup_all(query_subnets)
        lookup_by_subnet = {
            provider_lookup.lookup.query_subnet: provider_lookup
            for provider_lookup in provider_lookups
        }
        sources = tuple(
            segment
            for index, item in enumerate(request.sources)
                for segment in _resolve_address(
                    "source",
                    index,
                    item.address,
                    item.description,
                    lookup_by_subnet,
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
                )
        )
        analysis = NetworkAnalysis(
            lookups=[
                provider_lookup.lookup
                for provider_lookup in provider_lookups
            ],
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
    ) -> tuple[list[ProviderLookup], list[dict[str, object]]]:
        if not subnets:
            return [], []
        queue: asyncio.Queue[IPv4Network] = asyncio.Queue()
        for subnet in subnets:
            queue.put_nowait(subnet)
        results: dict[str, ProviderLookup] = {}
        raw_records: dict[str, dict[str, object]] = {}

        async def worker() -> None:
            while True:
                try:
                    subnet = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                provider_lookup = await self._lookup_one(subnet)
                results[str(subnet)] = provider_lookup
                raw_records[str(subnet)] = provider_lookup.raw
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
                    lookup = failure_lookup(
                        subnet,
                        "dependency_failure",
                        "NETWORK_PLAN_DEPENDENCY_FAILURE",
                        "network plan batch timeout",
                    )
                    results[key] = ProviderLookup(
                        lookup=lookup,
                        fact=None,
                        raw={
                            "query_subnet": key,
                            "error_code": lookup.error_code,
                        },
                    )
                    raw_records[key] = results[key].raw
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

    async def _lookup_one(self, subnet: IPv4Network) -> ProviderLookup:
        try:
            return await asyncio.wait_for(
                self.provider.lookup(subnet), timeout=self.lookup_timeout
            )
        except (TimeoutError, NetworkPlanTransportError) as exc:
            lookup = failure_lookup(
                subnet,
                "dependency_failure",
                "NETWORK_PLAN_DEPENDENCY_FAILURE",
                "network plan dependency failed",
            )
            return ProviderLookup(
                lookup=lookup,
                fact=None,
                raw={
                    "query_subnet": str(subnet),
                    "transport_error": type(exc).__name__,
                    "validation": lookup.model_dump(mode="json"),
                },
            )


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
    provider_lookup: ProviderLookup

    @property
    def key(self) -> tuple[object, ...]:
        provider_fact = self.provider_lookup.fact
        fact = provider_fact.fact if provider_fact is not None else None
        if fact is None:
            return (
                self.provider_lookup.lookup.status,
                self.provider_lookup.lookup.error_code,
            )
        return (
            self.provider_lookup.lookup.status,
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
    lookups: dict[str, ProviderLookup],
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
        pieces.append(
            _Piece(
                lower=max(lower, int(query.network_address)),
                upper=min(upper, int(query.broadcast_address)),
                provider_lookup=lookups[str(query)],
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
            resolved = [
                piece.provider_lookup.fact.fact
                for piece in relevant
                if piece.provider_lookup.fact is not None
            ]
            facts = tuple(resolved)
            fact_ids = tuple(dict.fromkeys(fact.fact_id for fact in facts))
            query_subnets = tuple(
                IPv4Network(piece.provider_lookup.lookup.query_subnet)
                for piece in relevant
            )
            lookup = relevant[0].provider_lookup.lookup
            status = _fact_status(lookup)
            classification = _common_classification(relevant)
            primary_fact = facts[0] if facts else None
            region_key = (
                (
                    primary_fact.area_id,
                    primary_fact.region_name,
                    primary_fact.platform_name,
                    primary_fact.network,
                    primary_fact.usage_code or "",
                )
                if primary_fact
                else None
            )
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
                    classification=classification,
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


def _common_classification(
    pieces: list[_Piece],
) -> ExplicitNetworkClassification | None:
    """The segment's explicit classification from its provider facts.

    The historical second-query condition is preserved: a classification
    exists only when every resolved fact in the segment shares the same
    explicit classification (equivalent to the summarized network falling
    inside exactly one catalog entry).
    """

    values = [
        piece.provider_lookup.fact.classification
        for piece in pieces
        if piece.provider_lookup.fact is not None
        and piece.provider_lookup.fact.classification is not None
    ]
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values[1:]):
        return first
    return None


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
