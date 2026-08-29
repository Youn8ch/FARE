"""V4-P5b: the semantic stage.

Runs the batch semantic analysis and produces semantic findings / question /
observation buckets. This stage never mutates items or decisions; the reduce
stage owns every decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.schemas import EvaluationRequest, SemanticAnalysis
from app.services.decision_reducer import Finding
from app.services.finding_factory import (
    apply_configured_semantic_effects,
    network_facts_of,
    segment_status,
    semantic_stage_findings,
)
from app.services.llm_client import LlmClientProtocol, LlmDependencyError
from app.services.output_guard import (
    SemanticGuardError,
    failed_semantic_analysis,
    guard_semantic_output,
)
from app.services.rule_loader import PolicyBundle
from app.services.stage_metrics import record_llm_stage
from app.services.stages.acl_stage import AclRecord


@dataclass(slots=True)
class SemanticStageOutput:
    semantic: SemanticAnalysis
    succeeded: bool
    findings: dict[str, list[Finding]]
    review_ids: dict[str, list[str]]
    question_ids: dict[str, list[str]]
    observation_ids: dict[str, list[str]]
    # The semantic input items are re-used verbatim by the request-findings
    # shadow stage (post-decision).
    payload_items: list[dict[str, Any]] = field(default_factory=list)


async def run(
    *,
    llm_client: LlmClientProtocol,
    policies: PolicyBundle,
    semantic_effects: dict[str, str],
    request: EvaluationRequest,
    records: list[AclRecord],
    model_raw: dict[str, Any],
    exceptions: list[str],
    deterministic_candidates: set[str],
) -> SemanticStageOutput:
    payload = build_payload(request, records, policies)
    model_raw["semantic_input"] = payload
    started = perf_counter()
    error: Exception | None = None
    findings: dict[str, list[Finding]] = {
        record.item_id: [] for record in records
    }
    review_ids: dict[str, list[str]] = {
        record.item_id: [] for record in records
    }
    question_ids: dict[str, list[str]] = {
        record.item_id: [] for record in records
    }
    observation_ids: dict[str, list[str]] = {
        record.item_id: [] for record in records
    }
    try:
        raw_semantic, semantic_raw = await llm_client.analyze(payload)
        model_raw["semantic"] = semantic_raw
        semantic = guard_semantic_output(
            raw_semantic,
            evidence_sources=evidence_sources(records),
            authoritative_facts=authoritative_facts(records),
            valid_rule_ids=policies.rule_ids,
            network_facts=network_fact_bindings(records),
        )
        semantic = semantic.model_copy(
            update={
                "candidate_rule_ids": sorted(
                    deterministic_candidates
                    | set(semantic.candidate_rule_ids)
                )
            }
        )
        semantic = apply_configured_semantic_effects(semantic, semantic_effects)
        findings, review_ids, question_ids, observation_ids = (
            semantic_stage_findings(
                [record.item_id for record in records],
                semantic,
                fact_conflict_effect=semantic_effects.get(
                    "fact_conflict", "review_required"
                ),
            )
        )
        succeeded = True
    except (LlmDependencyError, SemanticGuardError) as exc:
        error = exc
        detail = str(exc)
        exceptions.append(f"LLM semantic analysis: {detail}")
        model_raw.setdefault("semantic", {"error": detail})
        semantic = failed_semantic_analysis(
            [record.item_id for record in records], detail
        )
        failure_finding = Finding(
            code="LLM_SEMANTIC_ANALYSIS_FAILURE",
            source="semantic",
            reason_type="dependency_failure",
        )
        findings = {
            record.item_id: [failure_finding] for record in records
        }
        succeeded = False
    record_llm_stage(
        model_raw,
        "semantic",
        started,
        "passed" if succeeded else "rejected",
        error,
        llm_client,
    )
    return SemanticStageOutput(
        semantic=semantic,
        succeeded=succeeded,
        findings=findings,
        review_ids=review_ids,
        question_ids=question_ids,
        observation_ids=observation_ids,
        payload_items=list(payload["items"]),
    )


def build_payload(
    request: EvaluationRequest,
    records: list[AclRecord],
    policies: PolicyBundle,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "request_description": request.request_description,
        "items": [
            {
                "item_id": record.item_id,
                "access": {
                    "source": record.combination.source_text,
                    "destination": record.combination.destination_text,
                    "protocol": record.combination.protocol,
                    "port": record.combination.port.model_dump(mode="json"),
                },
                "source_description": record.combination.source_description,
                "destination_description": record.combination.destination_description,
                "request_description": record.combination.request_description,
                "authoritative_facts": _authoritative_fact(record),
                "source_network_facts": network_facts_of(record.combination.source),
                "destination_network_facts": network_facts_of(
                    record.combination.destination
                ),
                "network_plan_status": {
                    "source": segment_status(record.combination.source),
                    "destination": segment_status(record.combination.destination),
                },
                "acl_analysis": record.raw.analysis if record.raw else "",
                "acl_config": record.raw.config if record.raw else "",
            }
            for record in records
        ],
        "rules": [rule.semantic_summary() for rule in policies.rules],
    }


def evidence_sources(records: list[AclRecord]) -> dict[str, dict[str, str]]:
    return {
        record.item_id: {
            "request_description": record.combination.request_description,
            "source_description": record.combination.source_description,
            "destination_description": record.combination.destination_description,
            "acl_analysis": record.raw.analysis if record.raw else "",
            "acl_config": record.raw.config if record.raw else "",
        }
        for record in records
    }


def _authoritative_fact(record: AclRecord) -> dict[str, str]:
    source = record.combination.source
    destination = record.combination.destination
    facts: dict[str, str] = {}
    if source.zone is not None:
        facts.update(
            source_zone=source.zone,
            source_environment=source.environment or "",
            source_object_type=source.object_type or "",
        )
    if destination.zone is not None:
        facts.update(
            destination_zone=destination.zone,
            destination_environment=destination.environment or "",
            destination_object_type=destination.object_type or "",
        )
    source_fact = source.primary_fact
    destination_fact = destination.primary_fact
    if source_fact is not None:
        facts.setdefault("source_zone", source_fact.area_id)
    if destination_fact is not None:
        facts.setdefault("destination_zone", destination_fact.area_id)
    return facts


def authoritative_facts(records: list[AclRecord]) -> dict[str, dict[str, str]]:
    return {record.item_id: _authoritative_fact(record) for record in records}


def network_fact_bindings(
    records: list[AclRecord],
) -> dict[str, dict[str, dict[str, dict[str, str | None]]]]:
    return {
        record.item_id: {
            role: {
                fact.fact_id: {
                    field_name: getattr(fact, field_name)
                    for field_name in (
                        "area_id",
                        "area",
                        "region_name",
                        "platform_name",
                        "network",
                        "subnet",
                        "usage_code",
                        "description",
                    )
                }
                for fact in getattr(segment, "network_facts", ())
            }
            for role, segment in (
                ("source", record.combination.source),
                ("destination", record.combination.destination),
            )
        }
        for record in records
    }
