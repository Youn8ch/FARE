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
from app.services.evaluation_types import EvaluationItemContext
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
    contexts: list[EvaluationItemContext],
    extra_evidence: dict[str, dict[str, str]],
    model_raw: dict[str, Any],
    exceptions: list[str],
    deterministic_candidates: set[str],
) -> SemanticStageOutput:
    payload = build_payload(request, contexts, policies, extra_evidence)
    model_raw["semantic_input"] = payload
    started = perf_counter()
    error: Exception | None = None
    findings: dict[str, list[Finding]] = {
        context.item_id: [] for context in contexts
    }
    review_ids: dict[str, list[str]] = {
        context.item_id: [] for context in contexts
    }
    question_ids: dict[str, list[str]] = {
        context.item_id: [] for context in contexts
    }
    observation_ids: dict[str, list[str]] = {
        context.item_id: [] for context in contexts
    }
    try:
        raw_semantic, semantic_raw = await llm_client.analyze(payload)
        model_raw["semantic"] = semantic_raw
        semantic = guard_semantic_output(
            raw_semantic,
            evidence_sources=evidence_sources(contexts, extra_evidence),
            authoritative_facts=authoritative_facts(contexts),
            valid_rule_ids=policies.rule_ids,
            network_facts=network_fact_bindings(contexts),
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
                [context.item_id for context in contexts],
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
            [context.item_id for context in contexts], detail
        )
        failure_finding = Finding(
            code="LLM_SEMANTIC_ANALYSIS_FAILURE",
            source="semantic",
            reason_type="dependency_failure",
        )
        findings = {
            context.item_id: [failure_finding] for context in contexts
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
    contexts: list[EvaluationItemContext],
    policies: PolicyBundle,
    extra_evidence: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "request_description": request.request_description,
        "items": [
            {
                "item_id": context.item_id,
                "access": {
                    "source": context.combination.source_text,
                    "destination": context.combination.destination_text,
                    "protocol": context.combination.protocol,
                    "port": context.combination.port.model_dump(mode="json"),
                },
                "source_description": context.combination.source_description,
                "destination_description": context.combination.destination_description,
                "request_description": context.combination.request_description,
                "authoritative_facts": _authoritative_fact(context),
                "source_network_facts": network_facts_of(context.combination.source),
                "destination_network_facts": network_facts_of(
                    context.combination.destination
                ),
                "network_plan_status": {
                    "source": segment_status(context.combination.source),
                    "destination": segment_status(context.combination.destination),
                },
                "acl_analysis": extra_evidence.get(context.item_id, {}).get(
                    "acl_analysis", ""
                ),
                "acl_config": extra_evidence.get(context.item_id, {}).get(
                    "acl_config", ""
                ),
            }
            for context in contexts
        ],
        "rules": [rule.semantic_summary() for rule in policies.rules],
    }


def evidence_sources(
    contexts: list[EvaluationItemContext],
    extra_evidence: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    return {
        context.item_id: {
            "request_description": context.combination.request_description,
            "source_description": context.combination.source_description,
            "destination_description": context.combination.destination_description,
            **extra_evidence.get(context.item_id, {}),
        }
        for context in contexts
    }


def _authoritative_fact(context: EvaluationItemContext) -> dict[str, str]:
    source = context.combination.source
    destination = context.combination.destination
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


def authoritative_facts(
    contexts: list[EvaluationItemContext],
) -> dict[str, dict[str, str]]:
    return {context.item_id: _authoritative_fact(context) for context in contexts}


def network_fact_bindings(
    contexts: list[EvaluationItemContext],
) -> dict[str, dict[str, dict[str, dict[str, str | None]]]]:
    return {
        context.item_id: {
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
                ("source", context.combination.source),
                ("destination", context.combination.destination),
            )
        }
        for context in contexts
    }
