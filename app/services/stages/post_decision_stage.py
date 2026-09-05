"""V4-P6: the post-decision analysis stage (serial, non-authoritative).

Contains the three capabilities that must never change a business decision:
explanation, the ACL candidate shadow, and the request findings shadow.
Fixed serial order (D4): acl candidate shadow -> request findings shadow ->
explanation; all three consume only the formal reduce output and only
explain / observe.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.schemas import (
    AclCandidateAnalysis,
    EvaluationItem,
    EvaluationRequest,
    RequestFindingsAnalysis,
)
from app.services.acl_candidate_merge import merge_acl_candidate
from app.services.evaluation_types import EvaluationItemContext
from app.services.explanation_guard import (
    ExplanationGuardError,
    guard_explanation_output,
)
from app.services.llm_client import (
    LlmAclCandidateClientProtocol,
    LlmClientProtocol,
    LlmDependencyError,
    LlmRequestFindingsClientProtocol,
    guard_acl_candidates,
)
from app.services.output_guard import (
    RequestFindingGuardError,
    guard_request_findings,
)
from app.services.rule_loader import PolicyBundle
from app.services.stage_metrics import record_llm_stage
from app.services.stages.acl_stage import AclCompatOutcome
from app.services.stages.semantic_stage import evidence_sources


async def run(
    *,
    llm_client: LlmClientProtocol,
    policies: PolicyBundle,
    request: EvaluationRequest,
    items: list[EvaluationItem],
    contexts: list[EvaluationItemContext],
    acl_outcomes: list[AclCompatOutcome],
    extra_evidence: dict[str, dict[str, str]],
    semantic_succeeded: bool,
    semantic_payload_items: list[dict[str, Any]],
    acl_candidate_mode: str,
    request_findings_mode: str,
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> tuple[list[EvaluationItem], AclCandidateAnalysis | None, RequestFindingsAnalysis | None]:
    acl_candidate_analysis = await _acl_candidates_shadow(
        llm_client=llm_client,
        mode=acl_candidate_mode,
        request_id=request.request_id,
        acl_outcomes=acl_outcomes,
        model_raw=model_raw,
        exceptions=exceptions,
    )
    request_findings = await _request_findings_shadow(
        llm_client=llm_client,
        mode=request_findings_mode,
        request_id=request.request_id,
        contexts=contexts,
        extra_evidence=extra_evidence,
        semantic_payload_items=semantic_payload_items,
        model_raw=model_raw,
        exceptions=exceptions,
    )
    items = await _explanation(
        llm_client=llm_client,
        policies=policies,
        request_id=request.request_id,
        items=items,
        semantic_succeeded=semantic_succeeded,
        model_raw=model_raw,
        exceptions=exceptions,
    )
    return items, acl_candidate_analysis, request_findings


async def _acl_candidates_shadow(
    *,
    llm_client: LlmClientProtocol,
    mode: str,
    request_id: str,
    acl_outcomes: list[AclCompatOutcome],
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> AclCandidateAnalysis | None:
    acl_candidate_analysis: AclCandidateAnalysis | None = None
    candidate_started = perf_counter()
    candidate_status = "off"
    candidate_error: Exception | None = None
    if mode == "shadow":
        candidate_status = "passed"
        candidate_records = list(acl_outcomes)
        candidate_inputs = [
            {
                "item_id": outcome.item_id,
                "analysis": outcome.raw_analysis,
                "config": outcome.raw_config,
            }
            for outcome in candidate_records
        ]
        if candidate_inputs:
            model_raw["acl_candidates_input"] = candidate_inputs
            try:
                if not isinstance(
                    llm_client, LlmAclCandidateClientProtocol
                ):
                    raise LlmDependencyError(
                        "LLM client does not support ACL candidate extraction"
                    )
                untrusted_candidates = await llm_client.extract_acl_facts(
                    candidate_inputs,
                    request_id=request_id,
                )
                candidates = guard_acl_candidates(
                    untrusted_candidates, candidate_inputs
                )
                expected_ids = {outcome.item_id for outcome in candidate_records}
                if set(candidates) != expected_ids:
                    raise LlmDependencyError(
                        "LLM ACL candidate item set does not match shadow input"
                    )
                acl_candidate_analysis = AclCandidateAnalysis(
                    items=[
                        merge_acl_candidate(
                            item_id=outcome.item_id,
                            deterministic=outcome.facts,
                            llm_candidate=candidates[outcome.item_id],
                        )
                        for outcome in candidate_records
                    ]
                )
                model_raw["acl_candidates"] = acl_candidate_analysis.model_dump(
                    mode="json"
                )
            except LlmDependencyError as exc:
                candidate_status = "rejected"
                candidate_error = exc
                detail = str(exc)
                public_detail = "ACL candidate shadow output was rejected"
                exceptions.append(f"LLM ACL candidates: {detail}")
                acl_candidate_analysis = AclCandidateAnalysis(
                    items=[
                        merge_acl_candidate(
                            item_id=outcome.item_id,
                            deterministic=outcome.facts,
                            rejection_reason=public_detail,
                        )
                        for outcome in candidate_records
                    ]
                )
                model_raw["acl_candidates"] = {
                    "error": detail,
                    "result": acl_candidate_analysis.model_dump(mode="json"),
                }
        else:
            acl_candidate_analysis = AclCandidateAnalysis()
            model_raw["acl_candidates"] = acl_candidate_analysis.model_dump(
                mode="json"
            )
    record_llm_stage(
        model_raw,
        "acl_candidates",
        candidate_started,
        candidate_status,
        candidate_error,
        llm_client,
    )
    return acl_candidate_analysis


async def _request_findings_shadow(
    *,
    llm_client: LlmClientProtocol,
    mode: str,
    request_id: str,
    contexts: list[EvaluationItemContext],
    extra_evidence: dict[str, dict[str, str]],
    semantic_payload_items: list[dict[str, Any]],
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> RequestFindingsAnalysis | None:
    request_findings: RequestFindingsAnalysis | None = None
    findings_started = perf_counter()
    findings_status = "off"
    findings_error: Exception | None = None
    if mode == "shadow":
        findings_status = "passed"
        finding_inputs = list(semantic_payload_items)
        item_ids = [context.item_id for context in contexts]
        model_raw["request_findings_input"] = {
            "request_id": request_id,
            "items": finding_inputs,
        }
        try:
            if not isinstance(
                llm_client, LlmRequestFindingsClientProtocol
            ):
                raise LlmDependencyError(
                    "LLM client does not support request findings analysis"
                )
            untrusted_findings = await llm_client.analyze_request_findings(
                finding_inputs,
                request_id=request_id,
            )
            guarded_findings = guard_request_findings(
                untrusted_findings,
                evidence_sources=evidence_sources(contexts, extra_evidence),
            )
            request_findings = RequestFindingsAnalysis(
                status="completed",
                analyzed_item_ids=guarded_findings.analyzed_item_ids,
                findings=guarded_findings.findings,
            )
            model_raw["request_findings"] = request_findings.model_dump(
                mode="json"
            )
            model_raw["request_findings_stats"] = {
                "status": "completed",
                "finding_count": len(guarded_findings.findings),
                "affected_item_count": len(
                    {
                        item_id
                        for finding in guarded_findings.findings
                        for item_id in finding.affected_item_ids
                    }
                ),
            }
        except (LlmDependencyError, RequestFindingGuardError) as exc:
            findings_status = "rejected"
            findings_error = exc
            detail = str(exc)
            public_detail = "Request findings shadow output was rejected"
            exceptions.append(f"LLM request findings: {detail}")
            request_findings = RequestFindingsAnalysis(
                status="rejected",
                analyzed_item_ids=item_ids,
                rejection_reason=public_detail,
            )
            model_raw["request_findings"] = {
                "error": detail,
                "result": request_findings.model_dump(mode="json"),
            }
            model_raw["request_findings_stats"] = {
                "status": "rejected",
                "finding_count": 0,
                "affected_item_count": 0,
            }
    record_llm_stage(
        model_raw,
        "request_findings",
        findings_started,
        findings_status,
        findings_error,
        llm_client,
    )
    return request_findings


async def _explanation(
    *,
    llm_client: LlmClientProtocol,
    policies: PolicyBundle,
    request_id: str,
    items: list[EvaluationItem],
    semantic_succeeded: bool,
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> list[EvaluationItem]:
    explanation_started = perf_counter()
    explanation_status = "skipped"
    explanation_error: Exception | None = None
    if semantic_succeeded:
        explanation_status = "passed"
        try:
            explanation_payload = {
                "request_id": request_id,
                "policy_version": policies.version,
                "items": [item.model_dump(mode="json") for item in items],
            }
            model_raw["explanation_input"] = explanation_payload
            explanation, explanation_raw = await llm_client.explain(
                explanation_payload
            )
            model_raw["explanation"] = explanation_raw
            explained = guard_explanation_output(
                explanation,
                items=items,
                valid_rule_ids=policies.rule_ids,
            )
            items = [
                item.model_copy(
                    update={
                        "llm_explanation": explained[item.item_id].explanation,
                        "llm_recommendation": explained[item.item_id].recommendation,
                        "explanation_source": "llm",
                    }
                )
                for item in items
            ]
        except (LlmDependencyError, ExplanationGuardError) as exc:
            explanation_status = "rejected"
            explanation_error = exc
            exceptions.append(f"LLM explanation: {exc}")
            model_raw["explanation"] = {"error": str(exc), "template_fallback": True}
    record_llm_stage(
        model_raw,
        "explanation",
        explanation_started,
        explanation_status,
        explanation_error,
        llm_client,
    )
    return items
