from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections import OrderedDict
from ipaddress import IPv4Network
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from app.schemas import NetworkPlanTransportResponse
from app.services.catalog import NetworkCatalog


class NetworkPlanTransportError(RuntimeError):
    """A transport failure which has no valid provider response."""


class NetworkPlanFixtureError(NetworkPlanTransportError):
    pass


def _require_query_subnet(subnet: IPv4Network) -> None:
    if not isinstance(subnet, IPv4Network) or subnet.prefixlen != 24:
        raise ValueError("network plan lookup requires a canonical IPv4 /24")


class NetworkPlanClient(ABC):
    @abstractmethod
    async def lookup(self, subnet: IPv4Network) -> NetworkPlanTransportResponse:
        raise NotImplementedError


class TtlNetworkPlanClient(NetworkPlanClient):
    """Bounded positive-result cache; failures and 404 responses are never cached."""

    def __init__(self, inner: NetworkPlanClient, ttl_seconds: float, max_entries: int) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("network plan cache TTL and capacity must be positive")
        self.inner = inner
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[
            str, tuple[float, NetworkPlanTransportResponse]
        ] = OrderedDict()
        self._lock = asyncio.Lock()

    async def lookup(self, subnet: IPv4Network) -> NetworkPlanTransportResponse:
        _require_query_subnet(subnet)
        key = str(subnet)
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                expires_at, response = cached
                if expires_at > monotonic():
                    self._entries.move_to_end(key)
                    return response
                self._entries.pop(key, None)
        response = await self.inner.lookup(subnet)
        if _is_cacheable_success(response):
            async with self._lock:
                self._entries[key] = (monotonic() + self.ttl_seconds, response)
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
        return response


def _is_cacheable_success(response: NetworkPlanTransportResponse) -> bool:
    body = response.body
    return bool(
        response.http_status == 200
        and isinstance(body, dict)
        and body.get("code") == 200
        and body.get("success") is True
        and body.get("data") is not None
    )


class MockNetworkPlanClient(NetworkPlanClient):
    def __init__(self, fixture_path: Path | None) -> None:
        self.fixture_path = fixture_path
        self.fixture_version: str | None = None
        self.purpose: str | None = None
        self.responses: dict[str, Any] = {}
        self.calls: list[str] = []
        self.active_calls = 0
        self.max_active_calls = 0
        if fixture_path is not None:
            self._load(fixture_path)

    def _load(self, path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid network plan mock fixture: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError("network plan fixture root must be an object")
        version = value.get("fixture_version")
        purpose = value.get("purpose")
        responses = value.get("responses")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("network plan fixture_version is required")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("network plan fixture purpose is required")
        if not isinstance(responses, dict):
            raise ValueError("network plan fixture responses must be an object")
        self.fixture_version = version
        self.purpose = purpose
        self.responses = responses

    async def lookup(self, subnet: IPv4Network) -> NetworkPlanTransportResponse:
        _require_query_subnet(subnet)
        key = str(subnet)
        self.calls.append(key)
        raw = self.responses.get(key)
        if raw is None:
            raise NetworkPlanFixtureError(
                f"network plan fixture has no response for subnet {key}"
            )
        if not isinstance(raw, dict):
            raise NetworkPlanFixtureError(
                f"network plan fixture response for {key} must be an object"
            )
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            delay = raw.get("delay_ms", 0)
            if not isinstance(delay, int) or isinstance(delay, bool) or delay < 0:
                raise NetworkPlanFixtureError(f"invalid delay_ms for subnet {key}")
            if delay:
                await asyncio.sleep(delay / 1000)
            error = raw.get("transport_error")
            if error is not None:
                if error not in {"timeout", "connection_error"}:
                    raise NetworkPlanFixtureError(
                        f"unsupported transport_error for subnet {key}"
                    )
                raise NetworkPlanTransportError(str(error))
            status = raw.get("http_status", 200)
            if not isinstance(status, int) or isinstance(status, bool):
                raise NetworkPlanFixtureError(f"invalid http_status for subnet {key}")
            if "raw_body" in raw:
                raw_text = raw["raw_body"]
                if not isinstance(raw_text, str):
                    raise NetworkPlanFixtureError(f"raw_body for {key} must be text")
                return NetworkPlanTransportResponse(
                    http_status=status, body=None, raw_text=raw_text
                )
            return NetworkPlanTransportResponse(
                http_status=status,
                body=raw.get("body"),
                raw_text=None,
            )
        finally:
            self.active_calls -= 1


class HttpNetworkPlanClient(NetworkPlanClient):
    """HTTP transport boundary.

    The provider method, parameter name and authentication contract are not approved in
    the supplied plan. The adapter therefore accepts an injected request builder for
    contract tests and fails closed in production until that contract is configured.
    """

    def __init__(
        self,
        url: str,
        timeout: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        query_parameter: str | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.transport = transport
        self.query_parameter = query_parameter

    async def lookup(self, subnet: IPv4Network) -> NetworkPlanTransportResponse:
        _require_query_subnet(subnet)
        if not self.query_parameter:
            raise NetworkPlanTransportError(
                "network plan HTTP contract is not configured"
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.get(
                    self.url, params={self.query_parameter: str(subnet)}
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise NetworkPlanTransportError(type(exc).__name__) from exc
        try:
            body: object | None = response.json()
            raw_text = None
        except ValueError:
            body = None
            raw_text = response.text
        return NetworkPlanTransportResponse(
            http_status=response.status_code,
            body=body,
            raw_text=raw_text,
        )


class OfflineCatalogNetworkPlanClient(NetworkPlanClient):
    """Explicit compatibility adapter; never used as an implicit HTTP fallback."""

    def __init__(self, catalog: NetworkCatalog) -> None:
        self.catalog = catalog
        self.calls: list[str] = []

    async def lookup(self, subnet: IPv4Network) -> NetworkPlanTransportResponse:
        _require_query_subnet(subnet)
        self.calls.append(str(subnet))
        matches = [
            entry
            for entry in self.catalog.entries
            if entry.network.version == 4 and subnet.subnet_of(entry.network)
        ]
        if len(matches) != 1:
            if not matches:
                return NetworkPlanTransportResponse(
                    http_status=200,
                    body={
                        "code": 404,
                        "msg": "网段规划不存在",
                        "data": None,
                        "success": False,
                    },
                )
            return NetworkPlanTransportResponse(
                http_status=200,
                body={
                    "code": 409,
                    "msg": "offline catalog contains conflicting network facts",
                    "data": None,
                    "success": False,
                },
            )
        entry = matches[0]
        return NetworkPlanTransportResponse(
            http_status=200,
            body={
                "code": 200,
                "msg": "success",
                "data": {
                    "area": entry.zone,
                    "areaId": entry.zone,
                    "regionName": entry.zone,
                    "platformName": entry.environment,
                    "network": str(entry.network),
                    "gateway": None,
                    "subnet": str(subnet),
                    "vlanId": None,
                    "usageCode": entry.object_type,
                    "description": entry.id,
                },
                "success": True,
            },
        )
