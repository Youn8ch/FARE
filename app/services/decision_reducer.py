"""The single formal decision entry point (V4-P2).

Every stage (network / rule / ACL / semantic) only produces Findings;
:class:`DecisionReducer` is the only component allowed to produce a
``合规`` / ``待定`` decision.

- :meth:`DecisionReducer.reduce` is the pure priority algorithm (kept for
  direct unit tests); it never carries a trace.
- :meth:`DecisionReducer.reduce_item` is the formal per-item adjudication
  workflow: exactly one call per item. It computes the deterministic snapshot
  (network + rules + acl partitions) and the final decision (all partitions)
  in the same call, and derives the full ``DecisionTrace`` from them.

Priority: network errors bind first, then formal rules, catalog fact errors,
ACL facts, and finally semantic findings. Findings of the same priority keep
insertion order (stable), secondary findings never override a higher-priority
primary finding, and informational findings (``affects_decision=False``) are
recorded but cannot change a decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.schemas import DecisionTrace

FindingSource = Literal["network", "rule", "acl", "semantic"]

PRIORITY_NETWORK = 0
PRIORITY_RULE = 10
PRIORITY_CATALOG = 20
PRIORITY_ACL = 30
PRIORITY_SEMANTIC = 40


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    source: FindingSource
    reason_type: str | None = None
    priority: int = PRIORITY_SEMANTIC
    detail: str | None = None
    # Informational findings stay observable but never change a decision.
    affects_decision: bool = True


@dataclass(frozen=True, slots=True)
class ItemFindingSet:
    """One item's findings, partitioned by producing stage.

    The flat finding order mirrors the historical first-match chain:
    network error, matched rules, catalog fact errors, ACL findings,
    semantic findings. The deterministic snapshot is
    ``network + rules + catalog + acl``; the final decision additionally
    consumes ``semantic``.
    """

    network: tuple[Finding, ...] = ()
    rules: tuple[Finding, ...] = ()
    catalog: tuple[Finding, ...] = ()
    acl: tuple[Finding, ...] = ()
    semantic: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticTrace:
    """Stage-level semantic context consumed by the formal adjudication.

    ``succeeded=False`` marks the semantic dependency/guard failure path.
    Question/observation buckets never downgrade decisions.
    """

    succeeded: bool
    review_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    decision: str
    primary_finding: Finding | None
    reason_type: str | None
    reason_code: str | None
    findings: tuple[Finding, ...]
    matched_rules: tuple[str, ...]
    # Populated only by the formal reduce_item workflow (V4-P2).
    deterministic_decision: str | None = None
    deterministic_primary: Finding | None = None
    trace: DecisionTrace | None = None


class DecisionReducer:
    def primary_of(self, findings: Sequence[Finding]) -> Finding | None:
        """The primary finding of a sequence: stable highest-priority winner."""

        candidates = [finding for finding in findings if finding.affects_decision]
        if not candidates:
            return None
        # min() is stable: the earliest finding of the highest priority
        # wins, so equal-priority findings keep their insertion order.
        return min(
            enumerate(candidates),
            key=lambda pair: (pair[1].priority, pair[0]),
        )[1]

    def reduce(
        self,
        findings: Sequence[Finding],
        *,
        matched_rules: Iterable[str] = (),
    ) -> Decision:
        all_findings = tuple(findings)
        rules = tuple(matched_rules)
        primary = self.primary_of(all_findings)
        if primary is None:
            return Decision(
                decision="合规",
                primary_finding=None,
                reason_type=None,
                reason_code=None,
                findings=all_findings,
                matched_rules=rules,
            )
        return Decision(
            decision="待定",
            primary_finding=primary,
            reason_type=primary.reason_type,
            reason_code=primary.code,
            findings=all_findings,
            matched_rules=rules,
        )

    def reduce_item(
        self,
        finding_set: ItemFindingSet,
        *,
        matched_rules: Iterable[str] = (),
        semantic: SemanticTrace | None = None,
    ) -> Decision:
        """The formal per-item adjudication: exactly one call per item.

        Computes the deterministic snapshot and the final decision in this
        single call and derives the full ``DecisionTrace``; callers must not
        reduce the same item twice.
        """

        deterministic_findings = (
            *finding_set.network,
            *finding_set.rules,
            *finding_set.catalog,
            *finding_set.acl,
        )
        all_findings = (*deterministic_findings, *finding_set.semantic)
        rules = tuple(matched_rules)

        deterministic_primary = self.primary_of(deterministic_findings)
        final_primary = self.primary_of(all_findings)
        deterministic_decision = (
            "合规" if deterministic_primary is None else "待定"
        )
        if final_primary is None:
            final_decision = "合规"
            final_reason_type = None
            final_reason_code = None
        else:
            final_decision = "待定"
            final_reason_type = final_primary.reason_type
            final_reason_code = final_primary.code

        effect, finding_ids = _semantic_effect_and_ids(
            deterministic_decision=deterministic_decision,
            final_primary=final_primary,
            final_decision=final_decision,
            semantic=semantic,
        )
        trace = DecisionTrace(
            deterministic_decision=deterministic_decision,
            semantic_effect=effect,
            semantic_finding_ids=list(finding_ids),
            final_decision=final_decision,
            final_reason_code=final_reason_code,
        )
        return Decision(
            decision=final_decision,
            primary_finding=final_primary,
            reason_type=final_reason_type,
            reason_code=final_reason_code,
            findings=all_findings,
            matched_rules=rules,
            deterministic_decision=deterministic_decision,
            deterministic_primary=deterministic_primary,
            trace=trace,
        )


def _semantic_effect_and_ids(
    *,
    deterministic_decision: str,
    final_primary: Finding | None,
    final_decision: str,
    semantic: SemanticTrace | None,
) -> tuple[str, list[str]]:
    """The frozen semantic-effect semantics of the two-phase baseline.

    Order matters: pending deterministic items keep their finding ids merged
    (review + question + observation); only a semantic primary on a compliant
    deterministic item downgrades; question/observation buckets never change a
    decision.
    """

    if semantic is None:
        return "unchanged", []
    if not semantic.succeeded:
        return "semantic_failure", []
    if deterministic_decision == "待定":
        return "unchanged", [
            *semantic.review_ids,
            *semantic.question_ids,
            *semantic.observation_ids,
        ]
    downgraded = (
        final_primary is not None
        and final_primary.source == "semantic"
        and deterministic_decision != final_decision
    )
    if downgraded:
        return "downgraded", list(semantic.review_ids)
    if semantic.question_ids:
        return "question_only", list(semantic.question_ids)
    if semantic.observation_ids:
        return "observation_only", list(semantic.observation_ids)
    return "unchanged", []
