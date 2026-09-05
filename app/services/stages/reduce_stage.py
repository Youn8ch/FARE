"""V4-P5b: the deterministic assembly and the formal reduce stage.

The assembly partitions each item's deterministic findings without deciding;
``reduce_items`` performs exactly one ``DecisionReducer.reduce_item()`` per
item — the single formal adjudication workflow (V4-P2 / D1).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import AclAnalysis, EvaluationItem
from app.services.decision_reducer import (
    DecisionReducer,
    Finding,
    ItemFindingSet,
    SemanticTrace,
)
from app.services.evaluation_types import EvaluationItemContext
from app.services.finding_factory import catalog_findings, network_findings
from app.services.item_assembler import build_item, materialize_final_item, network_item_fields
from app.services.rule_loader import PolicyBundle
from app.services.stages.acl_stage import AclCompatOutcome
from app.services.stages.semantic_stage import SemanticStageOutput


@dataclass(slots=True)
class DeterministicOutcome:
    """One item's deterministic findings, partitioned for the formal reduce.

    The ACL-PATH-001 injection into ``item_matched_rules`` mirrors the
    historical item output: it enters matched_rules only when it is the
    deterministic primary (judged via the reducer's own priority algorithm).
    """

    network_findings: tuple[Finding, ...]
    rule_findings: tuple[Finding, ...]
    catalog_findings: tuple[Finding, ...]
    acl_findings: tuple[Finding, ...]
    matched_rule_ids: tuple[str, ...]
    item_matched_rules: list


def assemble_outcomes(
    policies: PolicyBundle,
    decision_reducer: DecisionReducer,
    contexts: list[EvaluationItemContext],
    acl_outcomes: list[AclCompatOutcome],
) -> tuple[dict[str, DeterministicOutcome], list[AclAnalysis]]:
    """Partition each item's deterministic findings without deciding.

    The frozen insertion order mirrors the historical first-match chain:
    network error, matched rules, catalog errors, then ACL findings.
    """

    outcomes: dict[str, DeterministicOutcome] = {}
    for context, acl in zip(contexts, acl_outcomes, strict=True):
        rule_result = context.rule_result
        network_finds = tuple(network_findings(rule_result.combination))
        catalog_finds = tuple(catalog_findings(rule_result.combination))
        deterministic = [
            *network_finds,
            *rule_result.findings,
            *catalog_finds,
            *acl.findings,
        ]
        primary = decision_reducer.primary_of(deterministic)
        item_matched = list(rule_result.matched_rules)
        no_path_rule = policies.acl_no_path_rule
        if primary is not None and primary.code == no_path_rule.id:
            item_matched.append(no_path_rule)
        outcomes[context.item_id] = DeterministicOutcome(
            network_findings=network_finds,
            rule_findings=rule_result.findings,
            catalog_findings=catalog_finds,
            acl_findings=acl.findings,
            matched_rule_ids=tuple(
                rule.id for rule in rule_result.matched_rules
            ),
            item_matched_rules=item_matched,
        )
    analyses = [
        AclAnalysis(
            raw_analysis=acl.raw_analysis,
            raw_config=acl.raw_config,
            extracted_facts=acl.facts,
        )
        for acl in acl_outcomes
    ]
    return outcomes, analyses


def reduce_items(
    policies: PolicyBundle,
    decision_reducer: DecisionReducer,
    contexts: list[EvaluationItemContext],
    outcomes: dict[str, DeterministicOutcome],
    semantic: SemanticStageOutput,
    acl_outcomes: list[AclCompatOutcome],
) -> tuple[list[EvaluationItem], int]:
    """Formal reduce stage: exactly one ``DecisionReducer.reduce_item()``
    per item. The deterministic snapshot and the final decision are formed
    inside that single workflow (V4-P2; the two-phase reduce of AC-05 was
    deliberately reversed, see docs/v3-baseline.md §6 / D1).
    """

    result: list[EvaluationItem] = []
    deterministic_pending_count = 0
    for context, acl in zip(contexts, acl_outcomes, strict=True):
        outcome = outcomes[context.item_id]
        decision = decision_reducer.reduce_item(
            ItemFindingSet(
                network=outcome.network_findings,
                rules=outcome.rule_findings,
                catalog=outcome.catalog_findings,
                acl=outcome.acl_findings,
                semantic=tuple(semantic.findings.get(context.item_id, ())),
            ),
            matched_rules=outcome.matched_rule_ids,
            semantic=SemanticTrace(
                succeeded=semantic.succeeded,
                review_ids=tuple(semantic.review_ids.get(context.item_id, ())),
                question_ids=tuple(semantic.question_ids.get(context.item_id, ())),
                observation_ids=tuple(
                    semantic.observation_ids.get(context.item_id, ())
                ),
            ),
        )
        if decision.trace is None or decision.deterministic_decision is None:
            raise RuntimeError(
                "the formal reduce_item workflow must return a trace"
            )
        if decision.deterministic_decision == "待定":
            deterministic_pending_count += 1
        item = build_item(
            item_id=context.item_id,
            combination=context.combination,
            facts=acl.facts,
            decision=decision,
            matched=outcome.item_matched_rules,
            no_path_rule=policies.acl_no_path_rule,
        )
        item = materialize_final_item(item, decision)
        result.append(
            item.model_copy(
                update=network_item_fields(
                    context.combination, acl.verification_status
                )
            )
        )
    added_pending_count = max(
        0,
        sum(item.decision == "待定" for item in result)
        - deterministic_pending_count,
    )
    return result, added_pending_count
