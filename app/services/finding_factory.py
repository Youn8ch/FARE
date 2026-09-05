"""V4-P5a: finding construction and text mapping, extracted verbatim from
``evaluator.py`` with zero behavior change (mechanical move).

The Evaluator no longer owns finding construction or 文案映射; stages and the
reduce workflow consume the functions here.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal

from app.schemas import SemanticAnalysis
from app.services.decision_reducer import (
    PRIORITY_CATALOG,
    PRIORITY_NETWORK,
    PRIORITY_SEMANTIC,
    Finding,
)
from app.services.rule_loader import Rule
from app.services.splitter import AccessCombination

FindingSource = Literal["network", "rule", "semantic"]

SEMANTIC_FAILURE_TEXT = (
    "必要的全申请语义分析未通过依赖或输出守卫，未据此输出合规结论。",
    "检查模型服务和输出契约后提交人工复核。",
)

COMPLIANT_REASON = (
    "权威网络事实完整，且未命中规则包中的拒绝规则。",
    "按既有审批流程继续处理。",
)

NETWORK_ERROR_TEXT = (
    "网段规划权威事实未完整解析，无法形成确定性合规结论。",
    "核实网段规划数据或依赖状态后重新评估。",
)

CATALOG_ERROR_TEXT = {
    "ADDRESS_ANY": "地址使用 any，且未命中已批准的最小开放规则。",
    "ADDRESS_INVALID": "地址无法解析；前置校验契约未满足。",
    "ZONE_UNRESOLVED": "地址子范围未命中唯一的权威网络目录。",
    "ZONE_CONFLICT": "地址子范围同时命中多个权威网络目录项。",
}


def finding_text(primary: Finding, matched: list[Rule]) -> tuple[str, str]:
    if primary.code in CATALOG_ERROR_TEXT:
        return (
            CATALOG_ERROR_TEXT[primary.code],
            "补充或修正权威网络目录，并确保每个地址子范围唯一归属。",
        )
    if primary.source == "network":
        return NETWORK_ERROR_TEXT
    for rule in matched:
        if rule.id == primary.code:
            return rule.reason_template, rule.recommendation
    return COMPLIANT_REASON


def segment_status(segment: object) -> str:
    return str(getattr(segment, "network_fact_status", "complete"))


def network_facts_of(segment: object) -> list[dict[str, Any]]:
    return [
        fact.model_dump(mode="json")
        if hasattr(fact, "model_dump")
        else dataclasses.asdict(fact)
        for fact in getattr(segment, "network_facts", ())
    ]


def primary_network_error(combination: AccessCombination) -> str | None:
    for segment in (combination.source, combination.destination):
        status = segment_status(segment)
        if status not in {"complete", "not_applicable"}:
            return str(getattr(segment, "error_code", None) or "NETWORK_PLAN_INVALID_RESPONSE")
    return None


def network_findings(combination: AccessCombination) -> list[Finding]:
    """Network facts findings; the frozen insertion order starts here."""

    findings: list[Finding] = []
    network_error = primary_network_error(combination)
    if network_error is not None:
        reason_type = (
            "fact_conflict"
            if network_error
            in {
                "NETWORK_PLAN_FACT_CONFLICT",
                "NETWORK_PLAN_SUBNET_MISMATCH",
                "NETWORK_PLAN_NETWORK_MISMATCH",
            }
            else (
                "dependency_failure"
                if network_error
                in {
                    "NETWORK_PLAN_DEPENDENCY_FAILURE",
                    "NETWORK_PLAN_AUTH_FAILURE",
                }
                else "fact_incomplete"
            )
        )
        findings.append(
            Finding(
                code=network_error,
                source="network",
                reason_type=reason_type,
                priority=PRIORITY_NETWORK,
            )
        )
    return findings


def catalog_findings(combination: AccessCombination) -> list[Finding]:
    """Catalog error findings; emitted only without a network error."""

    catalog_error = combination.source.error_code or combination.destination.error_code
    if primary_network_error(combination) is None and catalog_error:
        return [
            Finding(
                code=catalog_error,
                source="network",
                reason_type=(
                    "fact_conflict"
                    if catalog_error == "ZONE_CONFLICT"
                    else "fact_incomplete"
                ),
                priority=PRIORITY_CATALOG,
            )
        ]
    return []


def apply_configured_semantic_effects(
    semantic: SemanticAnalysis, effects: dict[str, str]
) -> SemanticAnalysis:
    contradiction_effect = effects.get("contradiction", "review_required")
    contradictions = [
        contradiction.model_copy(
            update={
                "applied_effect": (
                    contradiction_effect
                    if contradiction.status == "verified"
                    else "observe_only"
                )
            }
        )
        for contradiction in semantic.contradictions
    ]
    gaps = [
        gap.model_copy(
            update={
                "applied_effect": (
                    effects.get(gap.gap_type, "observe_only")
                    if gap.status == "verified"
                    else "observe_only"
                )
            }
        )
        for gap in semantic.policy_gaps
    ]
    return semantic.model_copy(
        update={"contradictions": contradictions, "policy_gaps": gaps}
    )


def semantic_stage_findings(
    item_ids: list[str],
    semantic: SemanticAnalysis,
    *,
    fact_conflict_effect: str,
) -> tuple[
    dict[str, list[Finding]],
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    """Turn verified semantic effects into per-item findings and buckets.

    Findings are appended in stable insertion order: conflicts first, then
    policy gaps. Question/observation buckets never downgrade decisions.
    """

    review_ids: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    observation_ids: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    question_ids: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    semantic_findings: dict[str, list[Finding]] = {
        item_id: [] for item_id in item_ids
    }

    for claim in (*semantic.claims, *semantic.network_claims):
        if claim.status == "conflict":
            target, downgrades = _effect_target(
                fact_conflict_effect, review_ids, question_ids, observation_ids
            )
            target[claim.scope].append(claim.claim_id)
            if downgrades:
                semantic_findings[claim.scope].append(
                    Finding(
                        code="SEMANTIC_FACT_CONFLICT",
                        source="semantic",
                        reason_type="fact_conflict",
                        detail=claim.claim_id,
                        priority=PRIORITY_SEMANTIC,
                    )
                )
    for contradiction in semantic.contradictions:
        if contradiction.status != "verified":
            continue
        target, downgrades = _effect_target(
            contradiction.applied_effect, review_ids, question_ids, observation_ids
        )
        target[contradiction.scope].append(contradiction.contradiction_id)
        if downgrades:
            semantic_findings[contradiction.scope].append(
                Finding(
                    code="SEMANTIC_FACT_CONFLICT",
                    source="semantic",
                    reason_type="fact_conflict",
                    detail=contradiction.contradiction_id,
                    priority=PRIORITY_SEMANTIC,
                )
            )
    for gap in semantic.policy_gaps:
        if gap.status != "verified":
            continue
        target, downgrades = _effect_target(
            gap.applied_effect, review_ids, question_ids, observation_ids
        )
        target[gap.scope].append(gap.gap_id)
        if downgrades:
            semantic_findings[gap.scope].append(
                Finding(
                    code="SEMANTIC_POLICY_GAP",
                    source="semantic",
                    reason_type="risk_uncertain",
                    detail=gap.gap_id,
                    priority=PRIORITY_SEMANTIC,
                )
            )
    for missing in semantic.missing_information:
        question_ids[missing.item_id].append(missing.missing_id)
    return semantic_findings, review_ids, question_ids, observation_ids


def _effect_target(
    effect: str,
    review_ids: dict[str, list[str]],
    question_ids: dict[str, list[str]],
    observation_ids: dict[str, list[str]],
) -> tuple[dict[str, list[str]], bool]:
    if effect == "review_required":
        return review_ids, True
    if effect == "question_only":
        return question_ids, False
    return observation_ids, False

