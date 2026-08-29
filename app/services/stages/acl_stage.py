"""V4-P5b: the ACL stage.

Gating consumes the RuleStage result — the rule engine is never invoked here
(V4 invariant 4.2.2). Network-blocked items and deterministic-pending items
(skip mode) are skipped without an ACL call. The stage owns the ACL findings
(dependency / no-path / ambiguous / port mismatch / unresolved firewall,
incl. ACL-PATH-001) and the verification status.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.schemas import AclRawResponse, ExtractedFacts
from app.services.acl_client import AclClient, AclDependencyError
from app.services.acl_extract import AclFactExtractor
from app.services.evaluation_types import RuleStageResult
from app.services.finding_factory import (
    Finding,
    acl_findings,
    network_fact_blocks_acl,
)
from app.services.rule_loader import Rule
from app.services.splitter import AccessCombination


@dataclass(slots=True)
class AclRecord:
    item_id: str
    combination: AccessCombination
    raw: AclRawResponse | None
    facts: ExtractedFacts
    dependency_error: str | None
    verification_status: str
    acl_findings: tuple[Finding, ...] = ()


@dataclass(slots=True)
class AclStageOutput:
    records: list[AclRecord]
    raw_records: list[dict[str, Any]]
    exceptions: list[str]


def _skipped_acl_record(
    item_id: str, combination: AccessCombination
) -> AclRecord:
    return AclRecord(
        item_id=item_id,
        combination=combination,
        raw=None,
        facts=ExtractedFacts(),
        dependency_error=None,
        verification_status="skipped",
        acl_findings=(),
    )


def acl_verification_status(
    combination: AccessCombination,
    facts: ExtractedFacts,
    dependency_error: str | None,
) -> str:
    if network_fact_blocks_acl(combination):
        return "skipped"
    if facts.explicit_no_path or facts.ambiguous:
        return "review_required"
    if facts.observed_ports and not any(
        combination.port.start <= port <= combination.port.end
        for port in facts.observed_ports
    ):
        return "review_required"
    if dependency_error or not facts.firewalls:
        return "unverified"
    return "verified"


async def run(
    *,
    acl_client: AclClient,
    extractor: AclFactExtractor,
    decision_mode: str,
    pending_mode: str,
    no_path_rule: Rule,
    max_concurrency: int,
    rule_results: tuple[RuleStageResult, ...],
) -> AclStageOutput:
    semaphore = asyncio.Semaphore(max_concurrency)
    analyzed = await asyncio.gather(
        *(
            _analyze_item(
                rule_result=rule_result,
                acl_client=acl_client,
                extractor=extractor,
                decision_mode=decision_mode,
                pending_mode=pending_mode,
                no_path_rule=no_path_rule,
                semaphore=semaphore,
            )
            for rule_result in rule_results
        )
    )
    records = [record for record, _ in analyzed]
    return AclStageOutput(
        records=records,
        raw_records=[raw_record for _, raw_record in analyzed],
        exceptions=[
            f"ACL dependency: {record.dependency_error}"
            for record in records
            if record.dependency_error
        ],
    )


async def _analyze_item(
    *,
    rule_result: RuleStageResult,
    acl_client: AclClient,
    extractor: AclFactExtractor,
    decision_mode: str,
    pending_mode: str,
    no_path_rule: Rule,
    semaphore: asyncio.Semaphore,
) -> tuple[AclRecord, dict[str, Any]]:
    combination = rule_result.combination
    if network_fact_blocks_acl(combination):
        return (
            _skipped_acl_record(rule_result.item_id, combination),
            {
                "item_id": rule_result.item_id,
                "skipped_due_to_network_fact": True,
            },
        )

    if pending_mode == "skip" and rule_result.matched_rules:
        return (
            _skipped_acl_record(rule_result.item_id, combination),
            {
                "item_id": rule_result.item_id,
                "skipped": True,
                "skip_reason": "deterministic_pending_rule",
                "matched_rule_ids": [
                    rule.id for rule in rule_result.matched_rules
                ],
            },
        )

    raw: AclRawResponse | None = None
    dependency_error: str | None = None
    try:
        async with semaphore:
            raw = await acl_client.analyze(combination)
        facts = extractor.extract(raw)
        raw_record = {
            "item_id": rule_result.item_id,
            "response": raw.model_dump(mode="json"),
        }
        verification = acl_verification_status(combination, facts, None)
    except AclDependencyError as exc:
        dependency_error = str(exc)
        facts = ExtractedFacts()
        raw_record = {"item_id": rule_result.item_id, "error": dependency_error}
        verification = "unverified"
    return (
        AclRecord(
            item_id=rule_result.item_id,
            combination=combination,
            raw=raw,
            facts=facts,
            dependency_error=dependency_error,
            verification_status=verification,
            acl_findings=acl_findings(
                combination,
                facts,
                dependency_error,
                decision_mode=decision_mode,
                no_path_rule=no_path_rule,
            ),
        ),
        raw_record,
    )
