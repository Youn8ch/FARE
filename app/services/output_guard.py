from __future__ import annotations

from app.schemas import (
    GuardResult,
    LlmSemanticResponse,
    PolicyGap,
    SemanticAnalysis,
    SemanticClaim,
    SemanticContradiction,
)

FORBIDDEN_FIELDS = {"decision", "approval", "route", "nat", "live_acl_status"}
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


def guard_semantic_output(
    output: LlmSemanticResponse,
    *,
    evidence_sources: dict[str, dict[str, str]],
    authoritative_facts: dict[str, dict[str, str]],
    valid_rule_ids: set[str],
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
        if claim.claim_id in claim_ids:
            raise SemanticGuardError("duplicate semantic claim id")
        claim_ids.add(claim.claim_id)
        if claim.scope not in expected:
            raise SemanticGuardError("semantic claim references an unknown item")
        if claim.field.lower() in FORBIDDEN_FIELDS:
            raise SemanticGuardError("semantic claim contains an out-of-scope field")
        source_text = evidence_sources[claim.scope].get(claim.source, "")
        if not claim.evidence or claim.evidence not in source_text:
            raise SemanticGuardError("semantic claim evidence cannot be located in its source")
        status = "candidate"
        if claim.field in AUTHORITATIVE_FIELDS:
            authoritative = authoritative_facts[claim.scope].get(claim.field)
            if authoritative is None:
                status = "candidate"
            elif authoritative.casefold() == claim.value.casefold():
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
    for contradiction in output.contradictions:
        _validate_scoped_evidence(
            contradiction.scope, contradiction.evidence, evidence_sources
        )
        contradictions.append(
            SemanticContradiction(
                **contradiction.model_dump(mode="python"), status="verified"
            )
        )

    gaps: list[PolicyGap] = []
    for gap in output.policy_gaps:
        _validate_scoped_evidence(gap.scope, gap.evidence, evidence_sources)
        gaps.append(PolicyGap(**gap.model_dump(mode="python"), status="verified"))

    return SemanticAnalysis(
        analyzed_item_ids=actual,
        claims=claims,
        contradictions=contradictions,
        candidate_rule_ids=sorted(set(output.candidate_rule_ids)),
        policy_gaps=gaps,
        questions_for_requester=output.questions_for_requester,
        recommendations=output.recommendations,
        guard_results=guard_results,
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
    scope: str, evidence: list[str], evidence_sources: dict[str, dict[str, str]]
) -> None:
    if scope not in evidence_sources:
        raise SemanticGuardError("semantic finding references an unknown item")
    combined = "\n".join(evidence_sources[scope].values())
    if any(not value or value not in combined for value in evidence):
        raise SemanticGuardError("semantic finding evidence cannot be located")
