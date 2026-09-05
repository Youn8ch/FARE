"""V4-P6: the post-decision analysis stage (serial, non-authoritative).

Contains the capabilities that must never change a business decision: the
request findings shadow and the explanation. Fixed serial order (D4):
request findings shadow -> explanation; both consume only the formal reduce
output and only observe / explain.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.schemas import (
    EvaluationItem,
    EvaluationRequest,
    RequestFindingsAnalysis,
)
from app.services.evaluation_types import EvaluationItemContext
from app.services.explanation_guard import (
    ExplanationGuardError,
    guard_explanation_output,
)
from app.services.llm_client import (
    LlmClientProtocol,
    LlmDependencyError,
    LlmRequestFindingsClientProtocol,
)
from app.services.output_guard import (
    RequestFindingGuardError,
    guard_request_findings,
)
from app.services.rule_loader import PolicyBundle
from app.services.stage_metrics import record_llm_stage
from app.services.stages.semantic_stage import evidence_sources


async def run(
    *,
    llm_client: LlmClientProtocol,
    policies: PolicyBundle,
    request: EvaluationRequest,
    items: list[EvaluationItem],
    contexts: list[EvaluationItemContext],
    semantic_succeeded: bool,
    semantic_payload_items: list[dict[str, Any]],
    request_findings_mode: str,
    model_raw: dict[str, Any],
    exceptions: list[str],
) -> tuple[list[EvaluationItem], RequestFindingsAnalysis | None]:
    request_findings = await _request_findings_shadow(
        llm_client=llm_client,
        mode=request_findings_mode,
        request_id=request.request_id,
        contexts=contexts,
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
    return items, request_findings


async def _request_findings_shadow(
    *,
    llm_client: LlmClientProtocol,
    mode: str,
    request_id: str,
    contexts: list[EvaluationItemContext],
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
                evidence_sources=evidence_sources(contexts),
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
