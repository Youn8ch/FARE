from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.services.splitter import AccessCombination

ALLOWED_CATEGORIES = {
    "zone_relation",
    "object_relation",
    "special_port",
    "least_privilege",
}
ALLOWED_REASON_TYPES = {
    "policy_violation",
    "fact_incomplete",
    "fact_conflict",
    "acl_no_path",
    "dependency_failure",
    "risk_uncertain",
}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    name: str
    category: str
    decision: str
    reason_type: str
    when: dict[str, Any]
    reason_template: str
    recommendation: str
    description: str
    semantic_keywords: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    remediation_template: str

    def semantic_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "semantic_keywords": list(self.semantic_keywords),
            "evidence_requirements": list(self.evidence_requirements),
            "remediation_template": self.remediation_template,
        }


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    version: str
    released_at: str
    rules: tuple[Rule, ...]

    @classmethod
    def load(cls, policy_dir: Path, catalog_version: str | None = None) -> PolicyBundle:
        manifest = _yaml(policy_dir / "manifest.yaml")
        version = _required(manifest, "version")
        if catalog_version is not None and version != catalog_version:
            raise ValueError("manifest and network catalog versions do not match")
        released_at = _required(manifest, "released_at")
        try:
            date.fromisoformat(released_at)
        except ValueError as exc:
            raise ValueError("manifest released_at must be ISO date") from exc
        if not manifest.get("sources") or not manifest.get("approvals"):
            raise ValueError("manifest sources and approvals are required")

        data = _yaml(policy_dir / "compliance_rules.yaml")
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("compliance_rules.yaml must contain rules")
        rules: list[Rule] = []
        ids: set[str] = set()
        for raw in raw_rules:
            if not isinstance(raw, dict):
                raise ValueError("each rule must be an object")
            rule_id = _required(raw, "id")
            if rule_id in ids:
                raise ValueError(f"duplicate rule id: {rule_id}")
            ids.add(rule_id)
            category = _required(raw, "category")
            reason_type = _required(raw, "reason_type")
            if category not in ALLOWED_CATEGORIES:
                raise ValueError(f"unsupported rule category: {category}")
            if reason_type not in ALLOWED_REASON_TYPES:
                raise ValueError(f"unsupported reason type: {reason_type}")
            if raw.get("decision") != "待定":
                raise ValueError(f"rule {rule_id} must be a rejection/pending rule")
            when = raw.get("when")
            if not isinstance(when, dict) or not when:
                raise ValueError(f"rule {rule_id} requires matching conditions")
            _validate_when(rule_id, category, when)
            semantic_keywords = raw.get("semantic_keywords", [])
            evidence_requirements = raw.get("evidence_requirements", [])
            if not isinstance(semantic_keywords, list) or not isinstance(
                evidence_requirements, list
            ):
                raise ValueError(
                    f"rule {rule_id} semantic_keywords and evidence_requirements must be lists"
                )
            rules.append(
                Rule(
                    id=rule_id,
                    name=_required(raw, "name"),
                    category=category,
                    decision="待定",
                    reason_type=reason_type,
                    when=when,
                    reason_template=_required(raw, "reason_template"),
                    recommendation=_required(raw, "recommendation"),
                    description=_required(raw, "description"),
                    semantic_keywords=tuple(str(value) for value in semantic_keywords),
                    evidence_requirements=tuple(
                        str(value) for value in evidence_requirements
                    ),
                    remediation_template=_required(raw, "remediation_template"),
                )
            )
        if "ACL-PATH-001" not in ids:
            raise ValueError("policy bundle must contain ACL-PATH-001")
        return cls(version=version, released_at=released_at, rules=tuple(rules))

    @property
    def rule_ids(self) -> set[str]:
        return {rule.id for rule in self.rules}

    @property
    def acl_no_path_rule(self) -> Rule:
        return next(rule for rule in self.rules if rule.id == "ACL-PATH-001")

    def match(self, combination: AccessCombination, total_combinations: int) -> list[Rule]:
        return [
            rule
            for rule in self.rules
            if rule.id != "ACL-PATH-001" and _matches(rule, combination, total_combinations)
        ]


