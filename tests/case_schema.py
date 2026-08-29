from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import EvaluationRequest, PortRange

SCHEMA_VERSION = "2026.08.0"
SuiteName = Literal[
    "core",
    "llm_pipeline",
    "acl_candidates",
    "request_findings",
    "explanation",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dependencies(StrictModel):
    acl_fixture: str | None = None
    policy_dir: str | None = None
    # None keeps the harness default (mock + versioned core fixture);
    # offline_catalog runs the explicit compatibility provider chain.
    network_plan_mode: Literal["mock", "offline_catalog"] | None = None


class FeatureFlags(StrictModel):
    llm_acl_candidate_mode: Literal["off", "shadow"] = "off"
    llm_request_findings_mode: Literal["off", "shadow"] = "off"
    acl_decision_mode: Literal["advisory", "required"] = "advisory"


class LlmProfile(StrictModel):
    profile: Literal["passthrough", "recording", "fixture", "http_stub"]
    semantic_fixture: str | None = None
    acl_candidate_fixture: str | None = None
    request_finding_fixture: str | None = None
    explanation_fixture: str | None = None


class ExpectedAccess(StrictModel):
    source: str
    destination: str
    protocol: str
    port: PortRange


class ExpectedItem(StrictModel):
    index: int = Field(ge=1)
    access: ExpectedAccess
    decision: Literal["合规", "待定"]
    reason_type: str | None
    reason_code: str | None
    matched_rule_ids: list[str]


class ExpectedAclFacts(StrictModel):
    firewalls: list[str] | None = None
    explicit_no_path: bool | None = None
    ambiguous: bool | None = None
    observed_ports: list[int] | None = None


class ExpectedAclAnalysis(StrictModel):
    extracted_facts: ExpectedAclFacts


class ExpectedLlmCalls(StrictModel):
    semantic: int = Field(ge=0)
    acl_candidates: int = Field(ge=0)
    request_findings: int = Field(ge=0)
    explanation: int = Field(ge=0)


class ExpectedLlmText(StrictModel):
    presence: Literal["present", "absent"]
    fallback: bool

    @model_validator(mode="after")
    def fallback_is_absent(self) -> ExpectedLlmText:
        if self.fallback and self.presence != "absent":
            raise ValueError("fallback LLM text must be absent")
        return self


class ExpectedResult(StrictModel):
    http_status: int = 200
    decision: Literal["合规", "待定"]
    items: list[ExpectedItem] = Field(min_length=1, max_length=4)
    acl_analysis: ExpectedAclAnalysis | None = None
    llm_calls: ExpectedLlmCalls
    semantic: dict[str, list[dict[str, Any]]] | None = None
    llm_text: ExpectedLlmText | None = None

    @model_validator(mode="after")
    def item_indices_are_contiguous(self) -> ExpectedResult:
        indices = [item.index for item in self.items]
        if indices != list(range(1, len(indices) + 1)):
            raise ValueError("expected item indices must be unique, ordered, and contiguous")
        return self


class EvaluationCase(StrictModel):
    id: str = Field(min_length=1)
    category: SuiteName
    dependencies: Dependencies
    feature_flags: FeatureFlags
    request: EvaluationRequest
    llm: LlmProfile
    expected: ExpectedResult

    @model_validator(mode="after")
    def fixture_modes_are_consistent(self) -> EvaluationCase:
        if (
            self.feature_flags.llm_acl_candidate_mode == "off"
            and self.llm.acl_candidate_fixture is not None
        ):
            raise ValueError("ACL candidate fixture is forbidden while mode is off")
        if (
            self.feature_flags.llm_request_findings_mode == "off"
            and self.llm.request_finding_fixture is not None
        ):
            raise ValueError("request finding fixture is forbidden while mode is off")
        fixture_values = (
            self.llm.semantic_fixture,
            self.llm.acl_candidate_fixture,
            self.llm.request_finding_fixture,
            self.llm.explanation_fixture,
        )
        if self.llm.profile == "fixture" and not any(fixture_values):
            raise ValueError("fixture profile requires at least one fixture")
        if self.llm.profile != "fixture" and any(fixture_values):
            raise ValueError("only fixture profile may reference LLM fixtures")
        return self


class CaseSuite(StrictModel):
    schema_version: Literal["2026.08.0"]
    suite: SuiteName
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_and_categories_match(self) -> CaseSuite:
        if any(case.category != self.suite for case in self.cases):
            raise ValueError("case category must match suite")
        case_ids = [case.id for case in self.cases]
        request_ids = [case.request.request_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case IDs must be unique within a suite")
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("request IDs must be unique within a suite")
        return self

