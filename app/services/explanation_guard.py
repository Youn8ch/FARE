from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.schemas import EvaluationItem, LlmExplanationItem, LlmExplanationResponse

MAX_EXPLANATION_LENGTH = 4000
RULE_ID_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]*(?:-[A-Z0-9_]+)+\b")
FORBIDDEN_ASSERTION_PATTERNS = (
    re.compile(r"(?:现网|当前网络|生产网络).{0,12}(?:已|已经)?(?:放通|开放|生效)"),
    re.compile(r"(?:已经|已获|已完成)(?:审批|批准)"),
    re.compile(r"(?:路由|NAT).{0,12}(?:已|已经|确认).{0,12}(?:可达|配置|生效|存在)"),
    re.compile(
        r"\b(?:already approved|approval granted|route is reachable|nat is configured)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:live|production)\s+(?:firewall|access control).{0,20}"
        r"(?:allows|permits|is active)\b",
        re.I,
    ),
)
PENDING_CONFLICT_PATTERNS = (
    re.compile(r"结论.{0,4}(?:为|是)?合规"),
    re.compile(r"可直接(?:放行|批准)"),
    re.compile(r"无需(?:整改|复核|审批)"),
    re.compile(r"\b(?:fully compliant|approved for access)\b", re.I),
)
COMPLIANT_CONFLICT_PATTERNS = (
    re.compile(r"结论.{0,4}(?:为|是)?待定"),
    re.compile(r"不合规|必须拒绝|禁止放行"),
    re.compile(r"\b(?:non-compliant|must be denied|pending review)\b", re.I),
)


class ExplanationGuardError(ValueError):
    pass


def guard_explanation_output(
    output: LlmExplanationResponse | Mapping[str, Any],
    *,
    items: list[EvaluationItem],
    valid_rule_ids: set[str],
) -> dict[str, LlmExplanationItem]:
    if isinstance(output, LlmExplanationResponse):
        parsed = output
    elif isinstance(output, Mapping):
        try:
            parsed = LlmExplanationResponse.model_validate(output)
        except ValidationError as exc:
            raise ExplanationGuardError(
                "LLM explanation failed schema validation"
            ) from exc
    else:
        raise ExplanationGuardError("LLM explanation must be a structured object")

    expected = {item.item_id for item in items}
    actual = [item.item_id for item in parsed.items]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ExplanationGuardError(
            "LLM explanation item set must completely and uniquely match request"
        )

    canonical = {item.item_id: item for item in items}
    guarded: dict[str, LlmExplanationItem] = {}
    for explanation in parsed.items:
        explanation_text = explanation.explanation.strip()
        recommendation_text = explanation.recommendation.strip()
        if not explanation_text or not recommendation_text:
            raise ExplanationGuardError("LLM explanation text must not be blank")
        if (
            len(explanation_text) > MAX_EXPLANATION_LENGTH
            or len(recommendation_text) > MAX_EXPLANATION_LENGTH
        ):
            raise ExplanationGuardError("LLM explanation text exceeds maximum length")

        combined = f"{explanation_text}\n{recommendation_text}"
        if any(pattern.search(combined) for pattern in FORBIDDEN_ASSERTION_PATTERNS):
            raise ExplanationGuardError("LLM explanation contains an unauthorized assertion")

        item = canonical[explanation.item_id]
        decision_patterns = (
            PENDING_CONFLICT_PATTERNS
            if item.decision == "待定"
            else COMPLIANT_CONFLICT_PATTERNS
        )
        if any(pattern.search(combined) for pattern in decision_patterns):
            raise ExplanationGuardError("LLM explanation conflicts with the locked decision")

        mentioned_rule_ids = set(explanation.referenced_rule_ids)
        mentioned_rule_ids.update(RULE_ID_PATTERN.findall(combined))
        if not mentioned_rule_ids.issubset(valid_rule_ids):
            raise ExplanationGuardError("LLM explanation referenced a nonexistent rule")
        matched_rule_ids = {rule.id for rule in item.matched_rules}
        if not mentioned_rule_ids.issubset(matched_rule_ids):
            raise ExplanationGuardError("LLM explanation referenced an unmatched rule")

        guarded[explanation.item_id] = explanation.model_copy(
            update={
                "explanation": explanation_text,
                "recommendation": recommendation_text,
                "referenced_rule_ids": sorted(mentioned_rule_ids),
            }
        )
    return guarded
