from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from app.schemas import (
    LlmExplanationResponse,
    LlmPolicyGap,
    LlmRequestFinding,
    LlmRequestFindingsResponse,
    LlmSemanticClaim,
    LlmSemanticContradiction,
    LlmSemanticResponse,
)
from app.services.llm_client import LlmDependencyError

FailureStage = Literal[
    "semantic",
    "request_findings",
    "explanation",
]


def semantic_claim(**updates: Any) -> LlmSemanticClaim:
    value = {
        "claim_id": "claim-001",
        "scope": "case-001",
        "claim_type": "access_purpose",
        "value": "synthetic purpose",
        "source": "request_description",
        "evidence": "synthetic purpose",
        "confidence": 0.9,
    }
    value.update(updates)
    return LlmSemanticClaim.model_validate(value)


def semantic_contradiction(**updates: Any) -> LlmSemanticContradiction:
    value = {
        "contradiction_id": "contradiction-001",
        "scope": "case-001",
        "description": "synthetic contradiction",
        "evidence": ["synthetic evidence A", "synthetic evidence B"],
    }
    value.update(updates)
    return LlmSemanticContradiction.model_validate(value)


def policy_gap(**updates: Any) -> LlmPolicyGap:
    value = {
        "gap_id": "gap-001",
        "scope": "case-001",
        "gap_type": "temporary_permanent_conflict",
        "description": "synthetic policy gap",
        "evidence": [
            {
                "item_id": "case-001",
                "source": "request_description",
                "quote": "synthetic evidence",
            }
        ],
        "affected_fields": ["temporary_access", "requested_duration"],
        "question_for_requester": "Please confirm the requested duration.",
        "suggested_effect": "review_required",
    }
    value.update(updates)
    return LlmPolicyGap.model_validate(value)


def semantic_response(
    item_ids: list[str],
    **updates: Any,
) -> LlmSemanticResponse:
    value: dict[str, Any] = {"analyzed_item_ids": item_ids}
    value.update(updates)
    return LlmSemanticResponse.model_validate(value)


def request_finding(**updates: Any) -> LlmRequestFinding:
    value: dict[str, Any] = {
        "finding_id": "finding-001",
        "finding_type": "missing_approval_context",
        "affected_item_ids": ["case-001"],
        "description": "synthetic missing approval context",
        "evidence": [
            {
                "item_id": "case-001",
                "source": "request_description",
                "quote": "synthetic request",
            }
        ],
        "confidence": 0.8,
        "status": "candidate",
        "question_for_requester": "Please provide synthetic approval context.",
    }
    value.update(updates)
    return LlmRequestFinding.model_validate(value)


def explanation_response(
    item_ids: list[str],
    **item_updates: Any,
) -> LlmExplanationResponse:
    items = [
        {
            "item_id": item_id,
            "explanation": "Synthetic guarded explanation.",
            "recommendation": "Follow the canonical remediation workflow.",
            **item_updates,
        }
        for item_id in item_ids
    ]
    return LlmExplanationResponse.model_validate({"items": items})


class RecordingLlmClient:
    """Deterministic passthrough LLM double with call and failure recording."""

    mode = "mock"

    def __init__(
        self,
        *,
        fail_stage: FailureStage | None = None,
        semantic_response: LlmSemanticResponse | None = None,
        request_findings: LlmRequestFindingsResponse | dict[str, Any] | None = None,
        explanation_response: LlmExplanationResponse | dict[str, Any] | None = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.semantic_response = semantic_response
        self.request_findings = request_findings
        self.explanation_response = explanation_response
        self.failed_stage: FailureStage | None = None
        self.semantic_calls = 0
        self.request_finding_calls = 0
        self.explanation_calls = 0
        self.semantic_item_ids: list[list[str]] = []
        self.request_finding_item_ids: list[list[str]] = []
        self.explanation_item_ids: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "recording-passthrough"

    @property
    def fixture_version(self) -> str:
        return "recording-fixture-2026.09.0"

    @property
    def prompt_versions(self) -> dict[str, str]:
        return {
            "semantic": "recording-2026.09.0",
            "request_findings": "recording-2026.09.0",
            "explanation": "recording-2026.09.0",
        }

    async def analyze(
        self, payload: dict[str, Any]
    ) -> tuple[LlmSemanticResponse, dict[str, Any]]:
        item_ids = [str(item["item_id"]) for item in payload["items"]]
        self.semantic_calls += 1
        self.semantic_item_ids.append(item_ids)
        if self.fail_stage == "semantic":
            self.failed_stage = "semantic"
            raise LlmDependencyError("recorded semantic failure")

        response = (
            self.semantic_response.model_copy(deep=True)
            if self.semantic_response is not None
            else LlmSemanticResponse(analyzed_item_ids=item_ids)
        )
        return response, response.model_dump(mode="json")

    async def analyze_request_findings(
        self,
        inputs: list[dict[str, Any]],
        *,
        request_id: str | None = None,
    ) -> LlmRequestFindingsResponse | dict[str, Any]:
        item_ids = [str(item["item_id"]) for item in inputs]
        self.request_finding_calls += 1
        self.request_finding_item_ids.append(item_ids)
        if self.fail_stage == "request_findings":
            self.failed_stage = "request_findings"
            raise LlmDependencyError("recorded request findings failure")
        if isinstance(self.request_findings, LlmRequestFindingsResponse):
            return self.request_findings.model_copy(deep=True)
        if self.request_findings is not None:
            return deepcopy(self.request_findings)
        return LlmRequestFindingsResponse(analyzed_item_ids=item_ids)

    async def explain(
        self, payload: dict[str, Any]
    ) -> tuple[LlmExplanationResponse | dict[str, Any], dict[str, Any]]:
        item_ids = [str(item["item_id"]) for item in payload["items"]]
        self.explanation_calls += 1
        self.explanation_item_ids.append(item_ids)
        if self.fail_stage == "explanation":
            self.failed_stage = "explanation"
            raise LlmDependencyError("recorded explanation failure")

        if isinstance(self.explanation_response, LlmExplanationResponse):
            response = self.explanation_response.model_copy(deep=True)
            return response, response.model_dump(mode="json")
        if self.explanation_response is not None:
            response = deepcopy(self.explanation_response)
            return response, deepcopy(response)

        response = LlmExplanationResponse.model_validate(
            {
                "items": [
                    {
                        "item_id": item["item_id"],
                        "explanation": item["reason"],
                        "recommendation": item["recommendation"],
                    }
                    for item in payload["items"]
                ]
            }
        )
        return response, response.model_dump(mode="json")
