"""AC-04: the single formal decision entry point.

Every stage (network / rule / ACL / semantic) only produces Findings; the
:class:`DecisionReducer` is the only component allowed to produce a
``合规`` / ``待定`` decision.

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
class Decision:
    decision: str
    primary_finding: Finding | None
    reason_type: str | None
    reason_code: str | None
    findings: tuple[Finding, ...]
    matched_rules: tuple[str, ...]


class DecisionReducer:
    def reduce(
        self,
        findings: Sequence[Finding],
        *,
        matched_rules: Iterable[str] = (),
    ) -> Decision:
        all_findings = tuple(findings)
        rules = tuple(matched_rules)
        candidates = [
            finding for finding in all_findings if finding.affects_decision
        ]
        primary: Finding | None = None
        if candidates:
            # min() is stable: the earliest finding of the highest priority
            # wins, so equal-priority findings keep their insertion order.
            primary = min(
                enumerate(candidates),
                key=lambda pair: (pair[1].priority, pair[0]),
            )[1]
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
