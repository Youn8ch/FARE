from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class NetworkEntry:
    id: str
    network: ipaddress.IPv4Network | ipaddress.IPv6Network
    zone: str
    environment: str
    object_type: str
    labels: frozenset[str]


@dataclass(frozen=True, slots=True)
class CatalogSegment:
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None
    original: str
    matches: tuple[NetworkEntry, ...]
    error_code: str | None = None

    @property
    def entry(self) -> NetworkEntry | None:
        return self.matches[0] if len(self.matches) == 1 else None


class NetworkCatalog:
    def __init__(self, version: str, entries: list[NetworkEntry]) -> None:
        self.version = version
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> NetworkCatalog:
        data = _read_yaml(path)
        version = str(data.get("version", "")).strip()
        if not version:
            raise ValueError("network catalog version is required")
        raw_entries = data.get("networks")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("network catalog must contain at least one network")
        entries: list[NetworkEntry] = []
        ids: set[str] = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError("each network entry must be an object")
            entry_id = _required(raw, "id")
            if entry_id in ids:
                raise ValueError(f"duplicate network id: {entry_id}")
            ids.add(entry_id)
            network = ipaddress.ip_network(_required(raw, "cidr"), strict=True)
            labels = raw.get("labels", [])
            if not isinstance(labels, list):
                raise ValueError(f"labels for {entry_id} must be a list")
            entries.append(
                NetworkEntry(
                    id=entry_id,
                    network=network,
                    zone=_required(raw, "zone"),
                    environment=_required(raw, "environment"),
                    object_type=_required(raw, "object_type"),
                    labels=frozenset(str(label) for label in labels),
                )
            )
        return cls(version, entries)

    def split_and_resolve(self, address: str) -> list[CatalogSegment]:
        if address.strip().lower() == "any":
            return [CatalogSegment(None, address, (), "ADDRESS_ANY")]
        try:
            network = ipaddress.ip_network(address, strict=False)
        except ValueError:
            return [CatalogSegment(None, address, (), "ADDRESS_INVALID")]

        start = int(network.network_address)
        end = int(network.broadcast_address)
        boundaries = {start, end + 1}
        family_entries = [
            entry for entry in self.entries if entry.network.version == network.version
        ]
        for entry in family_entries:
            entry_start = max(start, int(entry.network.network_address))
            entry_end = min(end, int(entry.network.broadcast_address))
            if entry_start <= entry_end:
                boundaries.add(entry_start)
                boundaries.add(entry_end + 1)

        segments: list[CatalogSegment] = []
        ordered = sorted(boundaries)
        addr_cls = ipaddress.IPv4Address if network.version == 4 else ipaddress.IPv6Address
        for lower, upper_exclusive in zip(ordered, ordered[1:], strict=False):
            if lower >= upper_exclusive:
                continue
            for subnet in ipaddress.summarize_address_range(
                addr_cls(lower), addr_cls(upper_exclusive - 1)
            ):
                matches = tuple(
                    entry for entry in family_entries if subnet.subnet_of(entry.network)
                )
                error = None
                if not matches:
                    error = "ZONE_UNRESOLVED"
                elif len(matches) > 1:
                    error = "ZONE_CONFLICT"
                segments.append(CatalogSegment(subnet, address, matches, error))
        return segments


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing policy file: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"policy file must contain a YAML object: {path}")
    return data


def _required(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"network field '{key}' is required")
    return value
