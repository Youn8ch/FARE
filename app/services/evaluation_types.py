"""V4-P1: typed contracts between the main-chain stages.

Every stage boundary carries a frozen dataclass; ``dict[str, Any]`` is not an
acceptable sole contract between business stages (V4 invariant 4.x). The
finding partitions produced here feed the single formal reduce in V4-P2.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.decision_reducer import PRIORITY_RULE, Finding
from app.services.rule_loader import Rule
from app.services.splitter import AccessCombination


@dataclass(frozen=True, slots=True)
class RuleStageResult:
    """One item's single formal rule match.

    ``PolicyBundle.match()`` runs exactly once per item; the stage result is
    the only rule-match channel for downstream stages.
    """

    item_id: str
    combination: AccessCombination
    matched_rules: tuple[Rule, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class EvaluationItemContext:
    """Neutral per-item context consumed by the downstream stages.

    The semantic, reduce, and post-decision stages receive this contract
    instead of any stage-specific record type.
    """

    item_id: str
    combination: AccessCombination
    rule_result: RuleStageResult


def rule_findings(matched_rules: tuple[Rule, ...]) -> tuple[Finding, ...]:
    """Findings for matched rules, in the bundle's frozen rule order."""

    return tuple(
        Finding(
            code=rule.id,
            source="rule",
            reason_type=rule.reason_type,
            priority=PRIORITY_RULE,
        )
        for rule in matched_rules
    )
