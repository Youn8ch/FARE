from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.schemas import (
    GuardResult,
    LlmRequestFindingsResponse,
    LlmSemanticResponse,
    NetworkSemanticClaim,
    PolicyGap,
    SemanticAnalysis,
    SemanticClaim,
    SemanticContradiction,
)

AUTHORITATIVE_FIELDS = {
    "source_zone",
    "destination_zone",
    "source_environment",
    "destination_environment",
    "source_object_type",
    "destination_object_type",
}


class SemanticGuardError(ValueError):
    pass


class RequestFindingGuardError(ValueError):
    pass


def guard_request_findings(
    finding_output: LlmRequestFindingsResponse | Mapping[str, Any],
    *,
    evidence_sources: dict[str, dict[str, str]],
) -> LlmRequestFindingsResponse:
    if isinstance(finding_output, LlmRequestFindingsResponse):
        parsed = finding_output
    elif isinstance(finding_output, Mapping):
        try:
            parsed = LlmRequestFindingsResponse.model_validate(finding_output)
        except ValidationError as exc:
            raise RequestFindingGuardError(
                "request findings failed schema validation"
            ) from exc
    else:
        raise RequestFindingGuardError("request findings must be a structured object")

    expected_item_ids = set(evidence_sources)
    analyzed_item_ids = parsed.analyzed_item_ids
    if (
        len(analyzed_item_ids) != len(set(analyzed_item_ids))
        or set(analyzed_item_ids) != expected_item_ids
    ):
        raise RequestFindingGuardError(
            "request findings item coverage is incomplete, duplicated, or unknown"
        )

    finding_ids: set[str] = set()
    for finding in parsed.findings:
        if not finding.finding_id.strip() or finding.finding_id in finding_ids:
            raise RequestFindingGuardError(
                "request finding IDs must be non-blank and unique"
            )
        finding_ids.add(finding.finding_id)
        affected_item_ids = finding.affected_item_ids
        if len(affected_item_ids) != len(set(affected_item_ids)):
            raise RequestFindingGuardError(
                "request finding affected items must be unique"
            )
        affected = set(affected_item_ids)
        if not affected or not affected.issubset(expected_item_ids):
            raise RequestFindingGuardError(
                "request finding references an empty or unknown affected item set"
            )

        evidenced_items: set[str] = set()
        for evidence in finding.evidence:
            if evidence.item_id not in affected:
                raise RequestFindingGuardError(
                    "request finding evidence references an unaffected item"
                )
            source_text = evidence_sources[evidence.item_id].get(evidence.source)
            if source_text is None:
                raise RequestFindingGuardError(
                    "request finding evidence references an unknown source"
                )
            if evidence.quote not in source_text:
                raise RequestFindingGuardError(
                    "request finding evidence cannot be located in its item source"
                )
            evidenced_items.add(evidence.item_id)
        if evidenced_items != affected:
            raise RequestFindingGuardError(
                "every affected request finding item requires bound evidence"
            )
    return parsed


