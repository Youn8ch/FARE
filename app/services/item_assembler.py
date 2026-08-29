"""V4-P5a: item assembly, extracted verbatim from ``evaluator.py`` with zero
behavior change (mechanical move).

``build_item`` renders the reducer's deterministic snapshot into an item;
``materialize_final_item`` applies the semantic effect text mapping and
attaches the trace + decision findings produced by the same formal reduce.
"""

from __future__ import annotations

from typing import Any

from app.schemas import (
    Access,
    DecisionFinding,
    EvaluationItem,
    ExtractedFacts,
    MatchedRule,
)
from app.services.decision_reducer import Decision
from app.services.finding_factory import (
    COMPLIANT_REASON,
    SEMANTIC_FAILURE_TEXT,
    finding_text,
    network_facts_of,
    segment_status,
)
from app.services.rule_loader import Rule
from app.services.splitter import AccessCombination


def build_item(
    *,
    item_id: str,
    combination: AccessCombination,
    facts: ExtractedFacts,
    decision: Decision,
    matched: list[Rule],
    no_path_rule: Rule,
) -> EvaluationItem:
    """Build the item from the reducer's deterministic snapshot.

    ``matched`` is pre-injected by the deterministic assembly: ACL-PATH-001
    is present only when it is the deterministic primary (historical
    behavior preserved, judged via the reducer's priority algorithm).
    """

    access = Access(
        source=combination.source_text,
        destination=combination.destination_text,
        protocol=combination.protocol,
        port=combination.port,
    )
    evidence = catalog_evidence(combination) + facts.evidence
    matched_rules = [
        MatchedRule(id=rule.id, name=rule.name, category=rule.category)
        for rule in matched
    ]
    primary = decision.deterministic_primary
    if primary is None:
        return EvaluationItem(
            item_id=item_id,
            access=access,
            decision=decision.deterministic_decision,
            evidence=evidence,
            matched_rules=matched_rules,
            reason=COMPLIANT_REASON[0],
            recommendation=COMPLIANT_REASON[1],
        )
    reason, recommendation = finding_text(primary, matched, no_path_rule)
    return EvaluationItem(
        item_id=item_id,
        access=access,
        decision=decision.deterministic_decision,
        reason_type=primary.reason_type,
        reason_code=primary.code,
        matched_rules=matched_rules,
        evidence=evidence,
        reason=reason,
        recommendation=recommendation,
    )


def materialize_final_item(
    item: EvaluationItem,
    decision: Decision,
) -> EvaluationItem:
    """Apply the reducer's semantic effect to the item text and attach the
    trace + full finding list produced by the same formal reduce call.

    Decisions never change here: every updated decision field comes from
    the reducer's final decision; this is text mapping + audit material
    attachment only. ``decision_findings`` mirrors the reducer's findings
    one-to-one in frozen insertion order (V4-P4).
    """

    trace = decision.trace
    if trace is None:
        return item
    primary = decision.primary_finding
    if (
        trace.semantic_effect == "semantic_failure"
        and primary is not None
        and primary.source == "semantic"
    ):
        item = item.model_copy(
            update={
                "decision": decision.decision,
                "reason_type": decision.reason_type,
                "reason_code": decision.reason_code,
                "reason": SEMANTIC_FAILURE_TEXT[0],
                "recommendation": SEMANTIC_FAILURE_TEXT[1],
            }
        )
    elif trace.semantic_effect == "downgraded":
        has_fact_conflict = (
            primary is not None and primary.code == "SEMANTIC_FACT_CONFLICT"
        )
        item = item.model_copy(
            update={
                "decision": decision.decision,
                "reason_type": decision.reason_type,
                "reason_code": decision.reason_code,
                "reason": (
                    "通过证据守卫的申请语义与权威事实或其他原文证据冲突。"
                    if has_fact_conflict
                    else "存在经证据验证且被服务端影响策略列为人工复核的规则缺口。"
                ),
                "recommendation": (
                    "核对冲突字段并补充无歧义的权威事实。"
                    if has_fact_conflict
                    else "提交人工复核，并由规则责任人评估是否补充正式规则。"
                ),
            }
        )
    decision_findings = [
        DecisionFinding(
            code=finding.code,
            source=finding.source,
            reason_type=finding.reason_type,
            affects_decision=finding.affects_decision,
            detail=finding.detail,
            is_primary=finding is decision.primary_finding,
        )
        for finding in decision.findings
    ]
    return item.model_copy(
        update={
            "decision_trace": trace,
            "decision_findings": decision_findings,
        }
    )


def catalog_evidence(item: AccessCombination) -> list[str]:
    evidence: list[str] = []
    for role, segment in (("源", item.source), ("目的", item.destination)):
        facts = network_facts_of(segment)
        for fact in facts:
            evidence.append(
                f"{role}地址引用网段事实 {fact['fact_id']}（区域 {fact['area_id']}）"
            )
    if item.source.zone is not None:
        evidence.append(
            f"源地址命中 {item.source.catalog_entry_id}（区域 {item.source.zone}）"
        )
    if item.destination.zone is not None:
        evidence.append(
            f"目的地址命中 {item.destination.catalog_entry_id}"
            f"（区域 {item.destination.zone}）"
        )
    return evidence


def network_item_fields(
    combination: AccessCombination, verification_status: str
) -> dict[str, Any]:
    return {
        "source_network_fact_ids": list(
            getattr(combination.source, "network_fact_ids", ())
        ),
        "destination_network_fact_ids": list(
            getattr(combination.destination, "network_fact_ids", ())
        ),
        "source_network_fact_status": segment_status(combination.source),
        "destination_network_fact_status": segment_status(combination.destination),
        "acl_verification_status": verification_status,
    }
