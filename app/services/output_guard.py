from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.schemas import (
    GuardResult,
    LlmRequestFindingsResponse,
    LlmSemanticResponse,
    MissingInformation,
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
    claim_fields_by_evidence: dict[str, dict[tuple[str, str], set[str]]] = {
        item_id: {} for item_id in expected
    }
    for claim in claims:
        key = (claim.source, claim.evidence.strip().casefold())
        claim_fields_by_evidence[claim.scope].setdefault(key, set()).add(
            claim.claim_type
        )
    for contradiction in output.contradictions:
        _register_unique_id(
            contradiction.contradiction_id,
            contradiction_ids,
            "semantic contradiction",
        )
        _validate_structured_evidence(
            contradiction.scope,
            contradiction.evidence,
            evidence_sources,
            require_distinct=True,
        )
        supported_fields = set()
        for evidence in contradiction.evidence:
            supported_fields.update(
                claim_fields_by_evidence[contradiction.scope].get(
                    (evidence.source, evidence.quote.strip().casefold()), set()
                )
            )
        eligible = len(supported_fields) >= 2
        contradictions.append(
            SemanticContradiction(
                **contradiction.model_dump(mode="python"),
                status="verified" if eligible else "rejected",
            )
        )
        guard_results.append(
            GuardResult(
                code="CONTRADICTION_ELIGIBILITY",
                status="passed" if eligible else "rejected",
                detail=(
                    "矛盾证据分别绑定到至少两个受控声明字段。"
                    if eligible
                    else "矛盾证据未分别绑定到至少两个受控声明字段，仅保留审计。"
                ),
                related_id=contradiction.contradiction_id,
            )
        )

    gaps: list[PolicyGap] = []
    gap_ids: set[str] = set()
    claim_fields_by_scope: dict[str, set[str]] = {item_id: set() for item_id in expected}
    for claim in claims:
        claim_fields_by_scope[claim.scope].add(claim.claim_type)
    for gap in output.policy_gaps:
        _register_unique_id(gap.gap_id, gap_ids, "semantic policy gap")
        _validate_structured_evidence(
            gap.scope,
            gap.evidence,
            evidence_sources,
        )
        eligible, detail = _policy_gap_eligibility(
            gap.gap_type,
            gap.affected_fields,
            gap.evidence,
            claim_fields_by_scope[gap.scope],
        )
        status = "verified" if eligible else "rejected"
        gaps.append(PolicyGap(**gap.model_dump(mode="python"), status=status))
        guard_results.append(
            GuardResult(
                code="POLICY_GAP_ELIGIBILITY",
                status="passed" if eligible else "rejected",
                detail=detail,
                related_id=gap.gap_id,
            )
        )

    missing_information: list[MissingInformation] = []
    missing_ids: set[str] = set()
    for missing in output.missing_information:
        _register_unique_id(
            missing.missing_id, missing_ids, "semantic missing information"
        )
        if missing.item_id not in expected:
            raise SemanticGuardError(
                "semantic missing information references an unknown item"
            )
        missing_information.append(
            MissingInformation.model_validate(missing.model_dump(mode="python"))
        )

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
        missing_information=missing_information,
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


def _validate_structured_evidence(
    scope: str,
    evidence: list[Any],
    evidence_sources: dict[str, dict[str, str]],
    *,
    require_distinct: bool = False,
) -> None:
    if scope not in evidence_sources:
        raise SemanticGuardError("semantic finding references an unknown item")
    normalized = [
        (item.item_id, item.source, item.quote.strip().casefold())
        for item in evidence
    ]
    if any(item_id != scope for item_id, _, _ in normalized):
        raise SemanticGuardError("semantic finding evidence crosses item boundaries")
    if require_distinct and len(normalized) != len(set(normalized)):
        raise SemanticGuardError("semantic contradiction requires distinct evidence")
    for item in evidence:
        source_text = evidence_sources[scope].get(item.source)
        if source_text is None:
            raise SemanticGuardError(
                "semantic finding evidence references an unknown source"
            )
        if item.quote not in source_text:
            raise SemanticGuardError("semantic finding evidence cannot be located")


def _policy_gap_eligibility(
    gap_type: str,
    affected_fields: list[str],
    evidence: list[Any],
    claim_fields: set[str],
) -> tuple[bool, str]:
    unique_evidence = {
        (item.item_id, item.source, item.quote.strip().casefold()) for item in evidence
    }
    if gap_type != "unclassified_privileged_access" and len(unique_evidence) < 2:
        return False, "可影响裁决的规则缺口需要至少两条不同的逐字证据。"
    affected = set(affected_fields)
    if not affected <= claim_fields:
        return False, "规则缺口的每个受影响字段都必须有证据声明，缺失信息只能进入提问通道。"
    eligible = False
    if gap_type == "temporary_permanent_conflict":
        eligible = {"temporary_access", "requested_duration"} <= affected
    elif gap_type == "purpose_target_mismatch":
        eligible = "access_purpose" in affected and bool(
            affected
            & {
                "system_role",
                "destination_environment",
                "destination_object_type",
            }
        )
    elif gap_type == "mixed_business_context":
        eligible = len(affected & claim_fields) >= 2
    elif gap_type == "approval_scope_mismatch":
        eligible = "approval_reference" in affected and bool(
            affected
            & {"access_purpose", "system_role", "requested_duration", "temporary_access"}
        )
    elif gap_type == "unclassified_privileged_access":
        eligible = "maintenance_method" in affected
    return (
        (True, "规则缺口满足服务端证据字段组合要求。")
        if eligible
        else (False, "规则缺口不满足该风险类型的服务端字段组合要求。")
    )


def _register_unique_id(identifier: str, seen: set[str], label: str) -> None:
    if not identifier.strip():
        raise SemanticGuardError(f"{label} id cannot be empty")
    if identifier in seen:
        raise SemanticGuardError(f"duplicate {label} id")
    seen.add(identifier)