def guard_semantic_output(
    output: LlmSemanticResponse,
    *,
    evidence_sources: dict[str, dict[str, str]],
    authoritative_facts: dict[str, dict[str, str]],
    valid_rule_ids: set[str],
    network_facts: dict[
        str, dict[str, dict[str, dict[str, str | None]]]
    ]
    | None = None,
) -> SemanticAnalysis:
    expected = set(evidence_sources)
    actual = output.analyzed_item_ids
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise SemanticGuardError("semantic analysis item coverage is incomplete or duplicated")
    if not set(output.candidate_rule_ids).issubset(valid_rule_ids):
        raise SemanticGuardError("semantic analysis referenced a nonexistent rule")

    guard_results = [
        GuardResult(
            code="ITEM_COVERAGE",
            status="passed",
            detail="模型批量输出完整覆盖全部访问组合。",
        ),
        GuardResult(
            code="RULE_ID_WHITELIST",
            status="passed",
            detail="候选规则编号均来自当前已发布规则包。",
        ),
    ]
    claims: list[SemanticClaim] = []
    claim_ids: set[str] = set()
    for claim in output.claims:
        _register_unique_id(claim.claim_id, claim_ids, "semantic claim")
        if claim.scope not in expected:
            raise SemanticGuardError("semantic claim references an unknown item")
        if claim.source not in evidence_sources[claim.scope]:
            raise SemanticGuardError("semantic claim references an unknown source")
        source_text = evidence_sources[claim.scope][claim.source]
        if not claim.evidence or claim.evidence not in source_text:
            raise SemanticGuardError("semantic claim evidence cannot be located in its source")
        status = "candidate"
        claim_type = claim.claim_type.lower()
        if claim_type in AUTHORITATIVE_FIELDS:
            authoritative = authoritative_facts.get(claim.scope, {}).get(claim_type)
            if authoritative is None:
                status = "candidate"
            elif authoritative.strip().casefold() == claim.value.strip().casefold():
                status = "verified"
            else:
                status = "conflict"
        claims.append(
            SemanticClaim(**claim.model_dump(mode="python"), status=status)
        )
        guard_results.append(
            GuardResult(
                code="CLAIM_EVIDENCE",
                status="passed",
                detail="声明证据已在指定原文字段中定位。",
                related_id=claim.claim_id,
            )
        )

    contradictions: list[SemanticContradiction] = []
    contradiction_ids: set[str] = set()
    for contradiction in output.contradictions:
        _register_unique_id(
            contradiction.contradiction_id,
            contradiction_ids,
            "semantic contradiction",
        )
        _validate_scoped_evidence(
            contradiction.scope,
            contradiction.evidence,
            evidence_sources,
            require_distinct=True,
        )
        contradictions.append(
            SemanticContradiction(
                **contradiction.model_dump(mode="python"), status="verified"
            )
        )

    gaps: list[PolicyGap] = []
    gap_ids: set[str] = set()
    for gap in output.policy_gaps:
        _register_unique_id(gap.gap_id, gap_ids, "semantic policy gap")
        _validate_scoped_evidence(gap.scope, gap.evidence, evidence_sources)
        gaps.append(PolicyGap(**gap.model_dump(mode="python"), status="verified"))

    network_claims: list[NetworkSemanticClaim] = []
    for claim in output.network_claims:
        _register_unique_id(claim.claim_id, claim_ids, "network semantic claim")
        if claim.scope not in expected:
            raise SemanticGuardError("network claim references an unknown item")
        if network_facts is None:
            raise SemanticGuardError("network fact bindings are unavailable")
        values_match = True
        for reference in claim.fact_references:
            if reference.item_id != claim.scope:
                raise SemanticGuardError("network claim crosses item boundaries")
            role_facts = network_facts.get(claim.scope, {}).get(reference.role, {})
            fact = role_facts.get(reference.fact_id)
            if fact is None:
                raise SemanticGuardError(
                    "network claim references an unknown or cross-role fact"
                )
            if reference.field not in fact:
                raise SemanticGuardError("network claim references an unknown fact field")
            if fact[reference.field] != reference.value:
                values_match = False
        if claim.source == "network_plan_fact":
            evidence_valid = True
        else:
            source_text = evidence_sources[claim.scope].get(claim.source)
            if source_text is None:
                raise SemanticGuardError("network claim references an unknown source")
            evidence_valid = bool(claim.evidence and claim.evidence in source_text)
        if not values_match or not evidence_valid:
            status = "rejected"
        elif claim.claim_type == "request_fact_conflict":
            status = "conflict"
        elif claim.claim_type == "network_fact_reference":
            status = "verified"
        else:
            status = "candidate"
        network_claims.append(
            NetworkSemanticClaim(
                **claim.model_dump(mode="python"),
                status=status,
            )
        )
        guard_results.append(
            GuardResult(
                code="NETWORK_FACT_REFERENCE",
                status="rejected" if status == "rejected" else "passed",
                detail=(
                    "网络事实结构化引用已通过绑定校验。"
                    if status != "rejected"
                    else "网络事实引用值或证据未通过绑定校验。"
                ),
                related_id=claim.claim_id,
            )
        )

    return SemanticAnalysis(
        analyzed_item_ids=actual,
        claims=claims,
        contradictions=contradictions,
        candidate_rule_ids=sorted(set(output.candidate_rule_ids)),
        policy_gaps=gaps,
        questions_for_requester=output.questions_for_requester,
        recommendations=output.recommendations,
        guard_results=guard_results,
        network_claims=network_claims,
    )


def failed_semantic_analysis(item_ids: list[str], detail: str) -> SemanticAnalysis:
    return SemanticAnalysis(
        analyzed_item_ids=item_ids,
        guard_results=[
            GuardResult(
                code="SEMANTIC_ANALYSIS_FAILURE",
                status="rejected",
                detail=detail,
            )
        ],
    )


def _validate_scoped_evidence(
    scope: str,
    evidence: list[str],
    evidence_sources: dict[str, dict[str, str]],
    *,
    require_distinct: bool = False,
) -> None:
    if scope not in evidence_sources:
        raise SemanticGuardError("semantic finding references an unknown item")
    normalized = [value.strip().casefold() for value in evidence]
    if any(not value for value in normalized):
        raise SemanticGuardError("semantic finding evidence cannot be empty")
    if require_distinct and len(normalized) != len(set(normalized)):
        raise SemanticGuardError("semantic contradiction requires distinct evidence")
    combined = "\n".join(evidence_sources[scope].values())
    if any(value not in combined for value in evidence):
        raise SemanticGuardError("semantic finding evidence cannot be located")


def _register_unique_id(identifier: str, seen: set[str], label: str) -> None:
    if not identifier.strip():
        raise SemanticGuardError(f"{label} id cannot be empty")
    if identifier in seen:
        raise SemanticGuardError(f"duplicate {label} id")
    seen.add(identifier)
