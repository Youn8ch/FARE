from __future__ import annotations

import ipaddress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Decision = Literal["合规", "待定"]
ReasonType = Literal[
    "policy_violation",
    "fact_incomplete",
    "fact_conflict",
    "acl_no_path",
    "dependency_failure",
    "risk_uncertain",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddressInput(StrictModel):
    address: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)

    @field_validator("address", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("address")
    @classmethod
    def validate_address_contract(cls, value: str) -> str:
        if value.lower() == "any":
            return value
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("address must be an IP, CIDR, or 'any'") from exc
        return value


class PortRange(StrictModel):
    start: int = Field(ge=0, le=65535)
    end: int = Field(ge=0, le=65535)

    @model_validator(mode="after")
    def ordered(self) -> PortRange:
        if self.end < self.start:
            raise ValueError("port end must be greater than or equal to start")
        return self


class EvaluationRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    sources: list[AddressInput] = Field(min_length=1, max_length=100)
    destinations: list[AddressInput] = Field(min_length=1, max_length=100)
    protocol: str = Field(min_length=1, max_length=32)
    ports: list[PortRange] = Field(min_length=1, max_length=100)
    request_description: str = Field(default="", max_length=10000)

    @field_validator("request_id", "protocol", "request_description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("protocol")
    @classmethod
    def normalize_protocol(cls, value: str) -> str:
        return value.lower()


class Access(StrictModel):
    source: str
    destination: str
    protocol: str
    port: PortRange


class MatchedRule(StrictModel):
    id: str
    name: str
    category: str


class EvaluationItem(StrictModel):
    item_id: str
    access: Access
    decision: Decision
    reason_type: ReasonType | None = None
    reason_code: str | None = None
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str
    recommendation: str
    explanation_source: Literal["llm", "template"] = "template"

    @model_validator(mode="after")
    def pending_requires_reason(self) -> EvaluationItem:
        if self.decision == "待定" and (not self.reason_type or not self.reason_code):
            raise ValueError("pending items require reason_type and reason_code")
        if self.decision == "合规" and (self.reason_type or self.reason_code):
            raise ValueError("compliant items cannot contain pending reason fields")
        return self


class ExtractedFacts(StrictModel):
    firewalls: list[str] = Field(default_factory=list)
    explicit_no_path: bool = False
    candidate_acls: list[str] = Field(default_factory=list)
    address_objects: list[str] = Field(default_factory=list)
    observed_ports: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    ambiguous: bool = False


class AclAnalysis(StrictModel):
    classification: Literal["候选路径与拟新增策略分析，非现网 ACL 状态"] = (
        "候选路径与拟新增策略分析，非现网 ACL 状态"
    )
    raw_analysis: str
    raw_config: str
    extracted_facts: ExtractedFacts


class ModelInfo(StrictModel):
    name: str
    version: str


ClaimStatus = Literal["verified", "candidate", "conflict", "rejected"]


class SemanticClaim(StrictModel):
    claim_id: str
    scope: str
    field: str
    value: str
    source: str
    evidence: str
    confidence: float = Field(ge=0, le=1)
    status: ClaimStatus


class SemanticContradiction(StrictModel):
    contradiction_id: str
    scope: str
    description: str
    evidence: list[str] = Field(min_length=2)
    status: ClaimStatus


class PolicyGap(StrictModel):
    gap_id: str
    scope: str
    description: str
    evidence: list[str] = Field(min_length=1)
    status: ClaimStatus


class GuardResult(StrictModel):
    code: str
    status: Literal["passed", "rejected"]
    detail: str
    related_id: str | None = None


class SemanticAnalysis(StrictModel):
    analyzed_item_ids: list[str]
    claims: list[SemanticClaim] = Field(default_factory=list)
    contradictions: list[SemanticContradiction] = Field(default_factory=list)
    candidate_rule_ids: list[str] = Field(default_factory=list)
    policy_gaps: list[PolicyGap] = Field(default_factory=list)
    questions_for_requester: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    guard_results: list[GuardResult] = Field(default_factory=list)


class EvaluationResponse(StrictModel):
    request_id: str
    decision: Decision
    policy_version: str
    model: ModelInfo
    semantic_analysis: SemanticAnalysis
    items: list[EvaluationItem]
    acl_analysis: AclAnalysis
    audit_id: str


class ErrorDetail(StrictModel):
    code: Literal["evaluation_in_progress", "idempotency_conflict"]
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail


class AclRawResponse(StrictModel):
    analysis: str = ""
    config: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LlmReviewItem(StrictModel):
    item_id: str
    review_required: bool
    risk_category: str | None = None
    candidate_rule_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""
    recommendation: str = ""


class LlmReviewResponse(StrictModel):
    items: list[LlmReviewItem]


class LlmSemanticClaim(StrictModel):
    claim_id: str
    scope: str
    field: str
    value: str
    source: str
    evidence: str
    confidence: float = Field(ge=0, le=1)


class LlmSemanticContradiction(StrictModel):
    contradiction_id: str
    scope: str
    description: str
    evidence: list[str] = Field(min_length=2)


class LlmPolicyGap(StrictModel):
    gap_id: str
    scope: str
    description: str
    evidence: list[str] = Field(min_length=1)


class LlmSemanticResponse(StrictModel):
    analyzed_item_ids: list[str]
    claims: list[LlmSemanticClaim] = Field(default_factory=list)
    contradictions: list[LlmSemanticContradiction] = Field(default_factory=list)
    candidate_rule_ids: list[str] = Field(default_factory=list)
    policy_gaps: list[LlmPolicyGap] = Field(default_factory=list)
    questions_for_requester: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class LlmExplanationItem(StrictModel):
    item_id: str
    explanation: str
    recommendation: str


class LlmExplanationResponse(StrictModel):
    items: list[LlmExplanationItem]


class LlmAclExtractionItem(StrictModel):
    item_id: str
    firewalls: list[str] = Field(default_factory=list)
    candidate_acls: list[str] = Field(default_factory=list)
    address_objects: list[str] = Field(default_factory=list)
    observed_ports: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class LlmAclExtractionResponse(StrictModel):
    items: list[LlmAclExtractionItem]
