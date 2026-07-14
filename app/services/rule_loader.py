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
    def load(cls, policy_dir: Path, catalog_version: str) -> PolicyBundle:
        manifest = _yaml(policy_dir / "manifest.yaml")
        version = _required(manifest, "version")
        if version != catalog_version:
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
    source = item.source.entry
    destination = item.destination.entry
    if rule.category == "zone_relation":
        return bool(
            source
            and destination
            and _eq(when, "source_zone", source.zone)
            and _eq(when, "destination_zone", destination.zone)
        )
    if rule.category == "object_relation":
        if not source or not destination:
            return False
        checks = {
            "source_zone": source.zone,
            "destination_zone": destination.zone,
            "source_environment": source.environment,
            "destination_environment": destination.environment,
            "source_object_type": source.object_type,
            "destination_object_type": destination.object_type,
        }
        if not all(_eq(when, key, value) for key, value in checks.items()):
            return False
        if "source_labels" in when and not set(when["source_labels"]).issubset(source.labels):
            return False
        if "destination_labels" in when and not set(when["destination_labels"]).issubset(
            destination.labels
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
            return item.source.network is None or item.destination.network is None
        if check == "prefix_too_broad":
            return _prefix_too_broad(item.source.network, when) or _prefix_too_broad(
                item.destination.network, when
            )
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


def _eq(when: dict[str, Any], key: str, actual: str) -> bool:
    return key not in when or str(when[key]).lower() == actual.lower()


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