def _matches(rule: Rule, item: AccessCombination, total_combinations: int) -> bool:
    when = rule.when
    source_segment = item.source
    destination_segment = item.destination
    source_fact = source_segment.primary_fact
    destination_fact = destination_segment.primary_fact
    if rule.category == "zone_relation":
        values = {
            "source_zone": (
                source_segment.zone
                if source_segment.zone is not None
                else (source_fact.area_id if source_fact else None)
            ),
            "destination_zone": (
                destination_segment.zone
                if destination_segment.zone is not None
                else (destination_fact.area_id if destination_fact else None)
            ),
            "source_area_id": source_fact.area_id if source_fact else None,
            "destination_area_id": destination_fact.area_id if destination_fact else None,
            "source_region_name": source_fact.region_name if source_fact else None,
            "destination_region_name": destination_fact.region_name if destination_fact else None,
            "source_platform_name": source_fact.platform_name if source_fact else None,
            "destination_platform_name": (
                destination_fact.platform_name if destination_fact else None
            ),
            "source_usage_code": source_fact.usage_code if source_fact else None,
            "destination_usage_code": destination_fact.usage_code if destination_fact else None,
        }
        return all(_eq(when, key, value) for key, value in values.items())
    if rule.category == "object_relation":
        checks = {
            "source_zone": (
                source_segment.zone
                if source_segment.zone is not None
                else (source_fact.area_id if source_fact else None)
            ),
            "destination_zone": (
                destination_segment.zone
                if destination_segment.zone is not None
                else (destination_fact.area_id if destination_fact else None)
            ),
            "source_area_id": source_fact.area_id if source_fact else None,
            "destination_area_id": destination_fact.area_id if destination_fact else None,
            "source_region_name": source_fact.region_name if source_fact else None,
            "destination_region_name": destination_fact.region_name if destination_fact else None,
            "source_platform_name": source_fact.platform_name if source_fact else None,
            "destination_platform_name": (
                destination_fact.platform_name if destination_fact else None
            ),
            "source_usage_code": source_fact.usage_code if source_fact else None,
            "destination_usage_code": destination_fact.usage_code if destination_fact else None,
            "source_environment": source_segment.environment,
            "destination_environment": destination_segment.environment,
            "source_object_type": source_segment.object_type,
            "destination_object_type": destination_segment.object_type,
        }
        if not all(_eq(when, key, value) for key, value in checks.items()):
            return False
        if "source_labels" in when and (
            source_segment.catalog_entry_id is None
            or not set(when["source_labels"]).issubset(source_segment.labels)
        ):
            return False
        if "destination_labels" in when and (
            destination_segment.catalog_entry_id is None
            or not set(when["destination_labels"]).issubset(destination_segment.labels)
        ):
            return False
        return True
    if rule.category == "special_port":
        if not _eq(when, "protocol", item.protocol):
            return False
        return any(item.port.start <= int(port) <= item.port.end for port in when.get("ports", []))
    if rule.category == "least_privilege":
        check = when.get("check")
        if check == "any_address":
            return (
                item.source.access_network is None or item.destination.access_network is None
            )
        if check == "prefix_too_broad":
            return _prefix_too_broad(
                item.source.access_network, when
            ) or _prefix_too_broad(item.destination.access_network, when)
        if check == "port_span":
            return item.port.end - item.port.start + 1 > int(when["max_ports"])
        if check == "combination_count":
            return total_combinations > int(when["max_combinations"])
    return False


def _prefix_too_broad(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None, when: dict[str, Any]
) -> bool:
    if network is None:
        return False
    threshold = int(when["min_ipv4_prefix"] if network.version == 4 else when["min_ipv6_prefix"])
    return network.prefixlen < threshold


def _eq(when: dict[str, Any], key: str, actual: str | None) -> bool:
    if key not in when:
        return True
    return actual is not None and str(when[key]).lower() == actual.lower()


_NETWORK_KEYS = {
    "source_zone",
    "destination_zone",
    "source_area_id",
    "destination_area_id",
    "source_region_name",
    "destination_region_name",
    "source_platform_name",
    "destination_platform_name",
    "source_usage_code",
    "destination_usage_code",
}
_OBJECT_KEYS = _NETWORK_KEYS | {
    "source_environment",
    "destination_environment",
    "source_object_type",
    "destination_object_type",
    "source_labels",
    "destination_labels",
}


def _validate_when(rule_id: str, category: str, when: dict[str, Any]) -> None:
    if category == "zone_relation":
        allowed = _NETWORK_KEYS
    elif category == "object_relation":
        allowed = _OBJECT_KEYS
    elif category == "special_port":
        allowed = {"protocol", "ports"}
        if not {"protocol", "ports"}.issubset(when):
            raise ValueError(f"rule {rule_id} special_port requires protocol and ports")
    elif category == "least_privilege":
        check = when.get("check")
        allowed_by_check = {
            "any_address": {"check"},
            "prefix_too_broad": {"check", "min_ipv4_prefix", "min_ipv6_prefix"},
            "port_span": {"check", "max_ports"},
            "combination_count": {"check", "max_combinations"},
        }
        if check is None and when == {"explicit_no_path": True}:
            allowed = {"explicit_no_path"}
        elif check not in allowed_by_check:
            raise ValueError(f"rule {rule_id} has an unsupported least_privilege check")
        else:
            allowed = allowed_by_check[str(check)]
            if allowed - set(when):
                raise ValueError(f"rule {rule_id} is missing required conditions")
    else:
        raise ValueError(f"unsupported rule category: {category}")
    unknown = set(when) - allowed
    if unknown:
        raise ValueError(
            f"rule {rule_id} has unsupported matching conditions: {sorted(unknown)}"
        )


def _yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing policy file: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"policy file must be an object: {path}")
    return data


def _required(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"policy field '{key}' is required")
    return value
