"""V4-P5b: the formal rule stage.

Exactly one ``PolicyBundle.match()`` per item; produces only matched rules
and rule findings, never a decision (V4 invariants 4.2).
"""

from __future__ import annotations

from app.services.evaluation_types import RuleStageResult, rule_findings
from app.services.rule_loader import PolicyBundle
from app.services.splitter import AccessCombination


def run(
    policies: PolicyBundle,
    request_id: str,
    combinations: list[AccessCombination],
) -> tuple[RuleStageResult, ...]:
    """item_id is minted here with the historical numbering."""

    total_combinations = len(combinations)
    results: list[RuleStageResult] = []
    for index, combination in enumerate(combinations, start=1):
        matched = policies.match(combination, total_combinations)
        matched_rules = tuple(matched)
        results.append(
            RuleStageResult(
                item_id=f"{request_id}-{index:03d}",
                combination=combination,
                matched_rules=matched_rules,
                findings=rule_findings(matched_rules),
            )
        )
    return tuple(results)
