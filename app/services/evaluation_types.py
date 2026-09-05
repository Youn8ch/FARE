"""V4-P1: typed contracts between the main-chain stages.

Every stage boundary carries a frozen dataclass; ``dict[str, Any]`` is not an
acceptable sole contract between business stages (V4 invariant 4.x). The
finding partitions produced here feed the single formal reduce in V4-P2.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import AclRawResponse, ExtractedFacts
from app.services.decision_reducer import PRIORITY_RULE, Finding
from app.services.rule_loader import Rule
from app.services.splitter import AccessCombination


@dataclass(frozen=True, slots=True)
class RuleStageResult:
    """One item's single formal rule match.

    ``PolicyBundle.match()`` runs exactly once per item; ACL gating consumes
    this result instead of re-matching.
    """

    item_id: str
    combination: AccessCombination
    matched_rules: tuple[Rule, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class EvaluationItemContext:
    """Neutral per-item context consumed by the downstream stages.

    The semantic, reduce, and post-decision stages receive this contract
    instead of any stage-specific record type. It carries no ACL-named
    field: ACL material travels only through the ACL stage's own output
    and its explicit compatibility adapter until the ACL stage is removed.
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


@dataclass(frozen=True, slots=True)
class AclStageResult:
    """One item's ACL outcome after gating on network facts + rule results.

    Includes the ACL findings (dependency / no-path / ambiguous / port
    mismatch / unresolved firewall, incl. ACL-PATH-001) owned by this stage.
    """

    item_id: str
    combination: AccessCombination
    raw: AclRawResponse | None
    facts: ExtractedFacts
    dependency_error: str | None
    verification_status: str
    findings: tuple[Finding, ...]
